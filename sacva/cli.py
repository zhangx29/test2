"""Command line: sacva prep | capital | marginal | calibrate | weights."""
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
from .weights import sacva_weights, write_weights


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
        res = sacva_weights(book, params, load_policy(a.policy_config))
        out = write_weights(res["weights"], a.out)
        detail = out.with_name(f"{out.stem}_detail.csv")
        res["detail"].to_csv(detail, index=False)
        print(res["weights"].to_string(index=False, float_format="{:.4f}".format))
        print(f"\nwritten: {out}, {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
