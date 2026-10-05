"""SA-CVA capital (OSFI CAR Ch. 8, MAR50.51-53) and its closed-form gradient.

Per risk class and measure (delta / vega):

    WS_k  = RW_k s_k^CVA - RW_k s_k^Hdg
    K_b   = sqrt( WS' rho WS + R * sum (RW_k s_k^Hdg)^2 )
    S_b   = max(-K_b, min(sum WS_k, K_b))
    K     = m_CVA * sqrt( sum K_b^2 + sum_{b!=c} gamma_bc S_b S_c )

and total SA-CVA = sum over risk classes and measures.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from . import riskclass

FACTOR_KEY = ["risk_class", "measure", "bucket", "risk_factor", "name", "series"]


@dataclass
class CapitalResult:
    total: float
    by_class: pd.DataFrame   # risk_class, measure, K
    by_bucket: pd.DataFrame  # risk_class, measure, bucket, K_b, S_b, sum_WS


def aggregate(book: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
    """Net sensitivities per risk factor: one row per factor with columns cva, hdg, rw, agg_bucket."""
    if book.empty:
        return pd.DataFrame(columns=FACTOR_KEY + ["quality", "group", "cva", "hdg", "rw", "agg_bucket"])
    b = book.copy()
    # a name's legal-entity group may be populated on some rows only (e.g. CVA rows but not hedge rows)
    with_group = (b["risk_class"] == "CCS") & (b["group"] != "")
    if with_group.any():
        grp = b[with_group].drop_duplicates("name").set_index("name")["group"]
        is_ccs = b["risk_class"] == "CCS"
        b.loc[is_ccs, "group"] = b.loc[is_ccs, "name"].map(grp).fillna(b.loc[is_ccs, "group"])
    b["cva"] = np.where(b["source"] == "CVA", b["amount"], 0.0)
    b["hdg"] = np.where(b["source"] == "HDG", b["amount"], 0.0)
    f = (b.groupby(FACTOR_KEY, sort=True, dropna=False)
         .agg(quality=("quality", "first"), group=("group", "first"), cva=("cva", "sum"), hdg=("hdg", "sum"))
         .reset_index())
    f["rw"] = [riskclass.risk_weight(r.risk_class, r.measure, r.bucket, r.risk_factor, r.quality, params)
               for r in f.itertuples()]
    f["agg_bucket"] = [riskclass.agg_bucket(rc, bk) for rc, bk in zip(f["risk_class"], f["bucket"])]
    # names without a legal-entity group form their own group (rho_name 'related' needs a shared parent)
    ccs = f["risk_class"] == "CCS"
    f.loc[ccs & (f["group"] == ""), "group"] = f.loc[ccs & (f["group"] == ""), "name"]
    return f.reset_index(drop=True)


class ClassModel:
    """One risk class / measure compiled for repeated evaluation (buckets, RWs, rho operators, gamma).

    ``evaluate(cva, hdg)`` takes net CVA / hedge sensitivities aligned with the factor rows it was built
    from and returns capital, bucket figures and, optionally, the gradient. Only numpy work is done per
    call, so the same model serves capital, gradients and thousands of marginal evaluations.
    """

    def __init__(self, f: pd.DataFrame, rc: str, measure: str, params: dict[str, Any]):
        self.rc, self.measure = rc, measure
        self.R = params["hedging_disallowance_R"]
        self.m = params["m_cva"]
        self.rw = f["rw"].to_numpy(float)
        agg = f["agg_bucket"].to_numpy()
        self.buckets = sorted(set(agg), key=lambda x: (len(x), x))
        self.pos = [np.flatnonzero(agg == b) for b in self.buckets]
        self.rho = [riskclass.rho_operator(rc, measure, f.iloc[idx], params) for idx in self.pos]
        nb = len(self.buckets)
        self.gamma = np.zeros((nb, nb))
        for i, b in enumerate(self.buckets):
            for j, c in enumerate(self.buckets):
                if i != j:
                    self.gamma[i, j] = riskclass.gamma(rc, measure, b, c, params)

    def evaluate(self, cva: np.ndarray, hdg: np.ndarray, want_grad: bool = False):
        """Returns (K, K_b, S_b, sum_WS, d_cva, d_hdg); gradients are None unless ``want_grad``."""
        R, rw = self.R, self.rw
        H = rw * hdg
        WS = rw * cva - H
        nb = len(self.buckets)
        Kb, Sb, sums = np.zeros(nb), np.zeros(nb), np.zeros(nb)
        rhoWS = []
        for i, idx in enumerate(self.pos):
            ws = WS[idx]
            r = self.rho[i](ws)
            Kb[i] = np.sqrt(max(float(ws @ r + R * H[idx] @ H[idx]), 0.0))
            sums[i] = ws.sum()
            Sb[i] = max(-Kb[i], min(sums[i], Kb[i]))
            rhoWS.append(r)
        Q = float(Kb @ Kb + Sb @ self.gamma @ Sb)
        K = self.m * np.sqrt(max(Q, 0.0))
        if not want_grad:
            return K, Kb, Sb, sums, None, None
        d_cva, d_hdg = np.zeros(len(rw)), np.zeros(len(rw))
        if Q > 0:
            dK_dQ = self.m / (2.0 * np.sqrt(Q))
            dQ_dS = 2.0 * (self.gamma @ Sb)
            for i, idx in enumerate(self.pos):
                if Kb[i] > 0:
                    dKb_dWS, dKb_dH = rhoWS[i] / Kb[i], R * H[idx] / Kb[i]
                else:
                    dKb_dWS, dKb_dH = np.zeros(len(idx)), np.zeros(len(idx))
                # An empty bucket (K_b = 0) moves along S_b = sum WS for any direction, since |sum WS| <= K_b.
                if Kb[i] == 0.0 or -Kb[i] < sums[i] < Kb[i]:
                    dS_dWS, dS_dH = np.ones(len(idx)), np.zeros(len(idx))
                elif sums[i] >= Kb[i]:
                    dS_dWS, dS_dH = dKb_dWS, dKb_dH
                else:
                    dS_dWS, dS_dH = -dKb_dWS, -dKb_dH
                dK_dWS = dK_dQ * (2.0 * rhoWS[i] + dQ_dS[i] * dS_dWS)      # 2 K_b dK_b/dWS = 2 rho WS
                dK_dH = dK_dQ * (2.0 * R * H[idx] + dQ_dS[i] * dS_dH)       # 2 K_b dK_b/dH  = 2 R H
                d_cva[idx] = rw[idx] * dK_dWS
                d_hdg[idx] = rw[idx] * (-dK_dWS + dK_dH)
        return K, Kb, Sb, sums, d_cva, d_hdg


def compile_models(f: pd.DataFrame, params: dict[str, Any]) -> dict[tuple[str, str], tuple[ClassModel, np.ndarray]]:
    """ClassModel per (risk_class, measure) with the row positions of its factors in ``f``."""
    out = {}
    for (rc, measure), grp in f.groupby(["risk_class", "measure"], sort=True):
        idx = grp.index.to_numpy()
        out[(rc, measure)] = (ClassModel(f.loc[idx].reset_index(drop=True), rc, measure, params), idx)
    return out


def _run(book: pd.DataFrame, params: dict[str, Any], want_grad: bool):
    f = aggregate(book, params)
    f["d_cva"] = 0.0
    f["d_hdg"] = 0.0
    class_rows, bucket_rows = [], []
    cva, hdg = f["cva"].to_numpy(float), f["hdg"].to_numpy(float)
    for (rc, measure), (model, idx) in compile_models(f, params).items():
        K, Kb, Sb, sums, dc, dh = model.evaluate(cva[idx], hdg[idx], want_grad)
        class_rows.append(dict(risk_class=rc, measure=measure, K=K))
        bucket_rows.extend(dict(risk_class=rc, measure=measure, bucket=b, K_b=Kb[i], S_b=Sb[i], sum_WS=sums[i])
                           for i, b in enumerate(model.buckets))
        if want_grad:
            f.loc[idx, "d_cva"] = dc
            f.loc[idx, "d_hdg"] = dh
    by_class = pd.DataFrame(class_rows, columns=["risk_class", "measure", "K"])
    by_bucket = pd.DataFrame(bucket_rows, columns=["risk_class", "measure", "bucket", "K_b", "S_b", "sum_WS"])
    total = float(by_class["K"].sum()) if len(by_class) else 0.0
    return CapitalResult(total, by_class, by_bucket), f


def capital(book: pd.DataFrame, params: dict[str, Any]) -> CapitalResult:
    """SA-CVA capital of a normalised book."""
    return _run(book, params, want_grad=False)[0]


def gradient(book: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
    """Per-factor sensitivities ∂K/∂s^CVA (d_cva) and ∂K/∂s^Hdg (d_hdg), in regulatory units."""
    return _run(book, params, want_grad=True)[1]


def directional(book: pd.DataFrame, trade: pd.DataFrame, params: dict[str, Any]) -> float:
    """Raw gradient dot product ∇K(book) · trade (trade factors absent from the book evaluated at zero).

    No fallback for a risk class with zero book capital (its gradient is 0 there); use
    :class:`sacva.marginal.MarginalEvaluator` / :func:`sacva.marginal.marginal` for the Euler marginal.
    """
    if trade.empty:
        return 0.0
    zero_trade = trade.assign(amount=0.0)
    g = gradient(pd.concat([book, zero_trade], ignore_index=True), params)
    t = aggregate(trade, params)[FACTOR_KEY + ["cva", "hdg"]]
    merged = t.merge(g[FACTOR_KEY + ["d_cva", "d_hdg"]], on=FACTOR_KEY, how="left")
    return float((merged["cva"] * merged["d_cva"] + merged["hdg"] * merged["d_hdg"]).sum())
