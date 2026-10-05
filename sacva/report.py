"""Excel / CSV output of calibration results."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


def _pivot(ccs: pd.DataFrame, col: str) -> pd.DataFrame:
    p = ccs.pivot_table(index=["policy", "bucket"], columns="quality", values=col, aggfunc="first")
    p.columns = [f"{q}" for q in p.columns]
    return p.reset_index()


def _ext_codes(df: pd.DataFrame) -> pd.DataFrame:
    """Report risk classes with the bank RISK_TYPE codes (CMDTY -> CO)."""
    from .cva_calc import RISK_TYPE_OUT
    if "risk_class" not in df.columns:
        return df
    return df.assign(risk_class=df["risk_class"].map(lambda x: RISK_TYPE_OUT.get(x, x)))


def summary_sheets(results: dict[str, Any], params: dict[str, Any], policy_cfg: dict[str, Any], inp=None,
                   calc_tables: dict[str, pd.DataFrame] | None = None) -> dict[str, pd.DataFrame]:
    ccs, mkt = results["ccs_grid"], _ext_codes(results["market_grid"])
    run = [] if inp is None else [("CALC_RUN_ID", inp.calc_run_id), ("BUS_DT", str(inp.bus_dt)), ("CCY", inp.ccy),
                                  ("input format", inp.fmt)]
    if inp is not None and inp.fmt == "cva_calc_rf":
        run.append(("sign convention", "input NW = RW*(SK_CVA + SK_HEDGE); internally s_CVA = -SK_CVA, s_Hdg = SK_HEDGE "
                                       "(OSFI MAR50.53). A unit CCS trade = SK_CVA of -10,000 (adds CVA risk); "
                                       "S_B reported in the input convention"))
    info = pd.DataFrame(run + [
        ("regulatory_params", params.get("version", "")),
        ("m_cva", params["m_cva"]),
        ("hedging_disallowance_R", params["hedging_disallowance_R"]),
        ("incremental_trade_size", policy_cfg["calibration"].get("trade_size", 1.0)),
        ("ccs_unit", policy_cfg["calibration"]["ccs"]["unit"]),
        ("ccs_tenor_profile", str(policy_cfg["calibration"]["ccs"]["tenor_profile"])),
        ("base_SA_CVA_capital", float(results["base_capital"]["K"].sum())),
        ("*_eff_rw (the RW)", "capital change per 1 unit of normalised sensitivity (dV/0.0001 for bp factors, "
                              "dV/0.01 for 1% relative factors); directly comparable with the regulatory RW"),
        ("unit trade", "1 per_bp = 10,000 normalised (a $1/bp CR01 or DV01); 1 per_1pct = 100 normalised"),
        ("*_per_unit", "capital change for the whole unit trade = eff_rw x 10,000 (bp factors) or x 100 (1% factors)"),
        ("Euler caveat", "Euler omits the R*(WS_Hdg)^2 disallowance of hedge factors absent from the book"),
        ("CPTY_RW", "per counterparty (INTERNAL_ENTITY|COUNTERPARTY_ID) x risk type: trade = the counterparty's own net "
                    "sensitivity profile scaled to a gross size of "
                    f"{policy_cfg['calibration'].get('counterparty', {}).get('trade_size', 1e4):,.0f} + its policy hedge; "
                    "RW = capital change / gross size, i.e. per unit of gross normalised sensitivity"),
        ("CPTY_RW_CLUSTER", "gross-sensitivity-weighted RW by policy x sector x quality x risk type for policies "
                            f"{policy_cfg['calibration'].get('counterparty', {}).get('cluster_policies', ['sector_hedge'])}"),
        ("CPTY_EULER_RECON", "per risk type: CVA Euler (counterparties + unattributed) + macro-hedge Euler = K_RT; "
                             "macro hedge split into policy-implied (in counterparty allocations) and unallocated"),
    ], columns=["item", "value"])
    sheets = {
        "Info": info,
        "CCS_Incremental_RW": _pivot(ccs, "incremental_eff_rw"),
        "CCS_Euler_RW": _pivot(ccs, "euler_eff_rw"),
        "CCS_Standalone_RW": _pivot(ccs, "standalone_eff_rw"),
        "CCS_Detail": ccs,
        "Market_Grid": mkt,
        "Base_Capital": _ext_codes(results["base_capital"]),
        "Euler_Contrib_Cpty": _ext_codes(pd.concat([results["euler"]["by_counterparty"], results["euler"]["check"]],
                                                   ignore_index=True)),
        "Euler_Contrib_Factor": _ext_codes(results["euler"]["by_factor"]),
    }
    if calc_tables:
        sheets.update(calc_tables)
    if inp is not None and not inp.recon_breaks.empty:
        sheets["RECON_BREAKS"] = inp.recon_breaks
    if inp is not None and not inp.name_conflicts.empty:
        sheets["NAME_CONFLICTS"] = inp.name_conflicts
    cp = results.get("counterparty")
    if cp is not None:
        sheets["CPTY_RW"] = _ext_codes(cp["cpty_rw"])
        sheets["CPTY_RW_CLUSTER"] = _ext_codes(cp["cpty_cluster"])
        sheets["CPTY_EULER_RECON"] = _ext_codes(cp["euler_recon"])
        sheets["Counterparty_Policy"] = cp["cpty_table"]
    return sheets


def write(sheets: dict[str, pd.DataFrame], out: str | Path) -> Path:
    out = Path(out)
    if out.suffix.lower() == ".xlsx":
        out.parent.mkdir(parents=True, exist_ok=True)
        with pd.ExcelWriter(out, engine="openpyxl") as xw:
            for name, df in sheets.items():
                df.to_excel(xw, sheet_name=name[:31], index=False)
    else:
        out.mkdir(parents=True, exist_ok=True)
        for name, df in sheets.items():
            df.to_csv(out / f"{name}.csv", index=False)
    return out
