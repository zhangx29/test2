import numpy as np
import pandas as pd
import pytest

from sacva.loader import rows
from sacva.params import load_params, load_policy


@pytest.fixture(scope="session")
def params():
    return load_params()


@pytest.fixture(scope="session")
def policy_cfg():
    return load_policy()


def ccs(name, bucket, quality, amount, tenor="5Y", source="CVA", group="", series="", unit="per_bp", policy=""):
    return dict(source=source, risk_class="CCS", measure="delta", bucket=bucket, risk_factor=tenor,
                name=name, quality=quality, group=group, series=series, amount=amount,
                amount_unit=unit, policy=policy)


def mkt(rc, measure, bucket, factor, amount, source="CVA", unit="per_unit"):
    return dict(source=source, risk_class=rc, measure=measure, bucket=bucket, risk_factor=factor,
                amount=amount, amount_unit=unit)


@pytest.fixture
def mk(params):
    def _mk(*recs):
        return rows(recs, params)
    return _mk


def random_book(params, seed=0, n_names=12):
    """Synthetic book with CVA and hedges across all risk classes (used by property tests)."""
    rng = np.random.default_rng(seed)
    recs = []
    sectors = ["1a", "1b", "2", "3", "4", "5", "6", "7"]
    tenors = params["CCS"]["tenors"]
    for i in range(n_names):
        b = sectors[i % len(sectors)]
        q = "IG" if i % 3 else "HY"
        grp = "G1" if i in (1, 2) else ""
        for t in tenors:
            recs.append(ccs(f"N{i}", b, q, rng.uniform(0.2, 3.0), tenor=t, group=grp))
        if i % 2 == 0:
            recs.append(ccs(f"N{i}", b, q, rng.uniform(0.5, 2.5), tenor="5Y", source="HDG", group=grp))
    for s in ("1", "2"):
        recs.append(ccs("CDX.NA.IG", "8", "IG", rng.uniform(1, 5), tenor="5Y", source="HDG", series=s))
    recs.append(ccs("CDX.NA.HY", "8", "HY", rng.uniform(1, 3), tenor="5Y", source="HDG", series="1"))
    for ccy in ("CAD", "USD", "BRL"):
        factors = ["PARALLEL", "INFL"] if ccy == "BRL" else ["1Y", "2Y", "5Y", "10Y", "30Y", "INFL"]
        for f in factors:
            recs.append(mkt("IR", "delta", ccy, f, rng.normal(0, 5), unit="per_bp"))
            recs.append(mkt("IR", "delta", ccy, f, rng.normal(0, 3), source="HDG", unit="per_bp"))
        for f in ("IRVOL", "INFLVOL"):
            recs.append(mkt("IR", "vega", ccy, f, rng.uniform(0, 4), unit="per_1pct"))
            recs.append(mkt("IR", "vega", ccy, f, rng.uniform(0, 2), source="HDG", unit="per_1pct"))
    for ccy in ("USD", "EUR", "JPY"):
        for m in ("delta", "vega"):
            recs.append(mkt("FX", m, ccy, "", rng.normal(0, 3), unit="per_1pct"))
            recs.append(mkt("FX", m, ccy, "", rng.normal(0, 1), source="HDG", unit="per_1pct"))
    for rc, buckets in (("RCS", ["1", "3", "8", "10", "16"]), ("EQ", ["1", "5", "11", "12"]), ("CMDTY", ["2", "7", "11"])):
        for b in buckets:
            for m in ("delta", "vega"):
                recs.append(mkt(rc, m, b, "", rng.normal(0, 2), unit="per_1pct"))
    return rows(recs, params)


def cpty_book(params, n_cpty=24, seed=0):
    """Counterparty-level book: CVA rows attributed to IE|CP, macro hedges unattributed."""
    rng = np.random.default_rng(seed)
    sectors = ["1a", "1b", "2", "3", "4", "5", "6", "7"]
    recs, tot = [], {}

    def add(rec):
        recs.append(rec)
        if rec["source"] == "CVA" and rec["risk_class"] != "CCS":
            k = (rec["risk_class"], rec["measure"], rec["bucket"], rec["risk_factor"])
            tot[k] = tot.get(k, 0.0) + rec["amount"]

    for i in range(n_cpty):
        cpty = f"IE{i % 2 + 1}|C{i}"
        b, q = sectors[i % len(sectors)], ("HY" if i % 5 == 0 else "IG")
        for t in params["CCS"]["tenors"]:
            add(dict(ccs(f"C{i}", b, q, rng.uniform(0.5, 3) * 1e4, tenor=t, unit="per_unit"), counterparty=cpty))
        for ccy in ("CAD", "USD"):
            for f in ("2Y", "5Y", "10Y"):
                add(dict(mkt("IR", "delta", ccy, f, rng.normal(0, 3) * 1e4), counterparty=cpty))
            add(dict(mkt("IR", "vega", ccy, "IRVOL", rng.uniform(1, 4) * 1e2), counterparty=cpty))
        add(dict(mkt("FX", "delta", "USDCAD", "", rng.normal(0, 2) * 1e2), counterparty=cpty))
        add(dict(mkt("FX", "vega", "USDCAD", "", rng.uniform(1, 3) * 1e2), counterparty=cpty))
    # macro hedges: rates delta 80%, vega 50%, FX delta 90% of the book; CCS index protection
    for (rc, m, b, f), a in tot.items():
        ratio = {"delta": 0.8 if rc == "IR" else 0.9, "vega": 0.5}[m]
        recs.append(mkt(rc, m, b, f, a * ratio, source="HDG"))
    recs.append(ccs("CDX.NA.IG", "8", "IG", 20e4, source="HDG", series="1", unit="per_unit"))
    return rows(recs, params)
