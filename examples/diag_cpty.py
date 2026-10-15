"""Diagnose incremental vs Euler RW for selected counterparties (CCS delta by default).

    python examples/diag_cpty.py --book <CVA_CALC_RF file> --cpty "BMOIRELAND|HINS" "BMOIRELAND|RENTBK"

For each counterparty prints: its CCS factors, the policy hedge legs, incremental RW at several trade
sizes (normalised units) in both directions, the Euler RW (all per normalised unit, i.e. the CPTY_RW
*_eff_rw columns; x 10,000 for bp factors or x 100 for 1% factors gives the per-$1 *_rw), a per-factor finite-difference check of the gradient, and
per-bucket K_b / sum WS / S_b before and after the trade (clipping regime of S_b).
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from sacva.counterparty import counterparty_table
from sacva.cva_calc import read_input
from sacva.engine import FACTOR_KEY
from sacva.loader import COLUMNS
from sacva.marginal import MarginalEvaluator
from sacva.params import load_params, load_policy
from sacva.policy import hedge_for


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True)
    ap.add_argument("--sheet")
    ap.add_argument("--cpty", nargs="+", required=True)
    ap.add_argument("--risk-class", default="CCS")
    ap.add_argument("--measure", default="delta")
    ap.add_argument("--params", help="regulatory params YAML used for the run (default: bundled)")
    ap.add_argument("--policy-config", help="hedging policy YAML used for the run (default: bundled)")
    a = ap.parse_args()
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    params, pcfg = load_params(a.params), load_policy(a.policy_config)
    book = read_input(a.book, params, a.sheet).book
    attrs = counterparty_table(book, pcfg).set_index("counterparty")
    key = (a.risk_class, a.measure)
    for cp in a.cpty:
        print("=" * 100, f"\n{cp}  policy={attrs.loc[cp, 'policy']}  sector={attrs.loc[cp, 'sector']}")
        rows = book[(book["counterparty"] == cp) & (book["source"] == "CVA")
                    & (book["risk_class"] == a.risk_class) & (book["measure"] == a.measure)]
        net = rows.groupby(FACTOR_KEY, dropna=False).agg(quality=("quality", "first"), group=("group", "first"),
                                                          amount=("amount", "sum")).reset_index()
        gross = net["amount"].abs().sum()
        unit = net.assign(source="CVA", policy="", counterparty="", amount=net["amount"] / gross)
        for c in COLUMNS:
            if c not in unit:
                unit[c] = ""
        hdg = hedge_for(unit[COLUMNS], attrs.loc[cp, "policy"], pcfg, params)
        print("\nnet CVA factors (unit gross):\n", unit[FACTOR_KEY + ["quality", "group", "amount"]].to_string(index=False))
        print("\npolicy hedge legs:\n", hdg[FACTOR_KEY + ["quality", "amount"]].to_string(index=False) if len(hdg) else " none")

        ev = MarginalEvaluator(book, params, size=1.0)
        ev.prepare([unit[COLUMNS], hdg[COLUMNS]])
        pu, ph = ev.positions(unit), ev.positions(hdg)
        au, ah = unit["amount"].to_numpy(float), hdg["amount"].to_numpy(float)
        print("\nsize        inc_eff_rw(+)  inc_eff_rw(-)  euler_eff_rw")
        for s in (1.0, 1e2, 1e4, 1e6):
            ip = ev.evaluate_delta(key, pu, au * s, ph, ah * s)
            im = ev.evaluate_delta(key, pu, -au * s, ph, -ah * s)
            print(f"{s:>8.0e}  {ip[0] / s:>13.6e}  {im[0] / s:>13.6e}  {ip[1] / s:>13.6e}")

        model, idx = ev.models[key]
        base = ev.base_k[key]
        names = set(unit["name"]) - {""}
        if names:
            fb = ev.f.loc[idx]
            grp = set(fb.loc[fb["name"].isin(names), "group"])
            rel = fb[fb["group"].isin(grp) | fb["name"].isin(names)]
            print("\nbook factors sharing a name / group with this counterparty:\n",
                  rel[FACTOR_KEY + ["quality", "group", "cva", "hdg", "rw", "agg_bucket"]].to_string(index=False))
        rng = np.random.default_rng(0)
        touched = set(model.buckets[i] for i, pos in enumerate(model.pos)
                      if np.isin(idx[pos], np.concatenate([pu, ph])).any())
        for i, b in enumerate(model.buckets):
            if b in touched:
                x, y = rng.standard_normal((2, len(model.pos[i])))
                print(f"rho symmetry bucket {b}: x.rho(y) - y.rho(x) = {x @ model.rho[i](y) - y @ model.rho[i](x):.3e}")
        print("\nper-factor gradient check (central difference, h = 1 normalised unit):")
        out = []
        for side, pos in (("CVA", pu), ("HDG", ph)):
            for p in np.unique(pos):
                loc = ev.local[p]
                g = ev.d_cva[p] if side == "CVA" else ev.d_hdg[p]
                e = np.zeros(len(idx)); e[loc] = 1.0
                c0, h0 = ev.cva[idx], ev.hdg[idx]
                if side == "CVA":
                    fd = (model.evaluate(c0 + e, h0)[0] - model.evaluate(c0 - e, h0)[0]) / 2
                else:
                    fd = (model.evaluate(c0, h0 + e)[0] - model.evaluate(c0, h0 - e)[0]) / 2
                out.append(dict(side=side, **dict(zip(FACTOR_KEY, ev.f.loc[p, FACTOR_KEY])),
                                book_cva=ev.cva[p], book_hdg=ev.hdg[p], grad=g, fd=fd, diff=g - fd))
        print(pd.DataFrame(out).to_string(index=False))

        dc, dh = np.zeros(len(idx)), np.zeros(len(idx))
        np.add.at(dc, ev.local[pu], au * 1e4)
        np.add.at(dh, ev.local[ph], ah * 1e4)
        K0, Kb0, Sb0, s0, *_ = model.evaluate(ev.cva[idx], ev.hdg[idx])
        K1, Kb1, Sb1, s1, *_ = model.evaluate(ev.cva[idx] + dc, ev.hdg[idx] + dh)
        regime = lambda s, k: np.where(s >= k, "clip+", np.where(s <= -k, "clip-", "inside"))
        print(f"\nK before {K0:,.2f}  after (+$1/bp) {K1:,.2f}  (base_k {base:,.2f})")
        print(pd.DataFrame(dict(bucket=model.buckets, K_b=Kb0, sum_WS=s0, S_b=Sb0, regime=regime(s0, Kb0),
                                K_b_after=Kb1, sum_WS_after=s1, regime_after=regime(s1, Kb1))).to_string(index=False))


if __name__ == "__main__":
    main()
