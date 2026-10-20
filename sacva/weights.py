"""SACVAWeights.csv: CCS and vega pricing weights per hedge type x sector x IG/HY, plus per-name lines.

For every counterparty and every hedge type (production label -> hedging policy, ``sacva_weights.hedge_types``)
the counterparty's RW (default incremental, per $1 user unit) is computed for its CCS delta and each vega risk type
as if the counterparty were under that policy. Then per hedge type x sector x IG/HY (sector / quality of the
counterparty's dominant CCS name):

* tenor columns (6m .. 10y, all equal) = sum(RW x gross CR01) / sum(gross CR01) over CCS delta, per $1/bp CR01
* vegaCost = the same over all vega risk types (IR, FX, EQ, CO, RCS), per $1 per 1% vega

Counterparties in ``sacva_weights.own_lines`` also get their own line (cptyCode = COUNTERPARTY_ID, across
internal entities) under their configured policy. Empty sector cells fall back to the representative new-name
CCS grid and to the hedge type's portfolio-wide vegaCost; ``detail`` records the source of every value.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .calibrate import ccs_representative_grid
from .counterparty import UNMAPPED, counterparty_rw, counterparty_table
from .marginal import MarginalEvaluator
from .riskclass import RISK_CLASSES

HEADER = ["cptyCode", "hedgeType", "bucket", "igHy", "vegaCost"]
TYPES = ["string", "string", "object", "string", "double?"]
NA = "NA"
BUCKET_OUT = {"1a": "1_A", "1b": "1_B"}


def _wavg(df: pd.DataFrame, keys: list[str], rw: str) -> pd.DataFrame:
    cols = keys + ["rw", "n_cpty", "gross"]
    if df.empty:
        return pd.DataFrame(columns=cols)
    w = df.assign(_w=df[rw] * df["gross_sens_user"])
    g = w.groupby(keys, sort=True).agg(_w=("_w", "sum"), gross=("gross_sens_user", "sum"),
                                       n_cpty=("counterparty", "nunique")).reset_index()
    g["rw"] = g["_w"] / g["gross"]
    return g[cols]


def sacva_weights(book: pd.DataFrame, params: dict[str, Any], policy_cfg: dict[str, Any],
                  ev: MarginalEvaluator | None = None) -> dict[str, pd.DataFrame]:
    """{'weights': rows of SACVAWeights.csv, 'detail': the same with sources and weights, 'cpty_rw': inputs}."""
    cfg = policy_cfg["sacva_weights"]
    hedge_types: dict[str, str] = dict(cfg["hedge_types"])
    label_of = {p: lbl for lbl, p in hedge_types.items()}
    for pol in list(hedge_types.values()) + list((cfg.get("own_lines") or {}).values()):
        if pol not in policy_cfg["policies"]:
            raise ValueError(f"sacva_weights: unknown policy '{pol}'")
        if pol not in label_of:
            raise ValueError(f"sacva_weights.own_lines: policy '{pol}' is not one of hedge_types")
    rw = cfg.get("rw", "incremental_rw")
    tenors = [str(t) for t in cfg["tenors"]]
    ev = ev or MarginalEvaluator(book, params, size=1.0)
    attrs = counterparty_table(book, policy_cfg)
    types = {("CCS", "delta")} | {(rc, "vega") for rc in RISK_CLASSES}
    runs = [counterparty_rw(book, params, policy_cfg, ev, attrs.assign(policy=pol), types).assign(hedge_type=lbl)
            for lbl, pol in hedge_types.items()]
    r = pd.concat(runs, ignore_index=True)
    ccs = r[(r["risk_class"] == "CCS") & (r["measure"] == "delta")]
    vega = r[r["measure"] == "vega"]

    keys = ["hedge_type", "sector", "quality"]
    ccs_cell = _wavg(ccs[ccs["sector"] != UNMAPPED], keys, rw).set_index(keys)
    vega_cell = _wavg(vega[vega["sector"] != UNMAPPED], keys, rw).set_index(keys)
    vega_all = _wavg(vega, ["hedge_type"], rw).set_index("hedge_type")
    grid_cfg = copy.deepcopy(policy_cfg)
    grid_cfg["policies"] = {p: policy_cfg["policies"][p] for p in hedge_types.values()}
    grid_cfg["calibration"]["ccs"]["buckets"] = list(cfg["buckets"])
    grid_cfg["calibration"]["ccs"]["qualities"] = list(cfg["qualities"])
    grid = ccs_representative_grid(book, params, grid_cfg, ev).set_index(["policy", "bucket", "quality"])
    grid_rw = rw if rw in grid.columns else "incremental_rw"

    def vega_fallback(lbl: str) -> tuple[float, str]:
        if lbl in vega_all.index:
            return float(vega_all.loc[lbl, "rw"]), "all counterparties (no vega in cell)"
        return np.nan, "none"

    detail = []
    for lbl, pol in hedge_types.items():
        for q in cfg["qualities"]:
            for b in cfg["buckets"]:
                k = (lbl, str(b), q)
                if k in ccs_cell.index:
                    c = ccs_cell.loc[k]
                    c_rw, c_n, c_g, c_src = float(c["rw"]), int(c["n_cpty"]), float(c["gross"]), "counterparties"
                else:
                    c_rw, c_n, c_g, c_src = float(grid.loc[(pol, str(b), q), grid_rw]), 0, 0.0, "new-name grid"
                if k in vega_cell.index:
                    v = vega_cell.loc[k]
                    v_rw, v_n, v_g, v_src = float(v["rw"]), int(v["n_cpty"]), float(v["gross"]), "counterparties"
                else:
                    (v_rw, v_src), v_n, v_g = vega_fallback(lbl), 0, 0.0
                detail.append(dict(cptyCode=NA, hedgeType=lbl, bucket=BUCKET_OUT.get(str(b), str(b)), igHy=q,
                                   policy=pol, ccs_rw=c_rw, vegaCost=v_rw, ccs_source=c_src, ccs_n_cpty=c_n,
                                   ccs_gross_cr01=c_g, vega_source=v_src, vega_n_cpty=v_n, vega_gross=v_g))
    for cp, pol in (cfg.get("own_lines") or {}).items():
        lbl = label_of[pol]
        cp = str(cp)
        c = _wavg(ccs[(ccs["counterparty_id"] == cp) & (ccs["hedge_type"] == lbl)], ["hedge_type"], rw)
        v = _wavg(vega[(vega["counterparty_id"] == cp) & (vega["hedge_type"] == lbl)], ["hedge_type"], rw)
        found = cp in set(attrs["counterparty_id"])
        if len(v):
            v_rw, v_src, v_g = float(v["rw"].iloc[0]), "counterparty", float(v["gross"].iloc[0])
        else:
            (v_rw, v_src), v_g = vega_fallback(lbl), 0.0
        detail.append(dict(cptyCode=cp, hedgeType=NA, bucket=NA, igHy=NA, policy=pol,
                           ccs_rw=float(c["rw"].iloc[0]) if len(c) else np.nan, vegaCost=v_rw,
                           ccs_source=("counterparty" if len(c) else "none: no CCS sensitivity") if found
                           else "none: COUNTERPARTY_ID not in book",
                           ccs_n_cpty=int(len(c) > 0), ccs_gross_cr01=float(c["gross"].iloc[0]) if len(c) else 0.0,
                           vega_source=v_src if found else "none", vega_n_cpty=int(len(v) > 0), vega_gross=v_g))
    detail = pd.DataFrame(detail)
    weights = detail[HEADER].copy()
    for t in tenors:
        weights[t] = detail["ccs_rw"]
    return {"weights": weights, "detail": detail, "cpty_rw": r}


def _fmt(v: Any) -> str:
    if isinstance(v, (float, np.floating)):
        return "" if np.isnan(v) else f"{float(v):.10g}"
    return str(v)


def write_weights(weights: pd.DataFrame, path: str | Path) -> Path:
    """Production layout: column header, then a type row (string, ..., double?), then the data rows."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tenors = list(weights.columns[len(HEADER):])
    lines = [",".join(HEADER + tenors), ",".join(TYPES + ["double?"] * len(tenors))]
    lines += [",".join(_fmt(v) for v in row) for row in weights.itertuples(index=False)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
