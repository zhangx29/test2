"""Regenerate every book-dependent number, table and the exhibit of the RW methodology memo
(docs/2026-10-20-sa-cva-rw-methodology.docx) on a given book.

    python examples/memo_figures.py --book <CVA_CALC_RF file> --out out/memo

Writes to --out:

* memo_numbers.csv   section, item, value: every inline number of the memo, keyed to where it appears
* table_3_4.csv      sector_hedge vs name_hedge new-name RW per $1 CR01 at $1, x_ref and stand-alone
* buckets_3_5.csv    CCS K_b / S_b per bucket against the stand-alone capital of an x_ref probe
* curve_3_7.csv      incremental RW / stand-alone by probe size (17 sizes, $1 to $100mm) for the exhibit cells
* table_4_2.csv      full recalculation vs K_eff fit at $1k and $31.6k, max error as % of stand-alone
* grid_cells.csv     the same quantities for every hedge type x bucket x IG/HY cell, to choose the memo's cells
* figure_3_7.png     the exhibit (only if matplotlib is installed)

All RWs are per $1 of CR01 ($/bp). Cells are new-name probes (memo 2.3) with the bundled 5Y profile unless the
policy config says otherwise; 3.6 and 5 also run the counterparty weights (``--skip-cpty`` to leave them out).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from sacva.calibrate import NEW_NAME, _ccs_trade
from sacva.cva_calc import read_input
from sacva.engine import capital
from sacva.marginal import MarginalEvaluator, scaled
from sacva.params import load_params, load_policy
from sacva.policy import build_trade
from sacva.sizeterm import k_eff, size_rw
from sacva.weights import BUCKET_OUT, sacva_weights

SIZES = 10 ** np.arange(0, 8.01, 0.5)      # 17 probe sizes, $1 .. $100mm per bp


def _cells(s: str, with_policy: bool) -> list[tuple]:
    return [tuple(c.split(":")) for c in s.split(",")] if s else []


def _money(x: float) -> str:
    return f"${x / 1e6:,.2f}mm" if abs(x) >= 1e6 else f"${x:,.0f}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--book", required=True)
    ap.add_argument("--sheet")
    ap.add_argument("--params", help="regulatory params YAML (default: bundled)")
    ap.add_argument("--policy-config", help="hedging policy YAML (default: bundled)")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--ref", type=float, default=1e4, help="reference / production probe size x_ref in $/bp")
    ap.add_argument("--table-cells", default="1a:IG,2:IG,3:HY,7:HY", help="3.4 rows, bucket:quality")
    ap.add_argument("--curve-cells", default="unhedged_delta:2:IG,name_hedge:2:IG,sector_hedge:2:IG,"
                                             "unhedged_delta:6:HY", help="3.7 / 4.2 rows, policy:bucket:quality")
    ap.add_argument("--offset-cell", default="", help="3.5 offset cell policy:bucket:quality "
                                                      "(default: the cell with the lowest $1 share of stand-alone)")
    ap.add_argument("--cpty-cell", default="unhedged_delta:2:IG", help="3.6 cell, policy:bucket:quality")
    ap.add_argument("--cf-bucket", default="2", help="3.3 / 5 bucket for the closed-form and Euler checks")
    ap.add_argument("--skip-cpty", action="store_true", help="skip the counterparty weights (3.6, 5)")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    params, pcfg = load_params(a.params), load_policy(a.policy_config)
    book = read_input(a.book, params, a.sheet).book
    ev = MarginalEvaluator(book, params, 1.0)
    K = ev.base_k.get(("CCS", "delta"), 0.0)
    ref = a.ref
    i_ref_pos = int(np.argmin(np.abs(SIZES - ref)))
    if not np.isclose(SIZES[i_ref_pos], ref):
        SIZES_ = np.sort(np.append(SIZES, ref))
    else:
        SIZES_ = SIZES
    i_ref_pos = int(np.argmin(np.abs(SIZES_ - ref)))
    k1, k31 = int(np.argmin(np.abs(SIZES_ - 1e3))), int(np.argmin(np.abs(SIZES_ - 10 ** 4.5)))
    num: list[dict] = []

    def put(section: str, item: str, value, memo_example: str = "") -> None:
        num.append(dict(section=section, item=item, value=value, example_book_value=memo_example))

    # ---- every new-name cell: full curve, Euler, stand-alone, K_eff fit, closed form with class K -------------------
    wcfg = pcfg["sacva_weights"]
    label_of = {p: lbl for lbl, p in wcfg["hedge_types"].items()}
    profile = pcfg["calibration"]["ccs"]["tenor_profile"]
    policies = list(dict.fromkeys(list(wcfg["hedge_types"].values())
                                  + [c[0] for c in _cells(a.curve_cells, True)] + ["sector_hedge", "name_hedge"]))
    trades = {(pol, str(b), q): build_trade(_ccs_trade(f"{NEW_NAME}{b}_{q}", str(b), q, profile, "per_bp", params),
                                            pol, pcfg, params)
              for pol in policies for b in wcfg["buckets"] for q in wcfg["qualities"]}
    ev.prepare(list(trades.values()))
    rows, curves = [], {}
    for (pol, b, q), t in trades.items():
        r = ev.evaluate(t)
        full = np.array([ev.evaluate(scaled(t, x)).incremental / x for x in SIZES_])
        curves[(pol, b, q)] = full
        e1, eul, s = full[0], r.euler, r.standalone
        ke = k_eff(e1, s, full[i_ref_pos], ref, K)
        fit = size_rw(SIZES_, e1, s, ke)
        cf = size_rw(SIZES_, eul, s, K) if K > 0 else np.full_like(SIZES_, s)
        rows.append(dict(policy=pol, hedge_type=label_of.get(pol, ""), bucket=b, quality=q, euler_rw=eul,
                         inc_rw_1=e1, inc_rw_1k=full[k1], inc_rw_ref=full[i_ref_pos], inc_rw_31_6k=full[k31],
                         standalone_rw=s, share_1=e1 / s if s else np.nan,
                         share_ref=full[i_ref_pos] / s if s else np.nan, ref_probe_standalone_capital=ref * s,
                         k_eff=ke, fit_1k=fit[k1], fit_31_6k=fit[k31],
                         fit_max_err_pct_sa=np.max(np.abs(fit - full)) / s if s else np.nan,
                         cf_max_err_rel=np.max(np.abs(cf - full) / np.abs(full)),
                         cf_max_err_pct_sa=np.max(np.abs(cf - full)) / s if s else np.nan,
                         monotone=bool(np.all(np.diff(full) >= -1e-9 * max(s, 1.0))),
                         euler_vs_inc1=eul / e1 - 1 if e1 else np.nan))
    grid = pd.DataFrame(rows)
    grid.to_csv(out / "grid_cells.csv", index=False)
    g = grid.set_index(["policy", "bucket", "quality"])
    hedge_cells = grid[grid["policy"].isin(wcfg["hedge_types"].values())]

    # ---- 1.x / 3.3 / 5: book capital ---------------------------------------------------------------------------------
    put("3.3, 3.7 subtitle, 4.1, 5", "CCS delta capital K (class level)", K, "$3.76mm")
    put("1.x", "total SA-CVA capital", ev.base, "")
    cfb = grid[(grid["bucket"] == a.cf_bucket) & grid["policy"].isin(wcfg["hedge_types"].values())]
    put("3.3", f"closed form (class K) vs full, bucket {a.cf_bucket}: max relative error over cells and sizes",
        cfb["cf_max_err_rel"].max(), "6.5% (sector_hedge)")
    put("3.3", f"closed form (class K) vs full, bucket {a.cf_bucket}: max error as % of stand-alone",
        cfb["cf_max_err_pct_sa"].max(), "2.8% (memo: 'within 4%')")
    worst = grid.loc[grid["cf_max_err_rel"].idxmax()]
    put("3.3", "worst closed-form cell (relative error)", f"{worst['policy']} {worst['bucket']} {worst['quality']}: "
                                                          f"{worst['cf_max_err_rel']:.1%}", "near-flat buckets (3.5)")

    # ---- 3.2: monotonicity -------------------------------------------------------------------------------------------
    put("3.2", "cells tested / monotone in size (hedge-type cells)",
        f"{int(hedge_cells['monotone'].sum())} of {len(hedge_cells)}", "48 of 48")

    # ---- 3.4 ----------------------------------------------------------------------------------------------------------
    t34 = []
    for b, q in _cells(a.table_cells, False):
        sh, nh = g.loc[("sector_hedge", b, q)], g.loc[("name_hedge", b, q)]
        t34.append({"cell": f"{b} {q}", "sector_hedge $1": sh["inc_rw_1"], "name_hedge $1": nh["inc_rw_1"],
                    f"sector_hedge ${ref:,.0f}": sh["inc_rw_ref"], f"name_hedge ${ref:,.0f}": nh["inc_rw_ref"],
                    "sector_hedge stand-alone": sh["standalone_rw"], "name_hedge stand-alone": nh["standalone_rw"]})
    pd.DataFrame(t34).to_csv(out / "table_3_4.csv", index=False)
    put("3.4", "table: see table_3_4.csv", len(t34))

    # ---- 3.5: offset bucket and probe vs bucket capital ---------------------------------------------------------------
    cap = capital(book, params).by_bucket
    cap = cap[(cap["risk_class"] == "CCS") & (cap["measure"] == "delta")].copy()
    ud = grid[grid["policy"] == "unhedged_delta"].pivot(index="bucket", columns="quality", values="standalone_rw")
    cap["ref_probe_standalone_IG"] = cap["bucket"].map(lambda b: ref * ud["IG"].get(b, np.nan) if "IG" in ud else np.nan)
    cap["ref_probe_standalone_HY"] = cap["bucket"].map(lambda b: ref * ud["HY"].get(b, np.nan) if "HY" in ud else np.nan)
    cap["probe_IG_over_K_b"] = cap["ref_probe_standalone_IG"] / cap["K_b"]
    cap.to_csv(out / "buckets_3_5.csv", index=False)
    put("3.5", f"stand-alone capital of a ${ref:,.0f}/bp unhedged 2 IG probe", g.loc[("unhedged_delta", "2", "IG"),
                                                                                    "ref_probe_standalone_capital"]
        if ("unhedged_delta", "2", "IG") in g.index else np.nan, "$5.0mm (= 10k x 5% x 10k: regulatory, not book)")
    if a.offset_cell:
        off = tuple(a.offset_cell.split(":"))
    else:
        h = hedge_cells[hedge_cells["policy"] == "unhedged_delta"]
        off = tuple(h.loc[h["share_1"].idxmin(), ["policy", "bucket", "quality"]])
    o = g.loc[off]
    put("3.5", "offset cell (lowest $1 share of stand-alone, unhedged)", " ".join(off), "unhedged_delta 6 HY")
    put("3.5", "offset cell: $1 incremental RW (memo calls it Euler)", o["inc_rw_1"], "0.25")
    put("3.5", f"offset cell: incremental RW at ${ref:,.0f}", o["inc_rw_ref"], "232")

    # ---- 3.7 ----------------------------------------------------------------------------------------------------------
    cc = _cells(a.curve_cells, True)
    curve = pd.DataFrame({"size_per_bp": SIZES_} | {f"{p} {b} {q}": curves[(p, b, q)] / g.loc[(p, b, q), "standalone_rw"]
                                                     for p, b, q in cc})
    curve.to_csv(out / "curve_3_7.csv", index=False)
    shares_ref = [g.loc[c, "share_ref"] for c in cc]
    put("3.7 title / label", f"share of stand-alone at ${ref:,.0f}: min - max over exhibit cells",
        f"{min(shares_ref):.0%}-{max(shares_ref):.0%}", "46-75%")
    for c in cc:
        put("3.7 end labels", f"{' '.join(c)}: $1 share of stand-alone", g.loc[c, "share_1"],
            {"unhedged_delta 2 IG": "49%", "name_hedge 2 IG": "45%", "sector_hedge 2 IG": "11%",
             "unhedged_delta 6 HY": "0.05%"}.get(" ".join(c), ""))
    i1m = int(np.argmin(np.abs(SIZES_ - 1e6)))
    put("3.7 text", "lowest share of stand-alone at $1mm over exhibit cells",
        min(curves[c][i1m] / g.loc[c, "standalone_rw"] for c in cc), "98.0% ('reaches stand-alone by about $1mm')")
    put("3.7 text", f"offset cell share: $1 -> ${ref:,.0f}", f"{o['share_1']:.2%} -> {o['share_ref']:.0%}",
        "near zero -> almost half")

    # ---- 4.1 / 4.2 ----------------------------------------------------------------------------------------------------
    t42 = [{"cell": " ".join(c), "full $1k": g.loc[c, "inc_rw_1k"], "fit $1k": g.loc[c, "fit_1k"],
            "full $31.6k": g.loc[c, "inc_rw_31_6k"], "fit $31.6k": g.loc[c, "fit_31_6k"],
            "max error % of stand-alone": g.loc[c, "fit_max_err_pct_sa"], "k_eff": g.loc[c, "k_eff"]} for c in cc]
    pd.DataFrame(t42).to_csv(out / "table_4_2.csv", index=False)
    put("4.1", "K_eff range over exhibit cells", f"{_money(min(r['k_eff'] for r in t42))} to "
                                                 f"{_money(max(r['k_eff'] for r in t42))}", "$4.06mm to $4.23mm")
    put("4.1", "K_eff range over all hedge-type cells", f"{_money(hedge_cells['k_eff'].min())} to "
                                                         f"{_money(hedge_cells['k_eff'].max())}", "")
    put("4.2", "max fit error over all hedge-type cells, % of stand-alone", hedge_cells["fit_max_err_pct_sa"].max(), "")
    over = hedge_cells.assign(d=hedge_cells["fit_1k"] - hedge_cells["inc_rw_1k"])
    put("4.2 text", "cells where the fit overstates the full $1k RW by > 10%",
        ", ".join(f"{r.policy} {r.bucket} {r.quality}" for r in over[over["d"] > 0.1 * over["inc_rw_1k"]]
                  .itertuples()) or "none", "unhedged_delta 6 HY (fit 29.7 vs 17.2)")
    put("4.4", f"cells where a ${ref:,.0f} trade has x*s > 10% of K_eff",
        int((hedge_cells["ref_probe_standalone_capital"] > 0.1 * hedge_cells["k_eff"]).sum()), "")

    # ---- 3.6 / 5: counterparty cells ---------------------------------------------------------------------------------
    if not a.skip_cpty:
        res = sacva_weights(book, params, pcfg, ev)
        d, r = res["detail"], res["cpty_rw"]
        p, b, q = a.cpty_cell.split(":")
        lbl = label_of.get(p, p)
        row = d[(d["hedgeType"] == lbl) & (d["bucket"] == BUCKET_OUT.get(b, b)) & (d["igHy"] == q)].iloc[0]
        put("3.6", f"{a.cpty_cell}: new-name RW per $1", g.loc[(p, b, q), "inc_rw_1"], "245")
        put("3.6", f"{a.cpty_cell}: counterparty cell RW per $1 ({row['ccs_source']}, {row['ccs_n_cpty']} cpty)",
            row["ccs_rw"], "362")
        c = r[(r["risk_class"] == "CCS") & (r["hedge_type"] == label_of.get("sector_hedge", "sector_hedge"))
              & (r["sector"] == a.cf_bucket)]
        if len(c):
            w = c["gross_sens_user"]
            eu, inc = (c["euler_rw"] * w).sum() / w.sum(), (c["incremental_rw"] * w).sum() / w.sum()
            put("5", f"sector_hedge counterparties bucket {a.cf_bucket}: Euler vs $1 incremental (gross-weighted)",
                eu / inc - 1, "within 0.1%")
            put("5", f"sector_hedge counterparties bucket {a.cf_bucket}: worst single counterparty |Euler/inc - 1|",
                float((c["euler_rw"] / c["incremental_rw"] - 1).abs().max()), "")
        put("2.3 / 3.6", "counterparty cells populated / sector rows", f"{int((d['ccs_source'] == 'counterparties').sum())}"
                                                                     f" of {int((d['cptyCode'] == 'NA').sum())}", "")

    nums = pd.DataFrame(num)
    nums.to_csv(out / "memo_numbers.csv", index=False)
    with pd.option_context("display.width", 200, "display.max_colwidth", 90):
        print(nums.to_string(index=False))
    _plot(curve, cc, g, ref, K, out)
    print(f"\nwritten to {out}")


def _plot(curve: pd.DataFrame, cc: list[tuple], g: pd.DataFrame, ref: float, K: float, out: Path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed: figure_3_7.png not written (curve_3_7.csv has the data)")
        return
    plt.rcParams["text.parse_math"] = False         # literal $ signs
    shares = [g.loc[c, "share_ref"] for c in cc]
    # end-label positions, pushed apart to at least 4.5 points of the y axis
    ys = {c: curve[" ".join(c)].iloc[0] * 100 for c in cc}
    pos, last_y = {}, None
    for c in sorted(cc, key=lambda c: -ys[c]):
        pos[c] = ys[c] if last_y is None else min(ys[c], last_y - 4.5)
        last_y = pos[c]
    fig, ax = plt.subplots(figsize=(10, 5.2))
    greys = ["#bdbab0", "#d6d4cc", "#c9c7bf"]
    for i, c in enumerate(cc):
        last = i == len(cc) - 1
        y = curve[" ".join(c)].to_numpy()
        ax.plot(curve["size_per_bp"], y * 100, marker="o", ms=4, lw=2.2 if last else 1.6,
                color="#2a78d6" if last else greys[i % len(greys)], zorder=3 if last else 2)
        ax.annotate(f"{' '.join(c)} · {g.loc[c, 'share_1']:.{2 if g.loc[c, 'share_1'] < 0.01 else 0}%}",
                    (1, pos[c]), xytext=(-8, 0), textcoords="offset points", ha="right", va="center",
                    color="#2a78d6" if last else "#333333", fontsize=10)
    ax.axvline(ref, ls="--", color="#888888", lw=1)
    ax.text(ref, 97, f"Current production probe, ${ref / 1e3:,.0f}k/bp\n{min(shares):.0%}-{max(shares):.0%} of "
                     f"stand-alone", ha="right", va="top", fontsize=10, color="#555555")
    ax.set_xscale("log")
    ax.set_xlim(1, 1e8)
    ax.set_ylim(0, 102)
    ax.set_xticks([1, 1e2, 1e4, 1e6, 1e8], ["$1", "$100", "$10k", "$1mm", "$100mm"])
    ax.yaxis.tick_right()
    ax.set_yticks([0, 25, 50, 75, 100], ["0%", "25%", "50%", "75%", "100%"])
    ax.grid(axis="y", color="#e5e5e5")
    for s in ("top", "left", "right"):
        ax.spines[s].set_visible(False)
    ax.set_xlabel("Probe size, CR01 in $/bp (log scale)")
    fig.suptitle(f"A ${ref / 1e3:,.0f}k probe already prices {min(shares):.0%}-{max(shares):.0%} of stand-alone "
                 f"capital", x=0.02, ha="left", fontsize=14, fontweight="bold")
    ax.set_title(f"Incremental RW per $1 CR01 as a share of stand-alone RW, by probe size. CCS K = ${K / 1e6:,.2f}mm",
                 loc="left", fontsize=10, color="#555555")
    fig.subplots_adjust(left=0.27, right=0.93, top=0.86, bottom=0.12)
    fig.savefig(out / "figure_3_7.png", dpi=150)
    print(f"figure: {out / 'figure_3_7.png'}")


if __name__ == "__main__":
    main()
