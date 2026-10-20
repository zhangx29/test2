"""Build the counterparty-level CVA_CALC_RF input (cva_calc_rf_yyyymmdd.csv) from the bank feeds.

Inputs (all read as text, column names as in the bank tables):

* CVA_CALC_RF_yyyymmdd      SA-CVA at risk-factor level, CVA and hedges mapped (no counterparty attribution):
                            the source of the hedge rows and of the CCS / market attribute and RW lookups
* CVA_LD_SENS_yyyymmdd      counterparty-level CVA sensitivities before mapping: DATA_RUN_ID, BUS_DT,
                            INTERNAL_ENTITY, COUNTERPARTY_ID, RISK_TYPE (Risk_Credit, Risk_IRCurve, ...), TENOR,
                            BUCKET, QUALIFIER, SENSITIVITY_AMOUNT, SENSITIVITY_AMOUNT_CURRENCY, REGIME_LEGAL_ENTITY
* CVA_LD_MKT_FX_yyyymmdd    BUS_DT, CCY_PAIR, FX_SRC, RATE, DATA_RUN_ID (e.g. CADUSD 0.702765 = USD per 1 CAD)
* CPTY_CD -> UEN mapping    UEN, CPTY_CD (distinct pairs from CVA_LD_TRADE)

Output rows (columns of :data:`sacva.cva_calc.RF_COLUMNS`):

* Hedges: CVA_CALC_RF rows with SK_HEDGE != 0; SK_CVA = 0, NW_SENSITIVITY = SK_HEDGE * RISK_WEIGHT,
  INTERNAL_ENTITY / COUNTERPARTY_ID blank.
* Market CVA (CVA_LD_SENS, RISK_TYPE other than Risk_Credit), per INTERNAL_ENTITY, COUNTERPARTY_ID and factor:
  RISK_MEASURE / RISK_TYPE from the RISK_TYPE mapping; RISK_SUB_BUCKET = RISK_BUCKET = BUCKET for CO / EQ,
  QUALIFIER otherwise; RISK_FACTOR = TENOR for IR delta in a major currency, PARALLEL for other IR delta, the
  RISK_TYPE (IR, FX, EQ, CO) otherwise; TENOR, NAME and CREDIT_* blank; RISK_WEIGHT looked up in CVA_CALC_RF on
  RISK_MEASURE + RISK_TYPE + RISK_SUB_BUCKET + RISK_BUCKET + RISK_FACTOR.
* CCS CVA (Risk_Credit): UEN from the CPTY_CD mapping (CP:<CPTY_CD> when unmapped); RISK_SUB_BUCKET,
  RISK_BUCKET, CREDIT_RTG, CREDIT_QUALITY, CREDIT_RELATED and RISK_WEIGHT looked up from the CVA_CALC_RF CCS rows
  with NAME = UEN; NAME = UEN; RISK_FACTOR = <UEN>_<TENOR>.
* All CVA rows: SK_CVA = SENSITIVITY_AMOUNT converted to the reporting currency, SK_HEDGE = 0,
  NW_SENSITIVITY = SK_CVA * RISK_WEIGHT. Rows are summed per counterparty and factor.

CVA rows that cannot be mapped are dropped and listed in ``exceptions``; ``recon`` compares the summed
counterparty CVA with the CVA_CALC_RF SK_CVA per risk factor.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from . import riskclass
from .cva_calc import IR_DELTA_ALIASES, RF_COLUMNS, RISK_TYPE_IN, _clean
from .loader import BookValidationError

FACTOR_COLS = ["RISK_MEASURE", "RISK_TYPE", "RISK_SUB_BUCKET", "RISK_BUCKET", "RISK_FACTOR"]
CCS_ATTRS = ["RISK_SUB_BUCKET", "RISK_BUCKET", "CREDIT_RTG", "CREDIT_QUALITY", "CREDIT_RELATED", "RISK_WEIGHT"]
EXC_COLS = ["INTERNAL_ENTITY", "COUNTERPARTY_ID", "RISK_TYPE", "TENOR", "BUCKET", "QUALIFIER", "SK_CVA", "issue"]


@dataclass
class PrepResult:
    rf: pd.DataFrame              # counterparty-level CVA_CALC_RF
    recon: pd.DataFrame           # per risk factor: CVA_CALC_RF SK_CVA vs summed counterparty SK_CVA
    recon_summary: pd.DataFrame   # the same per RISK_MEASURE x RISK_TYPE, with the ratio
    exceptions: pd.DataFrame      # CVA_LD_SENS rows not mapped (dropped) or RWs not found in CVA_CALC_RF


def load_mapping(path: str | Path | None = None) -> dict[str, Any]:
    if path is None:
        text = resources.files("sacva.config").joinpath("input_mapping.yaml").read_text(encoding="utf-8")
    else:
        text = Path(path).read_text(encoding="utf-8")
    return yaml.safe_load(text)


def read_table(path: str | Path, sheet: str | None = None) -> pd.DataFrame:
    """Read a CSV / Excel table as text with upper-case column names."""
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        df = pd.read_excel(path, sheet_name=sheet or 0, dtype=str)
    else:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.columns = [str(c).strip().upper() for c in df.columns]
    return df


def _text(s: pd.Series) -> pd.Series:
    return s.map(_clean)


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(_text(s).str.replace(",", "", regex=False).replace("", "0"), errors="raise").astype(float)


def _canon_factor(measure: pd.Series, rtype: pd.Series, factor: pd.Series) -> pd.Series:
    """RISK_FACTOR as compared across files: IR delta 'IR' / 'CURVE' are the PARALLEL factor."""
    ir_delta = (measure == "DELTA") & (rtype == "IR")
    return factor.where(~ir_delta, factor.replace(IR_DELTA_ALIASES))


def fx_multipliers(mkt_fx: pd.DataFrame, ccy: str) -> dict[str, float]:
    """Multiplier converting an amount in currency X into ``ccy``: CCY_PAIR <ccy>X at RATE (X per 1 ccy) -> 1/RATE;
    X<ccy> -> RATE."""
    mult: dict[str, float] = {ccy: 1.0}
    seen: dict[str, float] = {}
    for pair, rate in zip(_text(mkt_fx["CCY_PAIR"]).str.upper(), _num(mkt_fx["RATE"])):
        base, quote = pair[:3], pair[3:]
        if len(pair) != 6 or ccy not in (base, quote) or base == quote:
            continue
        if pair in seen and not np.isclose(seen[pair], rate):
            raise BookValidationError(f"CVA_LD_MKT_FX has several rates for {pair}: {seen[pair]}, {rate}")
        if rate <= 0:
            raise BookValidationError(f"CVA_LD_MKT_FX rate for {pair} must be positive")
        seen[pair] = rate
        if base == ccy:
            mult[quote] = 1.0 / rate
        else:
            mult[base] = rate
    return mult


def uen_lookup(uen_map: pd.DataFrame) -> dict[str, str]:
    """CPTY_CD -> UEN; a CPTY_CD mapped to several UENs is an error."""
    m = pd.DataFrame({"CPTY_CD": _text(uen_map["CPTY_CD"]), "UEN": _text(uen_map["UEN"])})
    m = m[(m["CPTY_CD"] != "") & (m["UEN"] != "")].drop_duplicates()
    dup = m[m["CPTY_CD"].duplicated(keep=False)]
    if len(dup):
        pairs = dup.groupby("CPTY_CD")["UEN"].apply(lambda u: "/".join(u)).head(20)
        raise BookValidationError("CPTY_CD mapped to several UENs: " + ", ".join(f"{k}->{v}" for k, v in pairs.items()))
    return dict(zip(m["CPTY_CD"], m["UEN"]))


def _single(df: pd.DataFrame, col: str) -> str:
    vals = sorted(set(_text(df[col])) - {""}) if col in df.columns else []
    if len(vals) > 1:
        raise BookValidationError(f"CVA_CALC_RF mixes several {col} values: {vals[:5]}")
    return vals[0] if vals else ""


def build_cva_calc_rf(calc_rf: pd.DataFrame, ld_sens: pd.DataFrame, mkt_fx: pd.DataFrame, uen_map: pd.DataFrame,
                      mapping: dict[str, Any], params: dict[str, Any]) -> PrepResult:
    calc = calc_rf.copy()
    for c in RF_COLUMNS:
        if c in calc.columns and c not in ("SK_CVA", "SK_HEDGE", "RISK_WEIGHT", "NW_SENSITIVITY"):
            calc[c] = _text(calc[c])
    for c in ("SK_CVA", "SK_HEDGE", "RISK_WEIGHT"):
        calc[c] = _num(calc[c])
    calc["RISK_MEASURE"], calc["RISK_TYPE"] = calc["RISK_MEASURE"].str.upper(), calc["RISK_TYPE"].str.upper()
    calc["_FACTOR"] = _canon_factor(calc["RISK_MEASURE"], calc["RISK_TYPE"], calc["RISK_FACTOR"])
    run_id, bus_dt = _single(calc, "CALC_RUN_ID"), _single(calc, "BUS_DT")
    ccy = mapping.get("reporting_currency", params["reporting_currency"])
    is_ccs = calc["RISK_TYPE"] == "CCS"

    # hedge rows: CVA_CALC_RF rows with a hedge sensitivity, CVA zeroed
    hdg = calc[calc["SK_HEDGE"] != 0].copy()
    hdg = hdg.assign(CALC_RUN_ID=run_id, BUS_DT=bus_dt, INTERNAL_ENTITY="", COUNTERPARTY_ID="", SK_CVA=0.0,
                     NW_SENSITIVITY=hdg["SK_HEDGE"] * hdg["RISK_WEIGHT"], CCY=ccy)

    # counterparty CVA: map, convert, sum
    ld = ld_sens.copy()
    for c in ("INTERNAL_ENTITY", "COUNTERPARTY_ID", "RISK_TYPE", "TENOR", "BUCKET", "QUALIFIER",
              "SENSITIVITY_AMOUNT_CURRENCY"):
        ld[c] = _text(ld[c]) if c in ld.columns else ""
    ld["TENOR"], ld["QUALIFIER"] = ld["TENOR"].str.upper(), ld["QUALIFIER"].str.upper()
    rt_map = mapping["risk_types"]
    ld["RISK_MEASURE"] = ld["RISK_TYPE"].map(lambda t: rt_map.get(t, {}).get("measure", ""))
    ld["RTYPE"] = ld["RISK_TYPE"].map(lambda t: rt_map.get(t, {}).get("type", ""))
    fx = fx_multipliers(mkt_fx, ccy)
    cur = ld["SENSITIVITY_AMOUNT_CURRENCY"].str.upper().replace("", ccy)
    scale = ld["RTYPE"].map(lambda t: float((mapping.get("amount_scale") or {}).get(t, 1.0)))
    ld["SK_CVA"] = float(mapping.get("cva_sign", 1)) * scale * _num(ld["SENSITIVITY_AMOUNT"]) * cur.map(fx)

    exc = []

    def drop(mask: pd.Series, issue: str) -> None:
        if mask.any():
            exc.append(ld[mask].assign(issue=issue)[EXC_COLS])

    drop(ld["RTYPE"] == "", "RISK_TYPE not in mapping")
    drop((ld["RTYPE"] != "") & ld["SK_CVA"].isna(), "no CVA_LD_MKT_FX rate for SENSITIVITY_AMOUNT_CURRENCY")
    drop((ld["RTYPE"] != "") & (ld["COUNTERPARTY_ID"] == ""), "COUNTERPARTY_ID blank")
    ld = ld[(ld["RTYPE"] != "") & ld["SK_CVA"].notna() & (ld["COUNTERPARTY_ID"] != "")]

    mkt, cr = ld[ld["RTYPE"] != "CCS"].copy(), ld[ld["RTYPE"] == "CCS"].copy()

    # market
    sub = np.where(mkt["RTYPE"].isin(mapping.get("bucket_from_bucket", ["CO", "EQ"])), mkt["BUCKET"], mkt["QUALIFIER"])
    mkt["RISK_SUB_BUCKET"] = mkt["RISK_BUCKET"] = sub
    ir_delta = (mkt["RTYPE"] == "IR") & (mkt["RISK_MEASURE"] == "DELTA")
    major = mkt["QUALIFIER"].isin(mapping["ir_major_currencies"])
    mkt["RISK_FACTOR"] = np.where(ir_delta, np.where(major, mkt["TENOR"], mapping.get("ir_other_factor", "PARALLEL")),
                                  mkt["RTYPE"])
    mkt = mkt.assign(RISK_TYPE=mkt["RTYPE"], TENOR="", NAME="", CREDIT_RTG="", CREDIT_QUALITY="", CREDIT_RELATED="")
    mkt["_FACTOR"] = _canon_factor(mkt["RISK_MEASURE"], mkt["RISK_TYPE"], mkt["RISK_FACTOR"])
    keys = ["RISK_MEASURE", "RISK_TYPE", "RISK_SUB_BUCKET", "RISK_BUCKET", "_FACTOR"]
    rw_tab = calc[~is_ccs].groupby(keys)["RISK_WEIGHT"].first()
    mkt = mkt.merge(rw_tab.reset_index(), on=keys, how="left")
    missing = mkt["RISK_WEIGHT"].isna()
    if missing.any():   # factor absent from CVA_CALC_RF: regulatory RW, or drop when the factor is invalid
        osfi = mkt[missing].apply(lambda r: _osfi_rw(r, params), axis=1)
        mkt.loc[missing, "RISK_WEIGHT"] = osfi
        exc.append(mkt[missing & osfi.notna()].assign(issue="factor not in CVA_CALC_RF: OSFI RW used")[EXC_COLS])
        exc.append(mkt[missing & osfi.isna()].assign(issue="invalid market risk factor (dropped)")[EXC_COLS])
        mkt = mkt[mkt["RISK_WEIGHT"].notna()]

    # CCS
    src = calc[is_ccs].assign(_cva=(calc.loc[is_ccs, "SK_CVA"] != 0))
    attrs = src.sort_values("_cva", ascending=False, kind="stable").groupby("NAME")[CCS_ATTRS].first()
    uen = uen_lookup(uen_map)
    prefix = mapping.get("cp_prefix", "CP:")
    cp_name = prefix + cr["COUNTERPARTY_ID"]
    name = cr["COUNTERPARTY_ID"].map(uen).fillna(cp_name)
    name = name.where(name.isin(attrs.index) | ~cp_name.isin(attrs.index), cp_name)
    cr["NAME"] = name
    drop_ccs = ~cr["NAME"].isin(attrs.index)
    if drop_ccs.any():
        exc.append(cr[drop_ccs].assign(issue="CCS: UEN / CP: name not among CVA_CALC_RF CCS names (dropped)")[EXC_COLS])
    cr = cr[~drop_ccs].join(attrs, on="NAME")
    cr = cr.assign(RISK_TYPE="CCS", RISK_FACTOR=cr["NAME"] + "_" + cr["TENOR"])
    cr["_FACTOR"] = cr["RISK_FACTOR"]

    out_keys = ["INTERNAL_ENTITY", "COUNTERPARTY_ID", "RISK_MEASURE", "RISK_TYPE", "RISK_SUB_BUCKET", "RISK_BUCKET",
                "RISK_FACTOR", "TENOR", "NAME", "CREDIT_RTG", "CREDIT_QUALITY", "CREDIT_RELATED", "RISK_WEIGHT", "_FACTOR"]
    cva = pd.concat([mkt[out_keys + ["SK_CVA"]], cr[out_keys + ["SK_CVA"]]], ignore_index=True)
    cva = cva.groupby(out_keys, sort=True, dropna=False)["SK_CVA"].sum().reset_index()
    cva = cva[cva["SK_CVA"] != 0]
    cva = cva.assign(CALC_RUN_ID=run_id, BUS_DT=bus_dt, SK_HEDGE=0.0, NW_SENSITIVITY=cva["SK_CVA"] * cva["RISK_WEIGHT"],
                     CCY=ccy)
    rf = pd.concat([cva[RF_COLUMNS], hdg[RF_COLUMNS]], ignore_index=True)

    # reconciliation per factor (CCS also per name) and per risk type
    rk = ["RISK_MEASURE", "RISK_TYPE", "RISK_SUB_BUCKET", "RISK_BUCKET", "_FACTOR"]
    a = calc.groupby(rk)["SK_CVA"].sum().rename("SK_CVA_CALC_RF")
    b = cva.groupby(rk)["SK_CVA"].sum().rename("SK_CVA_CPTY")
    recon = pd.concat([a, b], axis=1).fillna(0.0).reset_index().rename(columns={"_FACTOR": "RISK_FACTOR"})
    recon["DIFF"] = recon["SK_CVA_CPTY"] - recon["SK_CVA_CALC_RF"]
    recon = recon[(recon["SK_CVA_CALC_RF"] != 0) | (recon["SK_CVA_CPTY"] != 0)].reset_index(drop=True)
    summ = recon.groupby(["RISK_MEASURE", "RISK_TYPE"], as_index=False)[["SK_CVA_CALC_RF", "SK_CVA_CPTY", "DIFF"]].sum()
    summ["RATIO"] = summ["SK_CVA_CPTY"] / summ["SK_CVA_CALC_RF"].replace(0.0, np.nan)
    gross = recon.assign(_d=recon["DIFF"].abs(), _a=recon["SK_CVA_CALC_RF"].abs())
    g = gross.groupby(["RISK_MEASURE", "RISK_TYPE"], as_index=False)[["_d", "_a"]].sum()
    summ["ABS_DIFF_PCT"] = (g["_d"] / g["_a"].replace(0.0, np.nan)).to_numpy()
    exceptions = pd.concat(exc, ignore_index=True) if exc else pd.DataFrame(columns=EXC_COLS)
    return PrepResult(rf=rf, recon=recon, recon_summary=summ, exceptions=exceptions)


def _osfi_rw(r: pd.Series, params: dict[str, Any]) -> float:
    rc = RISK_TYPE_IN.get(r["RISK_TYPE"], "")
    measure = r["RISK_MEASURE"].lower()
    factor = r["_FACTOR"]
    if rc == "IR" and measure == "vega":
        factor = "IRVOL"
    elif rc != "IR":
        factor = ""
    if riskclass.validate(rc, measure, r["RISK_BUCKET"], factor, "", "", params):
        return np.nan
    return riskclass.risk_weight(rc, measure, r["RISK_BUCKET"], factor, "", params)


def write(res: PrepResult, out: str | Path) -> list[Path]:
    """Write <out> and its companions <stem>_recon.csv, <stem>_recon_summary.csv, <stem>_exceptions.csv."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    paths = [out] + [out.with_name(f"{out.stem}_{s}.csv") for s in ("recon", "recon_summary", "exceptions")]
    for p, df in zip(paths, (res.rf, res.recon, res.recon_summary, res.exceptions)):
        df.to_csv(p, index=False)
    return paths
