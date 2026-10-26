"""Command line: sacva prep | capital | marginal | calibrate | weights | price."""
from __future__ import annotations

import argparse
import logging
import sys

import pandas as pd

from . import report
from .calibrate import calibrate
from .engine import capital
from .cva_calc import calc_tables, read_input
from .marginal import marginal
from .params import load_params, load_policy
from .policy import build_trade
from . import prep as prep_mod
from .sizeterm import size_rw
from .weights import METHODS, read_weights, sacva_weights, write_weights


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--book", required=True, help="CVA_CALC_RF or generic sensitivity file (.csv / .xlsx)")
    p.add_argument("--sheet", help="Excel sheet (default: CVA_CALC_RF if present, else the first sheet)")
    p.add_argument("--params", help="regulatory params YAML (default: bundled OSFI CAR Ch. 8)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sacva", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_cap = sub.add_parser("capital", help="SA-CVA capital of a book (CVA_CALC_RB / CVA_CALC_RT)")
    _common(p_cap)
    p_cap.add_argument("--out", help="output .xlsx file or directory for CSVs")

    p_m = sub.add_parser("marginal", help="marginal capital of a trade file under a hedging policy")
    _common(p_m)
    p_m.add_argument("--trade", required=True, help="CVA sensitivities of the new trade (.csv / .xlsx)")
    p_m.add_argument("--policy", required=True, help="hedging policy name")
    p_m.add_argument("--policy-config", help="hedging policy YAML (default: bundled)")
    p_m.add_argument("--size", type=float, default=1.0, help="incremental trade size multiplier")

    p_c = sub.add_parser("calibrate", help="effective RW grids")
    _common(p_c)
    p_c.add_argument("--policy-config", help="hedging policy YAML (default: bundled)")
    p_c.add_argument("--out", required=True, help="output .xlsx file or directory for CSVs")

    p_w = sub.add_parser("weights", help="SACVAWeights.csv (CCS / vega RWs per hedge type x sector x IG/HY)")
    _common(p_w)
    p_w.add_argument("--policy-config", help="hedging policy YAML (default: bundled)")
    p_w.add_argument("--out", required=True, help="output SACVAWeights.csv (detail written to <stem>_detail.csv)")
    p_w.add_argument("--method", choices=METHODS,
                     help="pricing methodology (default: sacva_weights.method): current = one RW per $1 per cell; "
                          "size_term = Euler base plus a size term (adds eulerRw, standaloneRw, refRw, kEff, refSize)")

    p_pr = sub.add_parser("price", help="RW and capital of a trade of a given size from a SACVAWeights.csv")
    p_pr.add_argument("--weights", required=True, help="SACVAWeights.csv written by `sacva weights`")
    p_pr.add_argument("--cpty", help="cptyCode of an own line (instead of --hedge-type / --bucket / --ig-hy)")
    p_pr.add_argument("--hedge-type", help="hedgeType label, e.g. sector_hedge")
    p_pr.add_argument("--bucket", help="1_A, 1_B, 2 .. 7")
    p_pr.add_argument("--ig-hy", help="IG or HY")
    p_pr.add_argument("--cr01", type=float, help="trade CR01 in $/bp (gross)")
    p_pr.add_argument("--vega", type=float, help="trade vega in $ per 1%% (gross)")
    p_pr.add_argument("--floor", type=float, default=0.0, help="size_term only: RW floor as a fraction of stand-alone")

    p_p = sub.add_parser("prep", help="build the counterparty-level CVA_CALC_RF input from the bank feeds")
    p_p.add_argument("--calc-rf", required=True, help="CVA_CALC_RF_yyyymmdd (risk-factor level, hedges + lookups)")
    p_p.add_argument("--ld-sens", required=True, help="CVA_LD_SENS_yyyymmdd (counterparty-level CVA sensitivities)")
    p_p.add_argument("--mkt-fx", required=True, help="CVA_LD_MKT_FX_yyyymmdd (FX rates to the reporting currency)")
    p_p.add_argument("--uen-map", required=True, help="CPTY_CD -> UEN mapping (columns UEN, CPTY_CD)")
    p_p.add_argument("--mapping", help="input mapping YAML (default: bundled)")
    p_p.add_argument("--params", help="regulatory params YAML (default: bundled OSFI CAR Ch. 8)")
    p_p.add_argument("--out", required=True, help="output cva_calc_rf_yyyymmdd.csv")

    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    if a.cmd == "price":
        return _price(a)
    params = load_params(a.params)
    pd.set_option("display.width", 160)
    if a.cmd == "prep":
        res = prep_mod.build_cva_calc_rf(prep_mod.read_table(a.calc_rf), prep_mod.read_table(a.ld_sens),
                                         prep_mod.read_table(a.mkt_fx), prep_mod.read_table(a.uen_map),
                                         prep_mod.load_mapping(a.mapping), params)
        paths = prep_mod.write(res, a.out)
        print(res.recon_summary.to_string(index=False, float_format="{:,.4f}".format))
        if len(res.exceptions):
            logging.warning("%d CVA_LD_SENS rows not mapped cleanly; see %s", len(res.exceptions), paths[3])
        print("\nwritten: " + ", ".join(str(p) for p in paths))
        return 0
    inp = read_input(a.book, params, a.sheet)
    book = inp.book
    if not inp.recon_breaks.empty:
        logging.warning("%d input rows do not reconcile (RISK_WEIGHT / NW_SENSITIVITY); see RECON_BREAKS",
                        len(inp.recon_breaks))
    if not inp.name_conflicts.empty:
        logging.warning("%d CCS name attribute conflicts between rows (resolved from CVA rows); see NAME_CONFLICTS",
                        len(inp.name_conflicts))
    pd.set_option("display.float_format", "{:,.2f}".format)

    if a.cmd == "capital":
        res = capital(book, params)
        tables = calc_tables(res, inp)
        print(tables["CVA_CALC_RT"].to_string(index=False))
        print(f"\nTotal SA-CVA capital: {res.total:,.2f}")
        if a.out:
            sheets = dict(tables)
            if not inp.recon_breaks.empty:
                sheets["RECON_BREAKS"] = inp.recon_breaks
            if not inp.name_conflicts.empty:
                sheets["NAME_CONFLICTS"] = inp.name_conflicts
            print(f"written: {report.write(sheets, a.out)}")
    elif a.cmd == "marginal":
        pcfg = load_policy(a.policy_config)
        cva = read_input(a.trade, params).book
        cva = cva[cva["source"] == "CVA"]
        trade = build_trade(cva, a.policy, pcfg, params)
        m = marginal(book, trade, params, size=a.size)
        print(trade.to_string(index=False))
        print(f"\nK(book)      {m.base:,.4f}\nincremental  {m.incremental:,.6f}\n"
              f"euler        {m.euler:,.6f}\nstand-alone  {m.standalone:,.6f}")
    elif a.cmd == "calibrate":
        pcfg = load_policy(a.policy_config)
        res = calibrate(book, params, pcfg)
        sheets = report.summary_sheets(res, params, pcfg, inp, calc_tables(capital(book, params), inp))
        out = report.write(sheets, a.out)
        cl = sheets["CPTY_RW_CLUSTER"]
        print(cl.to_string(index=False, float_format="{:.6f}".format) if len(cl) else "no clustered counterparties")
        print(f"\nwritten: {out}")
    elif a.cmd == "weights":
        res = sacva_weights(book, params, load_policy(a.policy_config), method=a.method)
        out = write_weights(res["weights"], a.out)
        detail = out.with_name(f"{out.stem}_detail.csv")
        res["detail"].to_csv(detail, index=False)
        print(res["weights"].to_string(index=False, float_format="{:.4f}".format))
        print(f"\nmethod: {res['method']}\nwritten: {out}, {detail}")
    return 0


def _price(a: argparse.Namespace) -> int:
    """Price a trade from one weight-file row: a size_term file gives RW(x), a current file a flat RW per $1."""
    w = read_weights(a.weights)
    if a.cpty:
        sel = w[w["cptyCode"] == a.cpty]
    elif a.hedge_type and a.bucket and a.ig_hy:
        sel = w[(w["cptyCode"] == "NA") & (w["hedgeType"] == a.hedge_type) & (w["bucket"] == a.bucket)
                & (w["igHy"] == a.ig_hy)]
    else:
        raise SystemExit("give --cpty, or --hedge-type, --bucket and --ig-hy")
    if len(sel) != 1:
        raise SystemExit(f"{len(sel)} matching rows in {a.weights}")
    row, sized = sel.iloc[0], "kEff" in w.columns
    print(f"method: {'size_term' if sized else 'current'}")
    total = 0.0
    for kind, x, cols, flat in (("CCS", a.cr01, ("eulerRw", "standaloneRw", "kEff"), "5y"),
                                ("vega", a.vega, ("vegaEulerRw", "vegaStandaloneRw", "vegaKEff"), "vegaCost")):
        if not x:
            continue
        if sized:
            e, s, k = (float(row[c]) for c in cols)
            rw = size_rw(abs(x), e, s, k, a.floor)
            print(f"{kind}: size {abs(x):,.2f}  RW {rw:,.6f} per $1  (e {e:,.4f}, s {s:,.4f}, K_eff {k:,.0f}, "
                  f"x*s/K_eff {abs(x) * s / k if k else float('inf'):.2%})")
        else:
            rw = float(row[flat])
            print(f"{kind}: size {abs(x):,.2f}  RW {rw:,.6f} per $1 (flat)")
        print(f"{kind}: capital {rw * abs(x):,.2f}")
        total += rw * abs(x)
    print(f"total capital {total:,.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
