"""Golden tests: expected values computed by hand from OSFI CAR Ch. 8 (MAR50.51-53, 50.55-77)."""
import math

import numpy as np
import pandas as pd
import pytest

from sacva.engine import capital, gradient
from sacva.loader import BookValidationError

from conftest import ccs, mkt, random_book


def K(book, params):
    return capital(book, params).total


# ---- CCS ---------------------------------------------------------------

def test_single_name_unhedged_equals_rw_times_sensitivity(mk, params):
    # $1 CR01 = 1e4 regulatory units; IG financial RW 5% -> WS = 500; m_CVA = 1
    assert K(mk(ccs("A", "2", "IG", 1.0)), params) == pytest.approx(500.0)


def test_nr_maps_to_hy(mk, params):
    assert K(mk(ccs("A", "2", "NR", 1.0)), params) == pytest.approx(1200.0)


def test_perfect_single_name_hedge_leaves_disallowance(mk, params):
    # WS = 500 - 500 = 0; K_b = sqrt(R * 500^2) = 50
    book = mk(ccs("A", "2", "IG", 1.0), ccs("A", "2", "IG", 1.0, source="HDG"))
    assert K(book, params) == pytest.approx(50.0)


def test_two_names_same_bucket(mk, params):
    # rho = 0.5 (other name) -> sqrt(500^2 + 500^2 + 2*0.5*500*500)
    book = mk(ccs("A", "2", "IG", 1.0), ccs("B", "2", "IG", 1.0))
    assert K(book, params) == pytest.approx(math.sqrt(750000))


def test_related_names_and_quality_and_tenor(mk, params):
    # A IG 5Y WS=500, B HY 1Y WS=1200 related: rho = 0.9 tenor * 0.9 name * 0.8 quality = 0.648
    book = mk(ccs("A", "2", "IG", 1.0, group="G"), ccs("B", "2", "HY", 1.0, tenor="1Y", group="G"))
    exp = math.sqrt(500**2 + 1200**2 + 2 * 0.648 * 500 * 1200)
    assert K(book, params) == pytest.approx(exp)


def test_1a_1b_share_bucket(mk, params):
    # 1a IG WS 50, 1b IG WS 100 in the same aggregation bucket with rho 0.5
    book = mk(ccs("A", "1a", "IG", 1.0), ccs("B", "1b", "IG", 1.0))
    assert K(book, params) == pytest.approx(math.sqrt(2500 + 10000 + 5000))


def test_cross_bucket_gamma(mk, params):
    # bucket 2 WS 500, bucket 3 WS 300, gamma 5%
    book = mk(ccs("A", "2", "IG", 1.0), ccs("C", "3", "IG", 1.0))
    assert K(book, params) == pytest.approx(math.sqrt(500**2 + 300**2 + 2 * 0.05 * 500 * 300))


def test_sb_clipped_at_kb(mk, params):
    # bucket 2: Kb = sqrt(750000) = 866.03 < sum 1000 -> S_b = Kb
    book = mk(ccs("A", "2", "IG", 1.0), ccs("B", "2", "IG", 1.0), ccs("C", "3", "IG", 1.0))
    kb = math.sqrt(750000)
    exp = math.sqrt(kb**2 + 300**2 + 2 * 0.05 * kb * 300)
    assert K(book, params) == pytest.approx(exp)


def test_index_bucket_8_series(mk, params):
    # same index, other series: rho_name 0.9; same tenor; same quality -> 0.9. RW 1.5% -> WS 150 each (hedge)
    book = mk(ccs("CDX", "8", "IG", 1.0, source="HDG", series="1"),
              ccs("CDX", "8", "IG", 1.0, source="HDG", series="2"))
    ws = -150.0
    kb2 = ws**2 * 2 + 2 * 0.9 * ws * ws + 0.01 * (150**2 * 2)
    assert K(book, params) == pytest.approx(math.sqrt(kb2))


# ---- market risk classes ----------------------------------------------

def test_ir_delta_tenor_correlation(mk, params):
    book = mk(mkt("IR", "delta", "CAD", "5Y", 1.0, unit="per_bp"), mkt("IR", "delta", "CAD", "10Y", 1.0, unit="per_bp"))
    assert K(book, params) == pytest.approx(74 * math.sqrt(2 + 2 * 0.91))


def test_ir_other_currency(mk, params):
    book = mk(mkt("IR", "delta", "BRL", "PARALLEL", 1.0, unit="per_bp"), mkt("IR", "delta", "BRL", "INFL", 1.0, unit="per_bp"))
    assert K(book, params) == pytest.approx(158 * math.sqrt(2 + 2 * 0.4))


def test_ir_cross_currency_gamma(mk, params):
    book = mk(mkt("IR", "delta", "CAD", "5Y", 1.0, unit="per_bp"), mkt("IR", "delta", "USD", "5Y", 1.0, unit="per_bp"))
    assert K(book, params) == pytest.approx(74 * math.sqrt(2 + 2 * 0.5))


def test_fx_delta_and_vega(mk, params):
    book = mk(mkt("FX", "delta", "USD", "", 1.0, unit="per_1pct"), mkt("FX", "delta", "EUR", "", 1.0, unit="per_1pct"),
              mkt("FX", "vega", "USD", "", 1.0, unit="per_1pct"))
    # delta: 11 each, gamma 0.6; vega: 100% RW on s = 100 -> 100. Delta and vega simply summed.
    assert K(book, params) == pytest.approx(11 * math.sqrt(3.2) + 100)


def test_rcs_diff_quality_gamma_halved(mk, params):
    book = mk(mkt("RCS", "delta", "1", "", 1.0, unit="per_bp"), mkt("RCS", "delta", "8", "", 1.0, unit="per_bp"))
    # RW 0.5% -> 50; RW 2.0% -> 200; gamma = 1.0/2
    assert K(book, params) == pytest.approx(math.sqrt(50**2 + 200**2 + 2 * 0.5 * 50 * 200))


def test_eq_and_cmdty_other_bucket_zero_gamma(mk, params):
    book = mk(mkt("EQ", "delta", "11", "", 1.0, unit="per_1pct"), mkt("EQ", "delta", "5", "", 1.0, unit="per_1pct"),
              mkt("CMDTY", "vega", "2", "", 1.0, unit="per_1pct"))
    assert K(book, params) == pytest.approx(math.sqrt(70**2 + 30**2) + 100)


def test_ccs_vega_rejected(mk):
    with pytest.raises(BookValidationError):
        mk(dict(source="CVA", risk_class="CCS", measure="vega", bucket="2", risk_factor="5Y", name="A", quality="IG", amount=1))


def test_bad_tenor_rejected(mk):
    with pytest.raises(BookValidationError, match="row 2"):
        mk(ccs("A", "2", "IG", 1.0, tenor="7Y"))


# ---- gradient properties ------------------------------------------------

@pytest.mark.parametrize("seed", [0, 1, 2])
def test_euler_contributions_sum_to_K(params, seed):
    book = random_book(params, seed)
    g = gradient(book, params)
    euler = float((g["cva"] * g["d_cva"] + g["hdg"] * g["d_hdg"]).sum())
    assert euler == pytest.approx(capital(book, params).total, rel=1e-9)


@pytest.mark.parametrize("seed", [0, 3])
def test_gradient_matches_finite_difference(params, seed):
    book = random_book(params, seed)
    g = gradient(book, params)
    base = capital(book, params).total
    rng = np.random.default_rng(seed)
    for idx in rng.choice(len(book), size=25, replace=False):
        b2 = book.copy()
        h = 1e-3 * max(1.0, abs(book.at[idx, "amount"]))
        b2.at[idx, "amount"] += h
        b3 = book.copy()
        b3.at[idx, "amount"] -= h
        fd = (capital(b2, params).total - capital(b3, params).total) / (2 * h)
        r = book.loc[idx]
        key = (g["risk_class"] == r["risk_class"]) & (g["measure"] == r["measure"]) & (g["bucket"] == r["bucket"]) \
            & (g["risk_factor"] == r["risk_factor"]) & (g["name"] == r["name"]) & (g["series"] == r["series"])
        col = "d_cva" if r["source"] == "CVA" else "d_hdg"
        assert float(g.loc[key, col].iloc[0]) == pytest.approx(fd, rel=1e-5, abs=1e-6)


def test_homogeneity(params):
    book = random_book(params, 5)
    b2 = book.copy()
    b2["amount"] *= 3.0
    assert capital(b2, params).total == pytest.approx(3.0 * capital(book, params).total)


@pytest.mark.parametrize("bucket", ["2", "8"])
def test_ccs_rho_operator_matches_dense_matrix(params, bucket):
    from sacva import riskclass
    rng = np.random.default_rng(1)
    n = 60
    f = pd.DataFrame(dict(
        risk_class="CCS", measure="delta", bucket=bucket,
        risk_factor=rng.choice(params["CCS"]["tenors"], n),
        name=rng.choice([f"N{i}" for i in range(12)], n),
        series=rng.choice(["1", "2"], n) if bucket == "8" else "",
        quality=rng.choice(["IG", "HY"], n)))
    grp = {f"N{i}": ("G1" if i < 4 else "G2" if i < 6 else "") for i in range(12)}
    f["group"] = f["name"].map(grp) if bucket != "8" else ""
    x = rng.normal(size=n)
    dense = riskclass.rho_matrix("CCS", "delta", f, params) @ x
    fast = riskclass.rho_operator("CCS", "delta", f, params)(x)
    np.testing.assert_allclose(fast, dense, rtol=1e-12, atol=1e-12)
