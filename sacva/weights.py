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

``sacva_weights.method`` switches the pricing methodology:

* ``current``: one number per cell, the cell-average ``rw`` (per $1), as above.
* ``size_term``: Euler base plus a size term (:mod:`sacva.sizeterm`). Per cell the gross-weighted e (``rw``: the $1
  incremental or the Euler RW), s (stand-alone) and i_ref (incremental at ``ref_size``) give K_eff. The file adds
  eulerRw, standaloneRw, refRw, kEff, refSize and their vega* equivalents, so a trade of size x is priced at RW(x)
  (``sacva price``). Tenor columns and vegaCost carry RW(ref_size) = refRw.
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
from .sizeterm import k_eff

HEADER = ["cptyCode", "hedgeType", "bucket", "igHy", "vegaCost"]
TYPES = ["string", "string", "object", "string", "double?"]
NA = "NA"
BUCKET_OUT = {"1a": "1_A", "1b": "1_B"}
METHODS = ("current", "size_term")
SIZE_OUT = ["eulerRw", "standaloneRw", "refRw", "kEff", "refSize"]          # CCS; vega: vegaEulerRw, ...
SIZE_SRC = ["e", "s", "i_ref", "k_eff", "ref_size"]


def _wavg(df: pd.DataFrame, keys: list[str], rws: dict[str, str]) -> pd.DataFrame:
    """Gross-CR01-weighted average per ``keys`` of each column ``rws[out]``, returned as column ``out``."""
    cols = keys + list(rws) + ["n_cpty", "gross"]
    if df.empty:
        return pd.DataFrame(columns=cols)
    w = df.assign(**{f"_{o}": df[c] * df["gross_sens_user"] for o, c in rws.items()})
    g = w.groupby(keys, sort=True).agg(gross=("gross_sens_user", "sum"), n_cpty=("counterparty", "nunique"),
                                       **{f"_{o}": (f"_{o}", "sum") for o in rws}).reset_index()
    for o in rws:
        g[o] = g[f"_{o}"] / g["gross"]
    return g[cols]


def sacva_weights(book: pd.DataFrame, params: dict[str, Any], policy_cfg: dict[str, Any],
                  ev: MarginalEvaluator | None = None, method: str | None = None) -> dict[str, Any]:
    """{'weights': rows of SACVAWeights.csv, 'detail': the same with sources and weights, 'cpty_rw': inputs,
    'method': methodology used}. ``method`` overrides ``sacva_weights.method`` (current | size_term)."""
    cfg = policy_cfg["sacva_weights"]
    hedge_types: dict[str, str] = dict(cfg["hedge_types"])
    label_of = {p: lbl for lbl, p in hedge_types.items()}
    for pol in list(hedge_types.values()) + list((cfg.get("own_lines") or {}).values()):
        if pol not in policy_cfg["policies"]:
            raise ValueError(f"sacva_weights: unknown policy '{pol}'")
        if pol not in label_of:
            raise ValueError(f"sacva_weights.own_lines: policy '{pol}' is not one of hedge_types")
    rw = cfg.get("rw", "incremental_rw")
    method = method or cfg.get("method", "current")
    if method not in METHODS:
        raise ValueError(f"sacva_weights.method must be one of {METHODS}, got '{method}'")
    sized = method == "size_term"
    if sized and rw not in ("incremental_rw", "euler_rw"):
        raise ValueError("sacva_weights.method size_term needs rw = incremental_rw or euler_rw (the base e)")
    ref = {"ccs": 10000.0, "vega": 10000.0} | {k: float(v) for k, v in (cfg.get("ref_size") or {}).items()}
    tenors = [str(t) for t in cfg["tenors"]]
    ev = ev or MarginalEvaluator(book, params, size=1.0)
    attrs = counterparty_table(book, policy_cfg)
    types = {("CCS", "delta")} | {(rc, "vega") for rc in RISK_CLASSES}
    ref_sizes = {t: ref["ccs"] if t[1] == "delta" else ref["vega"] for t in types} if sized else None
    runs = [counterparty_rw(book, params, policy_cfg, ev, attrs.assign(policy=pol), types, ref_sizes)
            .assign(hedge_type=lbl) for lbl, pol in hedge_types.items()]
    r = pd.concat(runs, ignore_index=True)
    ccs = r[(r["risk_class"] == "CCS") & (r["measure"] == "delta")]
    vega = r[r["measure"] == "vega"]
    # columns averaged per cell: the published rw and, for size_term, e / s / i_ref
    rws = {"rw": rw} | ({"e": rw, "s": "standalone_rw", "i_ref": "ref_incremental_rw"} if sized else {})
    # K_eff fallback for a cell without curvature: class-level book capital (vega: summed over the vega classes)
    k_class = {"ccs": ev.base_k.get(("CCS", "delta"), 0.0),
               "vega": sum(k for (_, m), k in ev.base_k.items() if m == "vega")}

    keys = ["hedge_type", "sector", "quality"]
    ccs_cell = _wavg(ccs[ccs["sector"] != UNMAPPED], keys, rws).set_index(keys)
    vega_cell = _wavg(vega[vega["sector"] != UNMAPPED], keys, rws).set_index(keys)
    vega_all = _wavg(vega, ["hedge_type"], rws).set_index("hedge_type")
    grid_cfg = copy.deepcopy(policy_cfg)
    grid_cfg["policies"] = {p: policy_cfg["policies"][p] for p in hedge_types.values()}
    grid_cfg["calibration"]["ccs"]["buckets"] = list(cfg["buckets"])
    grid_cfg["calibration"]["ccs"]["qualities"] = list(cfg["qualities"])
    grid = ccs_representative_grid(book, params, grid_cfg, ev, ref["ccs"] if sized else None
                                   ).set_index(["policy", "bucket", "quality"])
    grid_rw = rw if rw in grid.columns else "incremental_rw"
    grid_cols = {"rw": grid_rw} | ({"e": grid_rw, "s": "standalone_rw", "i_ref": "ref_incremental_rw"} if sized else {})

    def vals(src, kind: str) -> dict[str, float]:
        """Published rw (size_term: RW at ref_size) plus e, s, i_ref, k_eff, ref_size of one averaged cell."""
        out = {o: float(src[o]) for o in rws}
        if sized:
            out["rw"] = out["i_ref"]
            out["k_eff"] = k_eff(out["e"], out["s"], out["i_ref"], ref[kind], k_class[kind])
            out["ref_size"] = ref[kind]
        return out

    nan = dict.fromkeys(list(rws) + (["k_eff", "ref_size"] if sized else []), np.nan)

    def vega_fallback(lbl: str) -> tuple[dict[str, float], str]:
        if lbl in vega_all.index:
            return vals(vega_all.loc[lbl], "vega"), "all counterparties (no vega in cell)"
        return nan, "none"

    def values(c: dict[str, float], v: dict[str, float]) -> dict[str, float]:
        out = dict(ccs_rw=c["rw"], vegaCost=v["rw"])
        if sized:
            out |= {f"ccs_{o}": c[o] for o in SIZE_SRC} | {f"vega_{o}": v[o] for o in SIZE_SRC}
        return out

    detail = []
    for lbl, pol in hedge_types.items():
        for q in cfg["qualities"]:
            for b in cfg["buckets"]:
                k = (lbl, str(b), q)
                if k in ccs_cell.index:
                    c_ = ccs_cell.loc[k]
                    c, c_n, c_g, c_src = vals(c_, "ccs"), int(c_["n_cpty"]), float(c_["gross"]), "counterparties"
                else:
                    g = grid.loc[(pol, str(b), q)]
                    c = vals({o: g[col] for o, col in grid_cols.items()}, "ccs")
                    c_n, c_g, c_src = 0, 0.0, "new-name grid"
                if k in vega_cell.index:
                    v_ = vega_cell.loc[k]
                    v, v_n, v_g, v_src = vals(v_, "vega"), int(v_["n_cpty"]), float(v_["gross"]), "counterparties"
                else:
                    (v, v_src), v_n, v_g = vega_fallback(lbl), 0, 0.0
                detail.append(dict(cptyCode=NA, hedgeType=lbl, bucket=BUCKET_OUT.get(str(b), str(b)), igHy=q,
                                   policy=pol, **values(c, v), ccs_source=c_src, ccs_n_cpty=c_n,
                                   ccs_gross_cr01=c_g, vega_source=v_src, vega_n_cpty=v_n, vega_gross=v_g))
    for cp, pol in (cfg.get("own_lines") or {}).items():
        lbl = label_of[pol]
        cp = str(cp)
        c_ = _wavg(ccs[(ccs["counterparty_id"] == cp) & (ccs["hedge_type"] == lbl)], ["hedge_type"], rws)
        v_ = _wavg(vega[(vega["counterparty_id"] == cp) & (vega["hedge_type"] == lbl)], ["hedge_type"], rws)
        found = cp in set(attrs["counterparty_id"])
        c = vals(c_.iloc[0], "ccs") if len(c_) else nan
        if len(v_):
            v, v_src, v_g = vals(v_.iloc[0], "vega"), "counterparty", float(v_["gross"].iloc[0])
        else:
            (v, v_src), v_g = vega_fallback(lbl), 0.0
        detail.append(dict(cptyCode=cp, hedgeType=NA, bucket=NA, igHy=NA, policy=pol, **values(c, v),
                           ccs_source=("counterparty" if len(c_) else "none: no CCS sensitivity") if found
                           else "none: COUNTERPARTY_ID not in book",
                           ccs_n_cpty=int(len(c_) > 0), ccs_gross_cr01=float(c_["gross"].iloc[0]) if len(c_) else 0.0,
                           vega_source=v_src if found else "none", vega_n_cpty=int(len(v_) > 0), vega_gross=v_g))
    detail = pd.DataFrame(detail)
    weights = detail[HEADER].copy()
    for t in tenors:
        weights[t] = detail["ccs_rw"]
    if sized:
        for o, s in zip(SIZE_OUT, SIZE_SRC):
            weights[o] = detail[f"ccs_{s}"]
        for o, s in zip(SIZE_OUT, SIZE_SRC):
            weights["vega" + o[0].upper() + o[1:]] = detail[f"vega_{s}"]
    return {"weights": weights, "detail": detail, "cpty_rw": r, "method": method}


def _fmt(v: Any) -> str:
    if isinstance(v, (float, np.floating)):
        return "" if np.isnan(v) else f"{float(v):.10g}"
    return str(v)


def write_weights(weights: pd.DataFrame, path: str | Path) -> Path:
    """Production layout: column header, then a type row (string, ..., double?), then the data rows. Columns after
    the header block (tenors and, for size_term, the size-term columns) are all double?."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tenors = list(weights.columns[len(HEADER):])
    lines = [",".join(HEADER + tenors), ",".join(TYPES + ["double?"] * len(tenors))]
    lines += [",".join(_fmt(v) for v in row) for row in weights.itertuples(index=False)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def read_weights(path: str | Path) -> pd.DataFrame:
    """SACVAWeights.csv back into a frame (skips the type row)."""
    w = pd.read_csv(path, dtype=str, keep_default_na=False, skiprows=[1])
    for c in w.columns[len(HEADER) - 1:]:
        w[c] = pd.to_numeric(w[c].replace("", np.nan))
    return w
