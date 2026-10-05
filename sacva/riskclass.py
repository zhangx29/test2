"""Per-risk-class regulatory rules: validation, risk weights, intra-bucket rho and cross-bucket gamma.

All functions take the regulatory params dict from :func:`sacva.params.load_params`.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

RISK_CLASSES = ("IR", "FX", "CCS", "RCS", "EQ", "CMDTY")
SINGLE_FACTOR_CLASSES = ("FX", "RCS", "EQ", "CMDTY")  # one risk factor per bucket


def measures(risk_class: str) -> tuple[str, ...]:
    return ("delta",) if risk_class == "CCS" else ("delta", "vega")


def ir_specified(ccy: str, p: dict[str, Any]) -> bool:
    return ccy in set(p["IR"]["specified_currencies"]) | {p["reporting_currency"]}


def validate(rc: str, measure: str, bucket: str, factor: str, name: str, quality: str, p: dict[str, Any]) -> str | None:
    """Return an error message for an invalid sensitivity row, or None if valid."""
    if rc not in RISK_CLASSES:
        return f"unknown risk_class '{rc}'"
    if measure not in measures(rc):
        return f"measure '{measure}' not allowed for {rc}"
    if rc == "IR":
        if not bucket:
            return "IR bucket (currency) required"
        if measure == "delta":
            allowed = p["IR"]["delta"]["tenors"] + ["INFL"] if ir_specified(bucket, p) else ["PARALLEL", "INFL"]
        else:
            allowed = p["IR"]["vega"]["factors"]
        if factor not in allowed:
            return f"IR {measure} risk_factor '{factor}' not in {allowed} for {bucket}"
    elif rc == "FX":
        if not bucket or bucket == p["reporting_currency"]:
            return "FX bucket must be a non-reporting currency"
    elif rc == "CCS":
        if bucket not in p["CCS"]["buckets"]:
            return f"CCS bucket '{bucket}' not in {p['CCS']['buckets']}"
        if factor not in p["CCS"]["tenors"]:
            return f"CCS tenor '{factor}' not in {p['CCS']['tenors']}"
        if not name:
            return "CCS name required"
        if quality not in ("IG", "HY"):
            return f"CCS quality '{quality}' must be IG, HY or NR"
    elif rc == "RCS":
        if bucket not in p["RCS"]["delta_rw"]:
            return f"RCS bucket '{bucket}' invalid"
    elif rc == "EQ":
        if bucket not in p["EQ"]["delta_rw"]:
            return f"EQ bucket '{bucket}' invalid"
    elif rc == "CMDTY":
        if bucket not in p["CMDTY"]["delta_rw"]:
            return f"CMDTY bucket '{bucket}' invalid"
    return None


def risk_weight(rc: str, measure: str, bucket: str, factor: str, quality: str, p: dict[str, Any]) -> float:
    if rc == "IR":
        if measure == "vega":
            return p["IR"]["vega"]["rw"]
        if ir_specified(bucket, p):
            return p["IR"]["delta"]["rw"][factor]
        return p["IR"]["delta"]["other_currency_rw"]
    if rc == "FX":
        return p["FX"][measure]["rw"]
    if rc == "CCS":
        return p["CCS"]["rw"][quality][bucket]
    if rc == "RCS":
        return p["RCS"]["delta_rw"][bucket] if measure == "delta" else p["RCS"]["vega_rw"]
    if rc == "EQ":
        if measure == "delta":
            return p["EQ"]["delta_rw"][bucket]
        return p["EQ"]["vega_rw_large"] if bucket in p["EQ"]["large_cap_buckets"] else p["EQ"]["vega_rw_other"]
    if rc == "CMDTY":
        return p["CMDTY"]["delta_rw"][bucket] if measure == "delta" else p["CMDTY"]["vega_rw"]
    raise ValueError(rc)


def agg_bucket(rc: str, bucket: str) -> str:
    """Bucket used for aggregation. CCS 1a/1b form a single bucket 1 (MAR50.63-64)."""
    if rc == "CCS" and bucket in ("1a", "1b"):
        return "1"
    return bucket


def _sym_lookup(table: dict[str, dict[str, float]], a: str, b: str) -> float:
    if a == b:
        return 1.0
    if a in table and b in table[a]:
        return table[a][b]
    if b in table and a in table[b]:
        return table[b][a]
    raise KeyError(f"no correlation for ({a}, {b})")


def rho_matrix(rc: str, measure: str, f: pd.DataFrame, p: dict[str, Any]) -> np.ndarray:
    """Intra-bucket correlation matrix for the factor rows ``f`` (all in one aggregation bucket)."""
    n = len(f)
    if n == 1:
        return np.ones((1, 1))
    if rc == "IR":
        ccy = f["bucket"].iloc[0]
        factors = list(f["risk_factor"])
        rho = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                a, b = factors[i], factors[j]
                if measure == "vega":
                    r = p["IR"]["vega"]["rho"]
                elif not ir_specified(ccy, p):
                    r = p["IR"]["delta"]["other_currency_rho"]
                elif "INFL" in (a, b):
                    r = p["IR"]["delta"]["rho_inflation"]
                else:
                    r = _sym_lookup(p["IR"]["delta"]["rho_tenor"], a, b)
                rho[i, j] = rho[j, i] = r
        return rho
    if rc == "CCS":
        c = p["CCS"]
        tenor = f["risk_factor"].to_numpy()
        name = f["name"].to_numpy()
        series = f["series"].to_numpy()
        group = f["group"].to_numpy()
        quality = f["quality"].to_numpy()
        is_index = (f["bucket"] == "8").to_numpy()
        r_tenor = np.where(tenor[:, None] == tenor[None, :], 1.0, c["rho_tenor_diff"])
        same_name = name[:, None] == name[None, :]
        related = (group[:, None] == group[None, :]) & (group[:, None] != "")
        r_name_single = np.where(same_name, c["rho_name"]["same"],
                                 np.where(related, c["rho_name"]["related"], c["rho_name"]["other"]))
        same_series = series[:, None] == series[None, :]
        r_name_index = np.where(same_name & same_series, c["rho_name_index"]["same_series"],
                                np.where(same_name, c["rho_name_index"]["other_series"], c["rho_name_index"]["other"]))
        both_index = is_index[:, None] & is_index[None, :]
        r_name = np.where(both_index, r_name_index, r_name_single)
        r_quality = np.where(quality[:, None] == quality[None, :], 1.0, c["rho_quality_diff"])
        rho = r_tenor * r_name * r_quality
        np.fill_diagonal(rho, 1.0)
        return rho
    # Single-factor classes: rows are pre-aggregated to one factor per bucket.
    raise ValueError(f"{rc} buckets must contain a single risk factor, got {n}")


def _codes(*cols: np.ndarray) -> tuple[np.ndarray, int]:
    """Integer partition codes for the joint key of the given columns."""
    if not cols:
        raise ValueError("at least one column")
    key = pd.MultiIndex.from_arrays([np.asarray(c) for c in cols]) if len(cols) > 1 else pd.Index(np.asarray(cols[0]))
    inv, uniq = pd.factorize(key)
    return inv.astype(np.int64), len(uniq)


def ccs_rho_terms(f: pd.DataFrame, p: dict[str, Any]) -> list[tuple[float, np.ndarray | None, int]]:
    """Exact decomposition of the CCS correlation (MAR50.65) into partition terms.

    rho = (a_T + b_T d_tenor) * name_part * (a_Q + b_Q d_quality), with
    name_part = o + r d_group + s d_name                      (single names; group defaults to the name)
              = o + r d_name + s d_(name, series)             (bucket 8 indices)
    so (rho x)_k = sum_terms coef * sum_{l in partition(k)} x_l. Returns (coef, codes or None for "all", n).
    """
    c = p["CCS"]
    a_t, b_t = c["rho_tenor_diff"], 1.0 - c["rho_tenor_diff"]
    a_q, b_q = c["rho_quality_diff"], 1.0 - c["rho_quality_diff"]
    tenor = f["risk_factor"].to_numpy()
    quality = f["quality"].to_numpy()
    name = f["name"].to_numpy()
    if (f["bucket"] == "8").all():
        n = c["rho_name_index"]
        parts = [(n["other"], []), (n["other_series"] - n["other"], [name]),
                 (n["same_series"] - n["other_series"], [name, f["series"].to_numpy()])]
    else:
        n = c["rho_name"]
        group = np.where(f["group"].to_numpy() == "", name, f["group"].to_numpy())
        parts = [(n["other"], []), (n["related"] - n["other"], [group]), (n["same"] - n["related"], [name])]
    terms = []
    for ct, tcols in ((a_t, []), (b_t, [tenor])):
        for cn, ncols in parts:
            for cq, qcols in ((a_q, []), (b_q, [quality])):
                coef = ct * cn * cq
                cols = tcols + ncols + qcols
                if coef == 0.0:
                    continue
                if cols:
                    codes, k = _codes(*cols)
                    terms.append((coef, codes, k))
                else:
                    terms.append((coef, None, 1))
    return terms


def rho_operator(rc: str, measure: str, f: pd.DataFrame, p: dict[str, Any]):
    """Callable x -> rho @ x for the factor rows ``f`` of one aggregation bucket.

    CCS uses the O(n) partition decomposition (buckets can hold thousands of names);
    other classes have at most a handful of factors per bucket and use the dense matrix.
    """
    if rc == "CCS" and len(f) > 1:
        terms = ccs_rho_terms(f, p)

        def apply(x: np.ndarray) -> np.ndarray:
            out = np.zeros_like(x, dtype=float)
            for coef, codes, k in terms:
                if codes is None:
                    out += coef * x.sum()
                else:
                    out += coef * np.bincount(codes, weights=x, minlength=k)[codes]
            return out
        return apply
    rho = rho_matrix(rc, measure, f, p)
    return lambda x: rho @ x


def _rcs_key(b: str) -> tuple[str, str | None]:
    i = int(b)
    if 1 <= i <= 7:
        return str(i), "IG"
    if 8 <= i <= 14:
        return str(i - 7), "HY"
    return str(i), None


def gamma(rc: str, measure: str, b: str, c: str, p: dict[str, Any]) -> float:
    """Cross-bucket correlation between aggregation buckets b and c (b != c)."""
    if rc == "IR":
        return p["IR"]["gamma"]
    if rc == "FX":
        return p["FX"]["gamma"]
    if rc == "CCS":
        return _sym_lookup(p["CCS"]["gamma"], b, c)
    if rc == "RCS":
        (gb, qb), (gc, qc) = _rcs_key(b), _rcs_key(c)
        g = _sym_lookup(p["RCS"]["gamma_same_quality"], gb, gc)
        if qb and qc and qb != qc:
            g *= p["RCS"]["diff_quality_factor"]
        return g
    if rc == "EQ":
        e = p["EQ"]
        if e["gamma_other_bucket"] in (b, c):
            return 0.0
        idx = {"12", "13"}
        if b in idx and c in idx:
            return e["gamma_12_13"]
        if b in idx or c in idx:
            return e["gamma_index_vs_1_10"]
        return e["gamma_1_10"]
    if rc == "CMDTY":
        cm = p["CMDTY"]
        return 0.0 if cm["gamma_other_bucket"] in (b, c) else cm["gamma_1_10"]
    raise ValueError(rc)
