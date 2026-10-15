import copy

import numpy as np
import pandas as pd
import pytest

from sacva.counterparty import calibrate_counterparties, counterparty_table
from sacva.cva_calc import calc_tables, normalise_cva_calc_rf, to_cva_calc_rf
from sacva.engine import capital
from sacva.loader import BookValidationError

from conftest import ccs, cpty_book, mkt


def _cfg(policy_cfg, **kw):
    cfg = copy.deepcopy(policy_cfg)
    cfg["calibration"].setdefault("counterparty", {}).update(kw)
    return cfg


# ---- CVA_CALC_RF counterparty granularity -------------------------------------------

def _rf_row(**kw):
    base = dict(CALC_RUN_ID="1", BUS_DT="31-Aug-26", INTERNAL_ENTITY="IE1", COUNTERPARTY_ID="C1", RISK_MEASURE="DELTA",
                RISK_TYPE="IR", RISK_SUB_BUCKET="USD", RISK_BUCKET="USD", RISK_FACTOR="5Y", TENOR="", NAME="",
                CREDIT_RTG="", CREDIT_QUALITY="", CREDIT_RELATED="", RISK_WEIGHT="0.0074", SK_CVA="100",
                SK_HEDGE="0", NW_SENSITIVITY="0.74", CCY="CAD")
    base.update(kw)
    return base


@pytest.mark.parametrize("kw,msg", [
    (dict(SK_HEDGE="5", NW_SENSITIVITY="0.777"), "separate rows"),
    (dict(COUNTERPARTY_ID=""), "without COUNTERPARTY_ID"),
    (dict(SK_CVA="0", SK_HEDGE="5", NW_SENSITIVITY="0.037"), "must leave INTERNAL_ENTITY"),
])
def test_rf_counterparty_contract(params, kw, msg):
    with pytest.raises(BookValidationError, match=msg):
        normalise_cva_calc_rf(pd.DataFrame([_rf_row(**kw)]), params)


def test_rf_counterparty_rows_attributed_and_aggregated(params):
    raw = pd.DataFrame([_rf_row(), _rf_row(COUNTERPARTY_ID="C2", SK_CVA="50", NW_SENSITIVITY="0.37"),
                        _rf_row(INTERNAL_ENTITY="", COUNTERPARTY_ID="", SK_CVA="0", SK_HEDGE="-120",
                                NW_SENSITIVITY="-0.888")])
    inp = normalise_cva_calc_rf(raw, params)
    assert sorted(inp.book["counterparty"]) == ["", "IE1|C1", "IE1|C2"]
    # one net factor across counterparties and the hedge: S_B = sum NW = 0.74 + 0.37 - 0.888
    rb = calc_tables(capital(inp.book, params), inp)["CVA_CALC_RB"]
    assert rb["S_B"].iloc[0] == pytest.approx(0.74 + 0.37 - 0.888)


def test_round_trip_counterparty_format(params):
    book = cpty_book(params)
    rf = to_cva_calc_rf(book, params, "1", "31-Aug-26")
    assert {"INTERNAL_ENTITY", "COUNTERPARTY_ID"} <= set(rf.columns)
    assert ((rf["SK_CVA"] != 0) & (rf["SK_HEDGE"] != 0)).sum() == 0
    back = normalise_cva_calc_rf(rf, params)
    assert back.recon_breaks.empty
    assert capital(back.book, params).total == pytest.approx(capital(book, params).total, rel=1e-12)
    assert set(back.book["counterparty"]) == set(book["counterparty"])


# ---- counterparty attributes ------------------------------------------------

def test_counterparty_table_sector_and_policy(params, policy_cfg, mk):
    book = mk(dict(ccs("BigName", "2", "IG", 3.0), counterparty="IE1|C1"),
              dict(ccs("Guarantor", "1a", "IG", 1.0), counterparty="IE1|C1"),
              dict(ccs("MDB", "1a", "IG", 1.0), counterparty="IE1|C2"),
              dict(mkt("FX", "delta", "USDCAD", "", 1.0, unit="per_1pct"), counterparty="IE2|C3"))
    cfg = copy.deepcopy(policy_cfg)
    cfg["assignment"]["counterparties"] = {"IE2|C3": "unhedged"}
    t = counterparty_table(book, cfg).set_index("counterparty")
    assert tuple(t.loc["IE1|C1", ["sector", "quality", "policy"]]) == ("2", "IG", "sector_hedge")  # dominant name
    assert t.loc["IE1|C2", "policy"] == "specific_hedge"                                           # bucket 1a rule
    assert tuple(t.loc["IE2|C3", ["sector", "policy"]]) == ("UNMAPPED", "unhedged")                # no CCS, id map


# ---- counterparty RWs ---------------------------------------------------------

@pytest.fixture(scope="module")
def calib(params, policy_cfg):
    book = cpty_book(params)
    return book, calibrate_counterparties(book, params, policy_cfg)


def test_euler_reconciliation(calib):
    _, res = calib
    r = res["euler_recon"]
    assert (r["check"].abs() <= 1e-6 * r["K_RT"]).all()
    assert (r["unattributed_cva_euler"].abs() <= 1e-6 * r["K_RT"]).all()   # all CVA attributed
    # counterparty allocations (CVA + policy-implied hedge) + unallocated macro hedge reproduce K
    np.testing.assert_allclose(r["cpty_euler_alloc"] + r["unallocated_hedge"], r["K_RT"], rtol=1e-9)


def test_rw_rows_cover_counterparties_and_risk_types(calib):
    book, res = calib
    rw = res["cpty_rw"]
    assert rw["counterparty"].nunique() == book.loc[book["counterparty"] != "", "counterparty"].nunique()
    assert set(zip(rw["risk_class"], rw["measure"])) == {("CCS", "delta"), ("IR", "delta"), ("IR", "vega"),
                                                          ("FX", "delta"), ("FX", "vega")}
    assert rw[["incremental_rw", "euler_rw", "standalone_rw"]].notna().all().all()


def test_specific_policy_vega_costs_more_than_sector_policy(calib):
    _, res = calib
    rw = res["cpty_rw"]
    v = rw[(rw["risk_class"] == "IR") & (rw["measure"] == "vega")]
    spec = v[v["policy"] == "specific_hedge"]["euler_rw"].mean()
    sect = v[v["policy"] == "sector_hedge"]["euler_rw"].mean()
    assert spec > 5 * sect


def test_incremental_converges_to_euler(params, policy_cfg):
    book = cpty_book(params, n_cpty=10, seed=3)
    rw = calibrate_counterparties(book, params, _cfg(policy_cfg, trade_size=1e-6))["cpty_rw"]
    tol = 1e-4 * rw["standalone_rw"].abs() + 1e-12
    assert ((rw["incremental_rw"] - rw["euler_rw"]).abs() <= tol).all()


def test_cluster_is_gross_weighted_and_sector_hedge_only(calib):
    _, res = calib
    rw, cl = res["cpty_rw"], res["cpty_cluster"]
    assert set(cl["policy"]) == {"sector_hedge"}
    sel = rw[(rw["policy"] == "sector_hedge") & (rw["sector"] == "2") & (rw["quality"] == "IG")
             & (rw["risk_class"] == "IR") & (rw["measure"] == "delta")]
    row = cl[(cl["sector"] == "2") & (cl["quality"] == "IG") & (cl["risk_class"] == "IR")
             & (cl["measure"] == "delta")].iloc[0]
    assert row["euler_rw"] == pytest.approx((sel["euler_rw"] * sel["gross_sens"]).sum() / sel["gross_sens"].sum())
    assert row["n_cpty"] == len(sel)


def test_single_factor_standalone_rw_is_regulatory(params, policy_cfg, mk):
    book = mk(dict(mkt("FX", "delta", "USDCAD", "", 3.0, unit="per_1pct"), counterparty="IE1|C1"))
    cfg = copy.deepcopy(policy_cfg)
    cfg["assignment"]["default"] = "unhedged"
    rw = calibrate_counterparties(book, params, cfg)["cpty_rw"].iloc[0]
    assert rw["unit"] == "per_1pct"
    assert rw["standalone_eff_rw"] == pytest.approx(0.11)
    assert rw["standalone_rw"] == pytest.approx(0.11 * 100)          # per $1 per 1% move
    assert rw["gross_sens"] == pytest.approx(300.0)
    assert rw["gross_sens_user"] == pytest.approx(3.0)


def test_ccs_rw_is_per_dollar_cr01(params, policy_cfg, mk):
    book = mk(dict(ccs("A", "2", "IG", 2.0), counterparty="IE1|C1"))
    cfg = copy.deepcopy(policy_cfg)
    cfg["assignment"]["default"] = "unhedged"
    rw = calibrate_counterparties(book, params, cfg)["cpty_rw"].iloc[0]
    assert rw["unit"] == "per_bp"
    # a $1 CR01 (10,000 normalised) stand-alone unhedged IG financial: capital = 0.05 * 10,000
    assert rw["standalone_rw"] == pytest.approx(500.0)
    assert rw["standalone_eff_rw"] == pytest.approx(0.05)


def test_rw_scaling_and_allocation_consistent(calib):
    _, res = calib
    rw = res["cpty_rw"]
    scale = rw["unit"].map({"per_bp": 1e4, "per_1pct": 1e2})
    for c in ("incremental", "euler", "standalone"):
        assert np.allclose(rw[f"{c}_rw"], rw[f"{c}_eff_rw"] * scale)
    assert np.allclose(rw["euler_alloc"], rw["euler_rw"] * rw["gross_sens_user"])
    assert np.allclose(rw["euler_alloc"], rw["euler_eff_rw"] * rw["gross_sens"])
    assert set(rw.loc[rw["measure"] == "vega", "unit"]) == {"per_1pct"}


# ---- name attributes across separate CVA / hedge rows ----------------------------------

def _ccs_rf(name, related, quality="IG", sub="2", cva="-100000", hdg="0", cp="C1", tenor="5Y"):
    rw = {"IG": 0.05, "HYNR": 0.12}[quality]
    return _rf_row(RISK_TYPE="CCS", RISK_SUB_BUCKET=sub, RISK_BUCKET=sub.split("_")[0], RISK_FACTOR=f"{name}_{tenor}",
                   TENOR=tenor, NAME=name, CREDIT_RTG=quality[:2], CREDIT_QUALITY=quality, CREDIT_RELATED=related,
                   RISK_WEIGHT=str(rw), SK_CVA=cva, SK_HEDGE=hdg, NW_SENSITIVITY=str(rw * (float(cva) + float(hdg))),
                   INTERNAL_ENTITY="IE1" if cp else "", COUNTERPARTY_ID=cp)


@pytest.mark.parametrize("hedge_first", [False, True])
def test_blank_parent_on_hedge_row_does_not_break_relation(params, hedge_first):
    cva_a = _ccs_rf("A", "P")
    hdg_a = _ccs_rf("A", "", cva="0", hdg="40000", cp="")
    cva_b = _ccs_rf("B", "P", cp="C2")
    rows_ = [hdg_a, cva_a, cva_b] if hedge_first else [cva_a, hdg_a, cva_b]
    inp = normalise_cva_calc_rf(pd.DataFrame(rows_), params)
    assert set(inp.book.loc[inp.book["name"] == "A", "group"]) == {"P"}
    assert inp.name_conflicts.empty
    # A (WS = -(-1e5 + 4e4)*0.05 = 3000, hedge WS 2000) and B (5000) related at 90%, plus R * 2000^2
    exp = np.sqrt(3000**2 + 5000**2 + 2 * 0.9 * 3000 * 5000 + 0.01 * 2000**2)
    assert capital(inp.book, params).total == pytest.approx(exp)


def test_conflicting_name_attributes_are_flagged(params):
    raw = pd.DataFrame([_ccs_rf("A", "P"), _ccs_rf("A", "Q", quality="HYNR", cva="0", hdg="10", cp=""),
                        _ccs_rf("B", "P", cp="C2")])
    inp = normalise_cva_calc_rf(raw, params)
    c = inp.name_conflicts.set_index("attribute")
    assert set(c.index) == {"CREDIT_QUALITY", "CREDIT_RELATED"}
    assert (c["NAME"] == "A").all()
    # resolved from the CVA rows
    a = inp.book[inp.book["name"] == "A"]
    assert set(a["quality"]) == {"IG"} and set(a["group"]) == {"P"}


# ---- reverse direction --------------------------------------------------------

def test_reverse_direction(params, policy_cfg):
    book = cpty_book(params, n_cpty=8, seed=5)
    rw = calibrate_counterparties(book, params, _cfg(policy_cfg, trade_size=1e-6))["cpty_rw"]
    # at the margin, reducing exposure releases capital at the Euler rate
    tol = 1e-4 * rw["standalone_rw"].abs() + 1e-12
    assert ((rw["incremental_rw_reverse"] + rw["euler_rw"]).abs() <= tol).all()
