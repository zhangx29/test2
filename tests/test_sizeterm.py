import copy
from pathlib import Path

import numpy as np
import pytest

from sacva.calibrate import NEW_NAME, _ccs_trade
from sacva.cli import main
from sacva.cva_calc import read_input
from sacva.marginal import MarginalEvaluator, scaled
from sacva.policy import build_trade
from sacva.sizeterm import k_eff, size_rw
from sacva.weights import SIZE_OUT, read_weights, sacva_weights, write_weights

EXAMPLE = Path(__file__).parents[1] / "examples" / "cva_calc_rf.csv"
XS = 10 ** np.arange(0, 8.01, 0.5)


def test_closed_form_limits_and_reference_point():
    e, s, i_ref, x_ref = 49.0, 460.0, 239.0, 1e4
    k = k_eff(e, s, i_ref, x_ref, k_class=1e6)
    rw = size_rw(XS, e, s, k)
    assert size_rw(x_ref, e, s, k) == pytest.approx(i_ref, rel=1e-12)
    assert size_rw(1e-6, e, s, k) == pytest.approx(e, rel=1e-6)
    assert size_rw(1e12, e, s, k) == pytest.approx(s, rel=1e-6)
    assert (np.diff(rw) > 0).all() and (rw >= e).all() and (rw <= s).all()
    # same as the unstable form (sqrt(K^2 + 2xKe + x^2 s^2) - K) / x
    assert rw == pytest.approx((np.sqrt(k * k + 2 * XS * k * e + XS ** 2 * s * s) - k) / XS, rel=1e-9)


def test_k_eff_edge_cases():
    assert k_eff(10.0, 50.0, 10.0, 1e4, k_class=3e6) == 3e6          # no curvature: class-level K
    assert k_eff(10.0, 50.0, 9.0, 1e4, k_class=3e6) == 3e6
    assert k_eff(10.0, 50.0, 50.0, 1e4, k_class=3e6) == 0.0          # already stand-alone at x_ref
    assert size_rw(5.0, 10.0, 50.0, 0.0) == 50.0
    assert np.isnan(k_eff(np.nan, 50.0, 20.0, 1e4, 3e6))
    # negative Euler (trade offsets the book) stays well defined and inside [e, s]
    k = k_eff(-20.0, 50.0, 5.0, 1e4, 3e6)
    rw = size_rw(XS, -20.0, 50.0, k)
    assert rw[0] == pytest.approx(-20.0, abs=1e-2) and (np.diff(rw) > 0).all() and rw[-1] <= 50.0
    assert size_rw(1.0, -20.0, 50.0, k, floor=0.1) == pytest.approx(5.0)   # floor at 10% of stand-alone
    with pytest.raises(ValueError):
        size_rw(0.0, 1.0, 2.0, 3.0)


@pytest.fixture(scope="module")
def example(params):
    return read_input(EXAMPLE, params).book


@pytest.mark.parametrize("pol,b,q,max_err", [("unhedged_delta", "2", "IG", 0.001), ("sector_hedge", "2", "IG", 0.01),
                                             ("name_hedge", "2", "IG", 0.002), ("unhedged_delta", "6", "HY", 0.03)])
def test_fit_tracks_full_recalculation(example, params, policy_cfg, pol, b, q, max_err):
    """Memo 4.2: K_eff calibrated at $10k reproduces the full incremental curve from $1 to $100mm."""
    ev = MarginalEvaluator(example, params, 1.0)
    t = build_trade(_ccs_trade(f"{NEW_NAME}{b}_{q}", b, q, {"5Y": 1.0}, "per_bp", params), pol, policy_cfg, params)
    r = ev.evaluate(t)
    full = np.array([ev.evaluate(scaled(t, x)).incremental / x for x in XS])
    k = k_eff(r.incremental, r.standalone, full[8], 1e4, ev.base_k[("CCS", "delta")])
    fit = size_rw(XS, r.incremental, r.standalone, k)
    assert fit[8] == pytest.approx(full[8], rel=1e-10)
    assert np.max(np.abs(fit - full)) / r.standalone < max_err


@pytest.fixture(scope="module")
def runs(example, params, policy_cfg):
    cfg = copy.deepcopy(policy_cfg)
    cfg["sacva_weights"]["own_lines"] = {"WB001": "unhedged_delta"}
    ev = MarginalEvaluator(example, params, 1.0)
    return (sacva_weights(example, params, cfg, ev), sacva_weights(example, params, cfg, ev, method="size_term"),
            ev)


def test_method_switch(runs, policy_cfg, example, params):
    cur, st, _ = runs
    assert cur["method"] == "current" and st["method"] == "size_term"
    tenors = ["6m", "1y", "3y", "5y", "10y"]
    assert list(cur["weights"].columns) == ["cptyCode", "hedgeType", "bucket", "igHy", "vegaCost"] + tenors
    vega_out = ["vega" + o[0].upper() + o[1:] for o in SIZE_OUT]
    assert list(st["weights"].columns) == list(cur["weights"].columns) + SIZE_OUT + vega_out
    w = st["weights"]
    # $1 base e in size_term is the current method's number; published tenor / vegaCost = RW at ref size
    assert np.allclose(w["eulerRw"], cur["weights"]["5y"], equal_nan=True)
    assert np.allclose(w["vegaEulerRw"], cur["weights"]["vegaCost"], equal_nan=True)
    assert np.allclose(w["5y"], w["refRw"]) and np.allclose(w["vegaCost"], w["vegaRefRw"])
    assert (w["refSize"] == 1e4).all() and (w["vegaRefSize"] == 1e4).all()
    assert ((w["eulerRw"] <= w["refRw"] + 1e-9) & (w["refRw"] <= w["standaloneRw"] + 1e-9)).all()
    # pricing at the reference size gives back refRw in every row
    assert np.allclose(size_rw(1e4, w["eulerRw"], w["standaloneRw"], w["kEff"]), w["refRw"], rtol=1e-8)
    bad = copy.deepcopy(policy_cfg)
    bad["sacva_weights"]["method"] = "flat"
    with pytest.raises(ValueError, match="method"):
        sacva_weights(example, params, bad)


def test_size_term_grid_cell_matches_new_name_curve(runs, params, policy_cfg, example):
    """An empty cell (unhedged 1_A HY) falls back to the new-name grid, with e, s, i_ref of that probe."""
    _, st, ev = runs
    d = st["detail"].set_index(["hedgeType", "bucket", "igHy"]).loc[("unhedged", "1_A", "HY")]
    assert d["ccs_source"] == "new-name grid"
    t = build_trade(_ccs_trade(f"{NEW_NAME}1a_HY", "1a", "HY", {"5Y": 1.0}, "per_bp", params), "unhedged_delta",
                    policy_cfg, params)
    assert d["ccs_i_ref"] == pytest.approx(ev.evaluate(scaled(t, 1e4)).incremental / 1e4, rel=1e-10)
    assert d["ccs_s"] == pytest.approx(ev.evaluate(t).standalone, rel=1e-10)


def test_price_cli_both_methods(runs, tmp_path, capsys):
    cur, st, _ = runs
    p_cur = write_weights(cur["weights"], tmp_path / "cur.csv")
    p_st = write_weights(st["weights"], tmp_path / "st.csv")
    w = read_weights(p_st)
    assert list(w.columns) == list(st["weights"].columns)
    row = w[(w["hedgeType"] == "sector_hedge") & (w["bucket"] == "2") & (w["igHy"] == "IG")].iloc[0]
    args = ["--hedge-type", "sector_hedge", "--bucket", "2", "--ig-hy", "IG", "--cr01", "10000"]
    main(["price", "--weights", str(p_st)] + args)
    out = capsys.readouterr().out
    assert "method: size_term" in out and f"capital {row['refRw'] * 1e4:,.2f}" in out
    main(["price", "--weights", str(p_cur)] + args)
    out = capsys.readouterr().out
    assert "method: current" in out and f"capital {row['eulerRw'] * 1e4:,.2f}" in out
