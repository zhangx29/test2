"""Effective risk-weight calibration grids.

Capital is reported per *user unit* (e.g. per $1 CR01, per $1 DV01, per $1 per 1% vol) and as an
*effective RW* = capital per regulatory unit, which is directly comparable with the regulatory risk
weight: a stand-alone unhedged position returns exactly its regulatory RW.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from . import riskclass
from .counterparty import calibrate_counterparties
from .engine import FACTOR_KEY, capital, gradient
from .loader import UNIT_SCALE, rows
from .marginal import MarginalEvaluator
from .policy import build_trade

NEW_NAME = "__NEW__"  # suffixed per cell so cells never share a synthetic factor
BASIS_REP = "representative new name (rho_name 50% vs book names)"


def _ccs_trade(name, bucket, quality, profile, unit, params, group=""):
    recs = [dict(source="CVA", risk_class="CCS", measure="delta", bucket=bucket, risk_factor=t, name=name,
                 quality=quality, group=group, amount=w, amount_unit=unit) for t, w in profile.items() if w]
    return rows(recs, params)


def ccs_representative_grid(book, params, policy_cfg, ev: MarginalEvaluator | None = None) -> pd.DataFrame:
    cal = policy_cfg["calibration"]
    c = cal["ccs"]
    unit, scale = c["unit"], UNIT_SCALE[c["unit"]]
    ev = ev or MarginalEvaluator(book, params, cal.get("trade_size", 1.0))
    cells = []
    for pol in policy_cfg["policies"]:
        for b in c["buckets"]:
            for q in c["qualities"]:
                cva = _ccs_trade(f"{NEW_NAME}{b}_{q}", b, q, c["tenor_profile"], unit, params)
                cells.append((pol, b, q, build_trade(cva, pol, policy_cfg, params)))
    ev.prepare([t for *_, t in cells])
    out = []
    for pol, b, q, trade in cells:
        r_ = ev.evaluate(trade)
        inc, eul, alone = r_.incremental, r_.euler, r_.standalone
        out.append(dict(policy=pol, risk_class="CCS", measure="delta", bucket=b, quality=q,
                        risk_factor="profile:" + ",".join(f"{t}={w:g}" for t, w in c["tenor_profile"].items()),
                        unit=unit, reg_rw=params["CCS"]["rw"][q][b],
                        incremental_per_unit=inc, euler_per_unit=eul, standalone_per_unit=alone,
                        incremental_eff_rw=inc / scale, euler_eff_rw=eul / scale, standalone_eff_rw=alone / scale,
                        basis=BASIS_REP))
    return pd.DataFrame(out)


def market_grid(book, params, policy_cfg, ev: MarginalEvaluator | None = None) -> pd.DataFrame:
    cal = policy_cfg["calibration"]
    ev = ev or MarginalEvaluator(book, params, cal.get("trade_size", 1.0))
    directions = cal.get("market_directions", {"delta": [1, -1], "vega": [1]})
    cells = []
    for pol in policy_cfg["policies"]:
        for f in cal["representative_factors"]:
            for d in directions[f["measure"]]:
                cva = rows([dict(source="CVA", risk_class=f["risk_class"], measure=f["measure"], bucket=str(f["bucket"]),
                                 risk_factor=str(f.get("risk_factor", "")), amount=float(d), amount_unit=f["unit"])], params)
                cells.append((pol, f, d, build_trade(cva, pol, policy_cfg, params)))
    ev.prepare([t for *_, t in cells])
    out = []
    for pol, f, d, trade in cells:
        r_ = ev.evaluate(trade)
        inc, eul, alone = r_.incremental, r_.euler, r_.standalone
        scale = UNIT_SCALE[f["unit"]]
        r = trade.iloc[0]
        out.append(dict(policy=pol, risk_class=f["risk_class"], measure=f["measure"], bucket=str(f["bucket"]),
                        quality="", risk_factor=str(f.get("risk_factor", "")), direction=d, unit=f["unit"],
                        reg_rw=riskclass.risk_weight(r["risk_class"], r["measure"], r["bucket"], r["risk_factor"], "", params),
                        incremental_per_unit=inc, euler_per_unit=eul, standalone_per_unit=alone,
                        incremental_eff_rw=inc / scale, euler_eff_rw=eul / scale, standalone_eff_rw=alone / scale,
                        basis="representative factor"))
    return pd.DataFrame(out)


def calibrate(book: pd.DataFrame, params: dict[str, Any], policy_cfg: dict[str, Any]) -> dict[str, Any]:
    """Full calibration: representative new-name CCS grid, representative market grid and
    counterparty-level RWs (per counterparty x risk type, clustered for the configured policies)."""
    ev = MarginalEvaluator(book, params, policy_cfg["calibration"].get("trade_size", 1.0))
    return {"ccs_grid": ccs_representative_grid(book, params, policy_cfg, ev),
            "market_grid": market_grid(book, params, policy_cfg, ev),
            "base_capital": capital(book, params).by_class,
            "euler": euler_contributions(book, params),
            "counterparty": calibrate_counterparties(book, params, policy_cfg)}


def euler_contributions(book: pd.DataFrame, params: dict[str, Any]) -> dict[str, pd.DataFrame]:
    """Euler risk allocation of K(book) on actual sensitivities; sums to K.

    ``by_counterparty``: CVA contributions s^CVA*dK/ds^CVA per counterparty and risk type, plus one
    macro-hedge row (s^Hdg*dK/ds^Hdg) and, if any, one unattributed-CVA row per risk type.
    ``by_factor``: the same per risk factor.
    """
    g = gradient(book, params)
    g["contribution"] = g["cva"] * g["d_cva"] + g["hdg"] * g["d_hdg"]
    by_factor = g[["risk_class", "measure", "bucket", "risk_factor", "name", "series", "quality",
                   "cva", "hdg", "d_cva", "d_hdg", "contribution"]]
    rows = book.merge(g[FACTOR_KEY + ["d_cva", "d_hdg"]], on=FACTOR_KEY, how="left")
    rows["contribution"] = rows["amount"] * np.where(rows["source"] == "CVA", rows["d_cva"], rows["d_hdg"])
    rows["counterparty"] = np.where(rows["source"] == "HDG", "HEDGES (macro)",
                                    np.where(rows["counterparty"] == "", "UNATTRIBUTED CVA", rows["counterparty"]))
    by_cpty = rows.groupby(["counterparty", "risk_class", "measure"], as_index=False, sort=True)["contribution"].sum()
    total = float(capital(book, params).total)
    check = pd.DataFrame([dict(counterparty="TOTAL: sum of contributions", risk_class="", measure="",
                               contribution=float(g["contribution"].sum())),
                          dict(counterparty="TOTAL: SA-CVA capital K", risk_class="", measure="", contribution=total)])
    return {"by_factor": by_factor, "by_counterparty": by_cpty, "check": check}
