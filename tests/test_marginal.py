import math

import pandas as pd
import pytest

from sacva.calibrate import calibrate, ccs_representative_grid, market_grid
from sacva.engine import capital, directional
from sacva.loader import empty_book
from sacva.marginal import marginal
from sacva.policy import assign_policy, build_trade

from conftest import ccs, mkt, random_book


# ---- policy -------------------------------------------------------------

def test_assignment_order(policy_cfg):
    assert assign_policy("X", "1a", "", policy_cfg) == "specific_hedge"
    assert assign_policy("X", "2", "", policy_cfg) == "sector_hedge"
    assert assign_policy("X", "1a", "unhedged", policy_cfg) == "unhedged"
    with pytest.raises(ValueError):
        assign_policy("X", "2", "nonexistent", policy_cfg)


def test_sector_hedge_beta_sized_index(mk, params, policy_cfg):
    trade = build_trade(mk(ccs("A", "2", "IG", 1.0)), "sector_hedge", policy_cfg, params)
    h = trade[trade["source"] == "HDG"].iloc[0]
    assert (h["bucket"], h["name"], h["series"]) == ("8", "CDX.NA.IG", "1")
    assert h["amount"] == pytest.approx(1e4 * 0.05 / 0.015)   # RW-matched


def test_specific_hedge_does_not_hedge_vega(mk, params, policy_cfg):
    cva = mk(mkt("FX", "vega", "USD", "", 1.0, unit="per_1pct"), mkt("FX", "delta", "USD", "", 1.0, unit="per_1pct"))
    trade = build_trade(cva, "specific_hedge", policy_cfg, params)
    hdg = trade[trade["source"] == "HDG"]
    assert list(hdg["measure"]) == ["delta"]


# ---- marginal -----------------------------------------------------------

def test_standalone_unhedged_eff_rw_equals_regulatory_rw(params, policy_cfg):
    grid = ccs_representative_grid(empty_book(), params, policy_cfg)
    g = grid[grid["policy"] == "unhedged"]
    assert (g["standalone_eff_rw"] - g["reg_rw"]).abs().max() < 1e-12
    assert (g["incremental_eff_rw"] - g["reg_rw"]).abs().max() < 1e-12
    assert (g["euler_eff_rw"] - g["reg_rw"]).abs().max() < 1e-12


def test_standalone_index_hedge_hand_calc(params, policy_cfg):
    # IG fin $1 CR01: bucket 2 WS 500; beta index hedge WS -500 in bucket 8 (gamma 45%)
    grid = ccs_representative_grid(empty_book(), params, policy_cfg)
    row = grid[(grid["policy"] == "sector_hedge") & (grid["bucket"] == "2") & (grid["quality"] == "IG")].iloc[0]
    k8 = math.sqrt(500**2 + 0.01 * 500**2)
    exp = math.sqrt(500**2 + k8**2 + 2 * 0.45 * 500 * (-500))
    assert row["standalone_per_unit"] == pytest.approx(exp)


def test_market_standalone_policy_effect(params, policy_cfg):
    g = market_grid(empty_book(), params, policy_cfg).query("direction == 1").set_index(["policy", "risk_class", "measure", "bucket"])
    fxv = lambda p: g.loc[(p, "FX", "vega", "USDCAD"), "standalone_eff_rw"]
    assert fxv("unhedged") == pytest.approx(1.0)
    assert fxv("specific_hedge") == pytest.approx(1.0)           # vega not hedged under specific policy
    assert fxv("sector_hedge") == pytest.approx(0.1)             # sqrt(R) * RW
    assert g.loc[("specific_hedge", "FX", "delta", "USDCAD"), "standalone_eff_rw"] == pytest.approx(0.011)


def test_incremental_converges_to_euler_including_clipped_bucket(mk, params, policy_cfg):
    book = mk(ccs("A", "2", "IG", 1.0), ccs("B", "2", "IG", 1.0), ccs("C", "3", "IG", 1.0),
              ccs("A", "2", "IG", 0.3, source="HDG"))
    for pol in ("unhedged", "sector_hedge"):
        trade = build_trade(mk(ccs("A", "2", "IG", 1.0)), pol, policy_cfg, params)
        m = marginal(book, scaled_down(trade), params, size=1.0)
        eul = directional(book, scaled_down(trade), params)
        assert m.incremental == pytest.approx(eul, rel=1e-4)


def scaled_down(trade, k=1e-7):
    return trade.assign(amount=trade["amount"] * k)


def test_policy_ordering_in_portfolio(params, policy_cfg):
    book = random_book(params, 7)
    g = market_grid(book, params, policy_cfg).query("direction == 1").set_index(["policy", "risk_class", "measure", "bucket"])
    for col in ("incremental_eff_rw", "euler_eff_rw"):
        v = lambda p: g.loc[(p, "IR", "vega", "CAD"), col]
        assert v("unhedged") >= v("specific_hedge") - 1e-12
        assert v("specific_hedge") > v("sector_hedge")


def test_calibrate_full(params, policy_cfg):
    book = random_book(params, 11)
    res = calibrate(book, params, policy_cfg)
    ccs_grid = res["ccs_grid"]
    n_cells = len(policy_cfg["policies"]) * 8 * 2
    assert len(ccs_grid) == n_cells
    assert ccs_grid[["incremental_eff_rw", "euler_eff_rw"]].notna().all().all()
    # random_book attributes CCS CVA to names: every name gets a counterparty CCS RW
    names = book[(book["risk_class"] == "CCS") & (book["source"] == "CVA")]["name"].nunique()
    assert res["counterparty"]["cpty_rw"]["counterparty"].nunique() == names
    factors = policy_cfg["calibration"]["representative_factors"]
    n_dirs = sum(2 if f["measure"] == "delta" else 1 for f in factors)
    assert len(res["market_grid"]) == len(policy_cfg["policies"]) * n_dirs


def _small_size_cfg(policy_cfg, size=1e-6):
    import copy
    cfg = copy.deepcopy(policy_cfg)
    cfg["calibration"]["trade_size"] = size
    return cfg


def test_grid_euler_matches_small_incremental_for_every_cell(params, policy_cfg):
    # regression: IG and HY cells must not share a synthetic factor in the shared gradient
    book = random_book(params, 13)
    cfg = _small_size_cfg(policy_cfg)
    g = ccs_representative_grid(book, params, cfg)
    tol = 1e-4 * g["reg_rw"]
    assert ((g["incremental_eff_rw"] - g["euler_eff_rw"]).abs() <= tol).all(), g


def test_euler_for_risk_class_absent_from_book_is_standalone(mk, params, policy_cfg):
    # regression: book has RCS delta but no RCS vega -> Euler along RCS vega = stand-alone
    book = mk(mkt("RCS", "delta", "3", "", 1.0, unit="per_bp"), ccs("A", "2", "IG", 1.0))
    g = market_grid(book, params, policy_cfg).query("direction == 1").set_index(["policy", "risk_class", "measure", "bucket"])
    row = g.loc[("unhedged", "RCS", "vega", "3")]
    assert row["euler_eff_rw"] == pytest.approx(1.0)
    assert row["incremental_eff_rw"] == pytest.approx(1.0)
    m = marginal(book, mk(mkt("RCS", "vega", "3", "", 1.0, unit="per_1pct")), params)
    assert m.euler == pytest.approx(100.0)


def test_euler_contributions_sum_to_capital(params):
    from sacva.calibrate import euler_contributions
    book = random_book(params, 17)
    res = euler_contributions(book, params)
    K = capital(book, params).total
    assert res["by_factor"]["contribution"].sum() == pytest.approx(K, rel=1e-9)
    assert res["by_counterparty"]["contribution"].sum() == pytest.approx(K, rel=1e-9)
    # CCS CVA rolls up per counterparty (= name here); market CVA is unattributed; hedges in one row per type
    cps = set(res["by_counterparty"]["counterparty"])
    assert {"N0", "HEDGES (macro)", "UNATTRIBUTED CVA"} <= cps


def test_market_grid_prices_both_delta_directions(params, policy_cfg):
    book = random_book(params, 7)
    g = market_grid(book, params, policy_cfg)
    d = g[(g["policy"] == "unhedged") & (g["risk_class"] == "IR") & (g["measure"] == "delta") & (g["bucket"] == "CAD")]
    assert sorted(d["direction"]) == [-1, 1]
    v = g[(g["policy"] == "unhedged") & (g["measure"] == "vega")]
    assert set(v["direction"]) == {1}


def test_normalised_input_matches_per_bp_input(params, policy_cfg):
    # Sensitivities pre-normalised by 0.0001 (a $1/bp CR01 entered as 10,000, no amount_unit column)
    # must give the same capital and RWs as the same book entered per bp.
    from sacva.loader import normalise
    raw_bp = pd.DataFrame([ccs("A", "2", "IG", 3.0), ccs("B", "3", "HY", 2.0), ccs("A", "2", "IG", 1.0, source="HDG"),
                           mkt("IR", "delta", "CAD", "5Y", 4.0, unit="per_bp")])
    raw_norm = raw_bp.assign(amount=raw_bp["amount"] * 1e4).drop(columns="amount_unit")
    bp, norm = normalise(raw_bp, params), normalise(raw_norm, params)
    assert capital(norm, params).total == pytest.approx(capital(bp, params).total)
    g_bp = ccs_representative_grid(bp, params, policy_cfg)
    g_norm = ccs_representative_grid(norm, params, policy_cfg)
    pd.testing.assert_series_equal(g_bp["incremental_eff_rw"], g_norm["incremental_eff_rw"])
    # eff_rw is capital per normalised unit: a stand-alone unhedged 10,000 (= $1/bp) CR01 returns the regulatory RW
    alone = ccs_representative_grid(empty_book(), params, policy_cfg).query("policy == 'unhedged'")
    assert (alone["incremental_per_unit"] - 1e4 * alone["incremental_eff_rw"]).abs().max() < 1e-9
