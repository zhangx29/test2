"""Counterparty-level marginal / Euler risk weights and their clustering.

For each counterparty c (INTERNAL_ENTITY|COUNTERPARTY_ID) and risk type rt (risk class x measure):

* s_c      c's net CVA sensitivity per risk factor in rt; gross G_c = sum |s_c|
* trade    s_c scaled to a gross size of ``calibration.counterparty.trade_size`` user units of the risk type
           (default 1: $1/bp = 10,000 normalised for bp factors, $1/1% = 100 normalised for 1% factors) plus the
           hedge c's policy puts on it
* RW       capital change per $1 user-unit sensitivity: incremental = [K(book + trade) - K(book)] / trade_size;
           Euler = ∇K(book)·trade / trade_size; stand-alone. ``*_eff_rw`` = the same per normalised unit.

so RW x gross user-unit sensitivity approximates the capital of a new trade with c of the same profile.
Euler allocation of c at actual size: G_c x Euler RW (CVA sensitivities plus policy-implied hedge).
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .engine import FACTOR_KEY
from .loader import COLUMNS, UNIT_SCALE
from .marginal import MarginalEvaluator
from .policy import assign_policy, hedge_for

UNMAPPED = "UNMAPPED"
BP_TYPES = {("CCS", "delta"), ("IR", "delta"), ("RCS", "delta")}


def unit_of(rc: str, measure: str, policy_cfg: dict[str, Any]) -> str:
    """User unit of a risk type for counterparty RWs (per_bp for CCS/IR/RCS delta, per_1pct otherwise)."""
    units = policy_cfg["calibration"].get("counterparty", {}).get("units") or {}
    return units.get(f"{rc}.{measure}") or ("per_bp" if (rc, measure) in BP_TYPES else "per_1pct")


def _split(cpty: str) -> tuple[str, str]:
    ie, sep, cp = cpty.partition("|")
    return (ie, cp) if sep else ("", cpty)


def counterparty_table(book: pd.DataFrame, policy_cfg: dict[str, Any]) -> pd.DataFrame:
    """Counterparty attributes: sector / quality of its dominant CCS name (by |CR01|) and hedging policy."""
    cva = book[(book["source"] == "CVA") & (book["counterparty"] != "")]
    out = []
    for cpty, g in cva.groupby("counterparty", sort=True):
        ie, cp = _split(cpty)
        ccs = g[g["risk_class"] == "CCS"]
        if len(ccs):
            w = ccs.groupby(["name", "bucket", "quality"])["amount"].apply(lambda a: a.abs().sum())
            name, sector, quality = w.idxmax()
        else:
            name, sector, quality = "", UNMAPPED, ""
        explicit = next((p for p in g["policy"] if p), "")
        mapped = policy_cfg["assignment"].get("counterparties", {})
        key = cpty if cpty in mapped else cp
        out.append(dict(counterparty=cpty, internal_entity=ie, counterparty_id=cp, ccs_name=name, sector=sector,
                        quality=quality, policy=assign_policy(key, sector, explicit, policy_cfg)))
    return pd.DataFrame(out, columns=["counterparty", "internal_entity", "counterparty_id", "ccs_name", "sector",
                                      "quality", "policy"])


def counterparty_rw(book: pd.DataFrame, params: dict[str, Any], policy_cfg: dict[str, Any],
                    ev: MarginalEvaluator | None = None, attrs: pd.DataFrame | None = None,
                    risk_types: set[tuple[str, str]] | None = None,
                    ref_sizes: dict[tuple[str, str], float] | None = None) -> pd.DataFrame:
    """RWs per counterparty x risk type, each counterparty under attrs['policy']; ``risk_types`` limits the
    (risk_class, measure) cells priced (default: all). ``ref_sizes`` {(risk_class, measure): user units} adds
    ``ref_size`` and ``ref_incremental_rw``: the incremental RW of the same profile at that gross size (NaN for
    risk types not listed)."""
    size = float(policy_cfg["calibration"].get("counterparty", {}).get("trade_size", 1.0))
    if size <= 0:
        raise ValueError("calibration.counterparty.trade_size must be positive")
    attrs = (attrs if attrs is not None else counterparty_table(book, policy_cfg)).set_index("counterparty")
    cols = ["counterparty", "internal_entity", "counterparty_id", "policy", "sector", "quality", "risk_class",
            "measure", "unit", "trade_size", "n_factors", "gross_sens", "gross_sens_user",
            "incremental_rw", "euler_rw", "standalone_rw", "incremental_rw_reverse",
            "incremental_eff_rw", "euler_eff_rw", "standalone_eff_rw", "incremental_eff_rw_reverse",
            "euler_alloc", "cva_euler"]
    if ref_sizes is not None:
        cols += ["ref_size", "ref_incremental_rw"]
    cva = book[(book["source"] == "CVA") & (book["counterparty"] != "")]
    if risk_types is not None:
        cva = cva[[k in risk_types for k in zip(cva["risk_class"], cva["measure"])]]
    net = (cva.groupby(["counterparty"] + FACTOR_KEY, sort=True, dropna=False)
           .agg(quality=("quality", "first"), group=("group", "first"), amount=("amount", "sum")).reset_index())
    cell_cols = ["counterparty", "risk_class", "measure"]
    net["gross"] = net["amount"].abs().groupby([net[c] for c in cell_cols]).transform("sum")
    net = net[net["gross"] > 0].reset_index(drop=True)
    if net.empty:
        return pd.DataFrame(columns=cols)
    net["cell"] = net.groupby(cell_cols, sort=True).ngroup()
    net["cell_policy"] = net["counterparty"].map(attrs["policy"])
    net["unit"] = [unit_of(rc, m, policy_cfg) for rc, m in zip(net["risk_class"], net["measure"])]
    net["scale"] = net["unit"].map(UNIT_SCALE)
    # unit trades: own net profile scaled to trade_size user units (gross, normalised: size x scale); policy hedge
    # legs built in one pass per policy
    units = net.assign(source="CVA", amount=net["amount"] / net["gross"] * size * net["scale"], policy="")
    legs = []
    for pol, u in units.groupby("cell_policy"):
        legs.append(hedge_for(u[COLUMNS + ["cell"]], pol, policy_cfg, params))
    hedges = pd.concat(legs, ignore_index=True) if legs else units.iloc[:0][COLUMNS + ["cell"]]
    ev = ev or MarginalEvaluator(book, params, size=1.0)
    ev.prepare([units[COLUMNS], hedges[COLUMNS]])
    pos_u, pos_h = ev.positions(units), ev.positions(hedges)
    amt_u, amt_h = units["amount"].to_numpy(float), hedges["amount"].to_numpy(float)
    cva_e = net["amount"].to_numpy(float) * ev.d_cva[pos_u]
    u_idx = units.groupby("cell").indices
    h_idx = hedges.groupby("cell").indices if len(hedges) else {}
    first = units.groupby("cell").head(1).set_index("cell")
    rows = []
    for cell, iu in u_idx.items():
        f0 = first.loc[cell]
        ih = h_idx.get(cell, np.array([], dtype=np.int64))
        key = (f0["risk_class"], f0["measure"])
        inc, eul, alone = ev.evaluate_delta(key, pos_u[iu], amt_u[iu], pos_h[ih], amt_h[ih])
        # trade reducing the existing exposure (opposite profile and hedge); its Euler RW is exactly -euler_rw
        inc_rev = ev.evaluate_delta(key, pos_u[iu], -amt_u[iu], pos_h[ih], -amt_h[ih])[0]
        gross, scale = float(f0["gross"]), float(f0["scale"])
        size_n = size * scale                      # normalised gross size of the probe
        a = attrs.loc[f0["counterparty"]]
        ref = {}
        if ref_sizes is not None:
            x = float(ref_sizes.get(key, np.nan))
            ref = dict(ref_size=x, ref_incremental_rw=np.nan if np.isnan(x) else ev.evaluate_delta(
                key, pos_u[iu], amt_u[iu] * x / size, pos_h[ih], amt_h[ih] * x / size)[0] / x)
        rows.append(dict(counterparty=f0["counterparty"], internal_entity=a["internal_entity"],
                         counterparty_id=a["counterparty_id"], policy=a["policy"], sector=a["sector"],
                         quality=a["quality"], risk_class=f0["risk_class"], measure=f0["measure"], unit=f0["unit"],
                         trade_size=size, n_factors=len(iu), gross_sens=gross, gross_sens_user=gross / scale,
                         incremental_rw=inc / size, euler_rw=eul / size, standalone_rw=alone / size,
                         incremental_rw_reverse=inc_rev / size,
                         incremental_eff_rw=inc / size_n, euler_eff_rw=eul / size_n, standalone_eff_rw=alone / size_n,
                         incremental_eff_rw_reverse=inc_rev / size_n,
                         euler_alloc=eul / size_n * gross, cva_euler=float(cva_e[iu].sum()), **ref))
    return pd.DataFrame(rows, columns=cols)


def cluster_rw(cpty_rw: pd.DataFrame, policies: list[str]) -> pd.DataFrame:
    """Gross-sensitivity-weighted RW per policy x sector x quality x risk type (selected policies only).

    Cells of one risk type share a unit, so weighting by normalised or user-unit gross sensitivity is the same."""
    d = cpty_rw[cpty_rw["policy"].isin(policies)]
    rws = ["incremental_rw", "euler_rw", "standalone_rw", "incremental_rw_reverse",
           "incremental_eff_rw", "euler_eff_rw", "standalone_eff_rw", "incremental_eff_rw_reverse"]
    cols = ["policy", "sector", "quality", "risk_class", "measure", "unit", "n_cpty", "gross_sens"] + rws
    if d.empty:
        return pd.DataFrame(columns=cols)
    w = d.assign(**{f"_{c}": d[c] * d["gross_sens"] for c in rws})
    g = w.groupby(["policy", "sector", "quality", "risk_class", "measure"], sort=True).agg(
        unit=("unit", "first"), n_cpty=("counterparty", "nunique"), gross_sens=("gross_sens", "sum"),
        **{f"_{c}": (f"_{c}", "sum") for c in rws}).reset_index()
    for c in rws:
        g[c] = g[f"_{c}"] / g["gross_sens"]
    return g[cols]


def euler_reconciliation(ev: MarginalEvaluator, cpty_rw: pd.DataFrame) -> pd.DataFrame:
    """Per risk type: K = CVA Euler (counterparties + unattributed) + macro-hedge Euler.

    The macro-hedge Euler is split into the part implied by counterparty hedging policies (included in the
    counterparty Euler allocations) and the unallocated remainder (hedges beyond / short of the policies).
    """
    rows = []
    for (rc, measure), (_, idx) in ev.models.items():
        cva_e = float(ev.cva[idx] @ ev.d_cva[idx])
        hdg_e = float(ev.hdg[idx] @ ev.d_hdg[idx])
        sel = cpty_rw[(cpty_rw["risk_class"] == rc) & (cpty_rw["measure"] == measure)]
        cpty_cva = float(sel["cva_euler"].sum())
        policy_hdg = float((sel["euler_alloc"] - sel["cva_euler"]).sum())
        k = ev.base_k[(rc, measure)]
        rows.append(dict(risk_class=rc, measure=measure, K_RT=k, cpty_cva_euler=cpty_cva,
                         unattributed_cva_euler=cva_e - cpty_cva, hedge_euler=hdg_e,
                         policy_hedge_allocated=policy_hdg, unallocated_hedge=hdg_e - policy_hdg,
                         cpty_euler_alloc=float(sel["euler_alloc"].sum()), check=cva_e + hdg_e - k))
    return pd.DataFrame(rows)


def calibrate_counterparties(book: pd.DataFrame, params: dict[str, Any], policy_cfg: dict[str, Any],
                             ev: MarginalEvaluator | None = None) -> dict[str, pd.DataFrame]:
    ev = ev or MarginalEvaluator(book, params, size=1.0)
    attrs = counterparty_table(book, policy_cfg)
    rw = counterparty_rw(book, params, policy_cfg, ev, attrs)
    policies = policy_cfg["calibration"].get("counterparty", {}).get("cluster_policies", ["sector_hedge"])
    return {"cpty_rw": rw, "cpty_cluster": cluster_rw(rw, policies), "euler_recon": euler_reconciliation(ev, rw),
            "cpty_table": attrs}
