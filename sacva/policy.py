"""Hedging policies: assign a policy to a counterparty and generate the policy hedge for a trade."""
from __future__ import annotations

from typing import Any

import pandas as pd

from . import riskclass


def assign_policy(name: str, bucket: str, explicit: str, policy_cfg: dict[str, Any]) -> str:
    a = policy_cfg["assignment"]
    for candidate in (explicit, a.get("counterparties", {}).get(name), a.get("ccs_buckets", {}).get(bucket), a["default"]):
        if candidate:
            if candidate not in policy_cfg["policies"]:
                raise ValueError(f"unknown hedging policy '{candidate}' for {name}")
            return candidate
    raise ValueError("assignment.default is required")


def rule(policy: str, rc: str, measure: str, policy_cfg: dict[str, Any]) -> dict[str, Any]:
    pol = policy_cfg["policies"][policy] or {}
    return pol.get(f"{rc}.{measure}") or {"instrument": "same_factor", "ratio": 0.0}


def hedge_for(cva: pd.DataFrame, policy: str, policy_cfg: dict[str, Any], params: dict[str, Any]) -> pd.DataFrame:
    """Hedge rows (source HDG, normalised regulatory units) that ``policy`` puts on against CVA rows ``cva``."""
    out = []
    for r in cva.itertuples(index=False):
        rl = rule(policy, r.risk_class, r.measure, policy_cfg)
        ratio = float(rl.get("ratio", 0.0))
        if ratio == 0.0 or r.amount == 0.0:
            continue
        h = r._asdict()
        h.update(source="HDG", policy="")
        if "counterparty" in h:
            h["counterparty"] = ""   # hedges are not attributed to a counterparty
        inst = rl.get("instrument", "same_factor")
        if r.risk_class != "CCS":
            if inst != "same_factor":
                raise ValueError(f"instrument '{inst}' not supported for {r.risk_class}")
        elif inst == "single_name":
            pass
        elif inst == "sector_proxy":
            h.update(name=f"PROXY:{r.bucket}:{r.quality}", group="")
        elif inst == "qualified_index":
            idx = rl["index"][r.quality]
            h.update(bucket="8", name=idx["name"], series=str(idx.get("series", "")), group="")
        else:
            raise ValueError(f"unknown CCS hedge instrument '{inst}'")
        beta = 1.0
        if rl.get("sizing", "cr01") == "beta":
            rw_cpty = riskclass.risk_weight(r.risk_class, r.measure, r.bucket, r.risk_factor, r.quality, params)
            rw_hdg = riskclass.risk_weight(h["risk_class"], h["measure"], h["bucket"], h["risk_factor"], h["quality"], params)
            beta = rw_cpty / rw_hdg
        h["amount"] = ratio * beta * r.amount
        out.append(h)
    return pd.DataFrame(out, columns=cva.columns)


def build_trade(cva: pd.DataFrame, policy: str, policy_cfg: dict[str, Any], params: dict[str, Any]) -> pd.DataFrame:
    """CVA rows plus the policy hedge."""
    hdg = hedge_for(cva, policy, policy_cfg, params)
    return pd.concat([cva, hdg], ignore_index=True) if len(hdg) else cva.reset_index(drop=True)
