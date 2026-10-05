"""Counterparty-level marginal / Euler risk weights and their clustering.

For each counterparty c (INTERNAL_ENTITY|COUNTERPARTY_ID) and risk type rt (risk class x measure):

* s_c      c's net CVA sensitivity per risk factor in rt; gross G_c = sum |s_c|
* trade    s_c scaled to a gross size of ``calibration.counterparty.trade_size`` (default 10,000 normalised,
           i.e. $1/bp for bp factors) plus the hedge c's policy puts on it
* RW       incremental = [K(book + trade) - K(book)] / size; Euler = ∇K(book)·trade / size; stand-alone

so RW x gross normalised sensitivity approximates the capital of a new trade with c of the same profile.
Euler allocation of c at actual size: G_c x Euler RW (CVA sensitivities plus policy-implied hedge).
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .engine import FACTOR_KEY
from .loader import COLUMNS
from .marginal import MarginalEvaluator
from .policy import assign_policy, hedge_for

UNMAPPED = "UNMAPPED"


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
                    ev: MarginalEvaluator | None = None, attrs: pd.DataFrame | None = None) -> pd.DataFrame:
    size = float(policy_cfg["calibration"].get("counterparty", {}).get("trade_size", 1e4))
    attrs = (attrs if attrs is not None else counterparty_table(book, policy_cfg)).set_index("counterparty")
    cols = ["counterparty", "internal_entity", "counterparty_id", "policy", "sector", "quality", "risk_class",
            "measure", "n_factors", "gross_sens", "incremental_rw", "euler_rw", "standalone_rw",
            "incremental_rw_reverse", "euler_alloc", "cva_euler"]
    cva = book[(book["source"] == "CVA") & (book["counterparty"] != "")]
    net = (cva.groupby(["counterparty"] + FACTOR_KEY, sort=True, dropna=False)
           .agg(quality=("quality", "first"), group=("group", "first"), amount=("amount", "sum")).reset_index())
    cell_cols = ["counterparty", "risk_class", "measure"]
    net["gross"] = net["amount"].abs().groupby([net[c] for c in cell_cols]).transform("sum")
    net = net[net["gross"] > 0].reset_index(drop=True)
    if net.empty:
        return pd.DataFrame(columns=cols)
    net["cell"] = net.groupby(cell_cols, sort=True).ngroup()
    net["cell_policy"] = net["counterparty"].map(attrs["policy"])
    # unit trades: own net profile scaled to the gross size; policy hedge legs built in one pass per policy
    units = net.assign(source="CVA", amount=net["amount"] / net["gross"] * size, policy="")
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
        gross = float(f0["gross"])
        a = attrs.loc[f0["counterparty"]]
        rows.append(dict(counterparty=f0["counterparty"], internal_entity=a["internal_entity"],
                         counterparty_id=a["counterparty_id"], policy=a["policy"], sector=a["sector"],
                         quality=a["quality"], risk_class=f0["risk_class"], measure=f0["measure"], n_factors=len(iu),
                         gross_sens=gross, incremental_rw=inc / size, euler_rw=eul / size, standalone_rw=alone / size,
                         incremental_rw_reverse=inc_rev / size,
                         euler_alloc=eul / size * gross, cva_euler=float(cva_e[iu].sum())))
    return pd.DataFrame(rows, columns=cols)


def cluster_rw(cpty_rw: pd.DataFrame, policies: list[str]) -> pd.DataFrame:
    """Gross-sensitivity-weighted RW per policy x sector x quality x risk type (selected policies only)."""
    d = cpty_rw[cpty_rw["policy"].isin(policies)]
    rws = ["incremental_rw", "euler_rw", "standalone_rw", "incremental_rw_reverse"]
    cols = ["policy", "sector", "quality", "risk_class", "measure", "n_cpty", "gross_sens"] + rws
    if d.empty:
        return pd.DataFrame(columns=cols)
    w = d.assign(**{f"_{c}": d[c] * d["gross_sens"] for c in rws})
    g = w.groupby(["policy", "sector", "quality", "risk_class", "measure"], sort=True).agg(
        n_cpty=("counterparty", "nunique"), gross_sens=("gross_sens", "sum"),
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
