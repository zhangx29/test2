"""Bank CVA_CALC_RF input format and CVA_CALC_RB / CVA_CALC_RT output tables.

Input (one row per risk factor, sensitivities already normalised: dV/0.0001 for bp factors, dV/0.01
for 1% relative factors, converted to CCY):

    CALC_RUN_ID, BUS_DT, RISK_MEASURE (DELTA|VEGA), RISK_TYPE (CCS|IR|FX|EQ|CO|RCS), RISK_SUB_BUCKET
    (CCS 1_A/1_B/2..8), RISK_BUCKET, RISK_FACTOR, TENOR, NAME (counterparty id), CREDIT_RTG,
    CREDIT_QUALITY (IG|HYNR), CREDIT_RELATED (parent entity id), RISK_WEIGHT, SK_CVA, SK_HEDGE,
    NW_SENSITIVITY, CCY

Counterparty granularity (optional columns INTERNAL_ENTITY, COUNTERPARTY_ID): CVA rows carry both ids with
SK_CVA != 0 and SK_HEDGE = 0; hedge rows (macro hedges, not attributable) leave both blank with SK_CVA = 0 and
SK_HEDGE != 0. Without these columns, CCS CVA rows are attributed to their NAME and market CVA rows are
unattributed.

Sign convention of this format: NW_SENSITIVITY = RISK_WEIGHT * (SK_CVA + SK_HEDGE), i.e. CVA is expressed
as a negative value and hedges offset by addition. OSFI (MAR50.53) writes WS = RW*(s_CVA - s_Hdg) with CVA
positive, so s_CVA = -SK_CVA, s_Hdg = SK_HEDGE and WS_OSFI = -NW_SENSITIVITY. Capital is invariant to this
global sign; S_B is reported back in the input convention (S_B = sum of NW, clipped by K_B).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import riskclass
from .engine import FACTOR_KEY, CapitalResult, aggregate
from .loader import BookValidationError, normalise

RF_COLUMNS = ["CALC_RUN_ID", "BUS_DT", "INTERNAL_ENTITY", "COUNTERPARTY_ID", "RISK_MEASURE", "RISK_TYPE", "RISK_SUB_BUCKET", "RISK_BUCKET",
              "RISK_FACTOR", "TENOR", "NAME", "CREDIT_RTG", "CREDIT_QUALITY", "CREDIT_RELATED",
              "RISK_WEIGHT", "SK_CVA", "SK_HEDGE", "NW_SENSITIVITY", "CCY"]
_REQUIRED = ["RISK_MEASURE", "RISK_TYPE", "RISK_BUCKET", "SK_CVA", "SK_HEDGE"]
_NULLS = {"", "[NULL]", "NULL", "NAN", "NONE", "<NA>"}

RISK_TYPE_IN = {"CCS": "CCS", "IR": "IR", "FX": "FX", "EQ": "EQ", "CO": "CMDTY", "CMDTY": "CMDTY", "RCS": "RCS"}
RISK_TYPE_OUT = {"CCS": "CCS", "IR": "IR", "FX": "FX", "EQ": "EQ", "CMDTY": "CO", "RCS": "RCS"}
IR_DELTA_ALIASES = {"INFLATION": "INFL", "CURVE": "PARALLEL", "IR": "PARALLEL"}   # IR: other-currency curve
IR_VEGA_IN = {"IR": "IRVOL", "IRVOL": "IRVOL", "INFL": "INFLVOL", "INFLATION": "INFLVOL", "INFLVOL": "INFLVOL"}
IR_VEGA_OUT = {"IRVOL": "IR", "INFLVOL": "INFL"}
QUALITY_IN = {"IG": "IG", "HYNR": "HY", "HY": "HY", "NR": "HY"}


@dataclass
class InputBook:
    book: pd.DataFrame                 # normalised internal book (OSFI sign convention)
    fmt: str                           # "cva_calc_rf" or "generic"
    calc_run_id: str = ""
    bus_dt: Any = ""
    ccy: str = ""
    ws_sign: int = 1                   # input-convention NW = ws_sign * internal WS
    recon: pd.DataFrame = field(default_factory=pd.DataFrame)
    name_conflicts: pd.DataFrame = field(default_factory=pd.DataFrame)   # NAME, attribute, values, resolved

    @property
    def recon_breaks(self) -> pd.DataFrame:
        if self.recon.empty:
            return self.recon
        return self.recon[~(self.recon["RW_OK"] & self.recon["NW_OK"])]


def is_cva_calc_rf(columns) -> bool:
    cols = {str(c).strip().upper() for c in columns}
    return {"SK_CVA", "SK_HEDGE"} <= cols


def _clean(v: Any) -> str:
    s = "" if v is None or (isinstance(v, float) and np.isnan(v)) else str(v).strip()
    if s.upper() in _NULLS:
        return ""
    if s.endswith(".0") and s[:-2].lstrip("-").isdigit():
        s = s[:-2]
    return s


def _num(v: Any) -> float:
    s = _clean(v).replace(",", "")
    return float(s) if s else 0.0


def _map_row(r: pd.Series, p: dict[str, Any]) -> dict[str, str]:
    """Internal keys for one CVA_CALC_RF row (raises KeyError/ValueError with a reason)."""
    rt = _clean(r["RISK_TYPE"]).upper()
    if rt not in RISK_TYPE_IN:
        raise ValueError(f"unknown RISK_TYPE '{rt}' (expected one of {sorted(RISK_TYPE_IN)})")
    rc = RISK_TYPE_IN[rt]
    measure = _clean(r["RISK_MEASURE"]).lower()
    sub = _clean(r.get("RISK_SUB_BUCKET", "")).upper()
    bucket = _clean(r["RISK_BUCKET"]).upper()
    factor = _clean(r.get("RISK_FACTOR", "")).upper()
    out = dict(risk_class=rc, measure=measure, bucket=bucket, risk_factor="", name="", quality="", group="")
    if rc == "CCS":
        out["bucket"] = {"1_A": "1a", "1A": "1a", "1_B": "1b", "1B": "1b"}.get(sub, sub or bucket).lower()
        out["risk_factor"] = _clean(r.get("TENOR", "")).upper()
        out["name"] = _clean(r.get("NAME", ""))
        q = _clean(r.get("CREDIT_QUALITY", "")).upper()
        if q not in QUALITY_IN:
            raise ValueError(f"CREDIT_QUALITY '{q}' must be IG or HYNR")
        out["quality"] = QUALITY_IN[q]
        # parent entity id; a parent appearing as a NAME matches its children through its own id
        out["group"] = _clean(r.get("CREDIT_RELATED", "")) or out["name"]
    elif rc == "IR":
        out["risk_factor"] = IR_VEGA_IN.get(factor, factor) if measure == "vega" else IR_DELTA_ALIASES.get(factor, factor)
    return out


def normalise_cva_calc_rf(raw: pd.DataFrame, params: dict[str, Any], tol: float = 1e-6) -> InputBook:
    df = raw.copy()
    df.columns = [str(c).strip().upper() for c in df.columns]
    missing = [c for c in _REQUIRED if c not in df.columns]
    if missing:
        raise BookValidationError(f"CVA_CALC_RF input missing columns {missing}")
    errors, records, recon = [], [], []
    by_cpty = "COUNTERPARTY_ID" in df.columns
    for i, r in df.iterrows():
        rowno = i + 2
        try:
            k = _map_row(r, params)
        except ValueError as e:
            errors.append(f"row {rowno}: {e}")
            continue
        msg = riskclass.validate(k["risk_class"], k["measure"], k["bucket"], k["risk_factor"], k["name"], k["quality"], params)
        if msg:
            errors.append(f"row {rowno}: {msg}")
            continue
        sk_cva, sk_hdg = _num(r["SK_CVA"]), _num(r["SK_HEDGE"])
        cpty = ""
        if by_cpty:
            ie, cp = _clean(r.get("INTERNAL_ENTITY", "")), _clean(r["COUNTERPARTY_ID"])
            if sk_cva != 0.0 and sk_hdg != 0.0:
                errors.append(f"row {rowno}: CVA and hedge sensitivities must be on separate rows")
                continue
            if sk_cva != 0.0 and not cp:
                errors.append(f"row {rowno}: CVA row without COUNTERPARTY_ID")
                continue
            if sk_hdg != 0.0 and (ie or cp):
                errors.append(f"row {rowno}: hedge row must leave INTERNAL_ENTITY / COUNTERPARTY_ID blank")
                continue
            cpty = f"{ie}|{cp}" if sk_cva != 0.0 else ""
        rw = riskclass.risk_weight(k["risk_class"], k["measure"], k["bucket"], k["risk_factor"], k["quality"], params)
        rw_in = _num(r["RISK_WEIGHT"]) if "RISK_WEIGHT" in df.columns and _clean(r["RISK_WEIGHT"]) else np.nan
        nw_in = _num(r["NW_SENSITIVITY"]) if "NW_SENSITIVITY" in df.columns and _clean(r["NW_SENSITIVITY"]) else np.nan
        nw = rw * (sk_cva + sk_hdg)
        recon.append(dict(ROW=rowno, RISK_MEASURE=k["measure"].upper(), RISK_TYPE=_clean(r["RISK_TYPE"]).upper(),
                          RISK_BUCKET=k["bucket"], RISK_FACTOR=k["risk_factor"], NAME=k["name"],
                          RISK_WEIGHT_INPUT=rw_in, RISK_WEIGHT_OSFI=rw,
                          NW_INPUT=nw_in, NW_RECALC=nw,
                          RW_OK=bool(np.isnan(rw_in) or abs(rw_in - rw) <= 1e-12),
                          NW_OK=bool(np.isnan(nw_in) or abs(nw_in - nw) <= max(0.01, tol * abs(nw)))))
        base = dict(k, series="", amount_unit="per_unit", policy="", counterparty=cpty,
                    _parent=_clean(r.get("CREDIT_RELATED", "")) if k["risk_class"] == "CCS" else "")
        if sk_cva != 0.0:
            records.append(dict(base, source="CVA", amount=-sk_cva))
        if sk_hdg != 0.0:
            records.append(dict(base, source="HDG", amount=sk_hdg))
    if errors:
        raise BookValidationError("invalid CVA_CALC_RF rows:\n  " + "\n  ".join(errors[:50]))
    conflicts = pd.DataFrame(columns=["NAME", "attribute", "values", "resolved"])
    if records:
        rec_df, conflicts = _harmonise_names(pd.DataFrame(records))
        records = rec_df.drop(columns="_parent").to_dict("records")
    book = normalise(pd.DataFrame(records), params) if records else normalise(
        pd.DataFrame(columns=["source", "risk_class", "measure", "amount"]), params)

    def single(col: str) -> Any:
        if col not in df.columns:
            return ""
        vals = {_clean(v) for v in df[col]} - {""}
        if len(vals) > 1:
            raise BookValidationError(f"input mixes several {col} values: {sorted(vals)[:5]}")
        return df[col].iloc[0] if vals else ""

    return InputBook(book=book, fmt="cva_calc_rf", calc_run_id=_clean(single("CALC_RUN_ID")),
                     bus_dt=single("BUS_DT"), ccy=_clean(single("CCY")), ws_sign=-1, recon=pd.DataFrame(recon),
                     name_conflicts=conflicts)


def _harmonise_names(rec: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """One set of attributes per CCS NAME across its CVA and hedge rows.

    CVA and hedge rows for the same name come from different feeds (e.g. a CDS hedge row without
    CREDIT_RELATED). Per name: the parent is the populated CREDIT_RELATED (falling back to the name itself);
    quality and sub-bucket are taken from the CVA rows when present. Disagreements are reported rather than
    resolved silently by row order.
    """
    rec = rec.copy()
    conflicts = []
    ccs = rec["risk_class"] == "CCS"
    for name, g in rec[ccs].groupby("name", sort=True):
        pref = g.index[g["source"] == "CVA"] if (g["source"] == "CVA").any() else g.index

        def pick(values: pd.Series, label: str) -> str:
            preferred = values[values.index.isin(pref)]
            choice = (preferred if len(preferred) else values).mode().iloc[0]
            distinct = sorted(set(values))
            if len(distinct) > 1:
                conflicts.append(dict(NAME=name, attribute=label, values=", ".join(distinct), resolved=choice))
            return choice

        parents = g["_parent"][(g["_parent"] != "") & (g["_parent"] != name)]
        parent = pick(parents, "CREDIT_RELATED") if len(parents) else ""
        rec.loc[g.index, "group"] = parent or name
        rec.loc[g.index, "quality"] = pick(g["quality"], "CREDIT_QUALITY")
        rec.loc[g.index, "bucket"] = pick(g["bucket"], "RISK_SUB_BUCKET")
    return rec, pd.DataFrame(conflicts, columns=["NAME", "attribute", "values", "resolved"])


def read_input(path: str | Path, params: dict[str, Any], sheet: str | None = None) -> InputBook:
    """Read a CVA_CALC_RF file (auto-detected) or a generic sensitivity file."""
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        if sheet is None:
            names = pd.ExcelFile(path).sheet_names
            sheet = "CVA_CALC_RF" if "CVA_CALC_RF" in names else names[0]
        raw = pd.read_excel(path, sheet_name=sheet, dtype=object)
    else:
        raw = pd.read_csv(path, dtype=str, keep_default_na=False)
    if is_cva_calc_rf(raw.columns):
        return normalise_cva_calc_rf(raw, params)
    return InputBook(book=normalise(raw, params), fmt="generic")


def to_cva_calc_rf(book: pd.DataFrame, params: dict[str, Any], calc_run_id: str = "", bus_dt: Any = "",
                   ccy: str | None = None) -> pd.DataFrame:
    """Write an internal book in CVA_CALC_RF format: one CVA row per counterparty and risk factor
    (INTERNAL_ENTITY / COUNTERPARTY_ID populated) and one hedge row per risk factor (ids blank)."""
    f = aggregate(book, params).set_index(FACTOR_KEY)
    b = book.copy()
    b["counterparty"] = np.where(b["source"] == "CVA", b["counterparty"], "")
    g = b.groupby(["source", "counterparty"] + FACTOR_KEY, sort=True, dropna=False)["amount"].sum().reset_index()
    out = []
    for r in g.itertuples(index=False):
        a = f.loc[tuple(getattr(r, k) for k in FACTOR_KEY)]
        ie, _, cp = r.counterparty.partition("|") if "|" in r.counterparty else ("", "", r.counterparty)
        sub = {"1a": "1_A", "1b": "1_B"}.get(r.bucket, r.bucket)
        is_ccs = r.risk_class == "CCS"
        if is_ccs:
            factor, tenor, bucket = f"{r.name}_{r.risk_factor}", r.risk_factor, riskclass.agg_bucket("CCS", r.bucket)
        elif r.risk_class == "IR":
            factor = IR_VEGA_OUT.get(r.risk_factor, r.risk_factor) if r.measure == "vega" else r.risk_factor
            tenor, bucket = "", r.bucket
        else:
            factor, tenor, bucket = RISK_TYPE_OUT[r.risk_class], "", r.bucket
        sk_cva = -r.amount if r.source == "CVA" else 0.0
        sk_hdg = r.amount if r.source == "HDG" else 0.0
        out.append(dict(CALC_RUN_ID=calc_run_id, BUS_DT=bus_dt, INTERNAL_ENTITY=ie, COUNTERPARTY_ID=cp,
                        RISK_MEASURE=r.measure.upper(), RISK_TYPE=RISK_TYPE_OUT[r.risk_class],
                        RISK_SUB_BUCKET=sub.upper() if is_ccs else sub, RISK_BUCKET=bucket, RISK_FACTOR=factor,
                        TENOR=tenor, NAME=r.name if is_ccs else "", CREDIT_RTG=a["quality"] if is_ccs else "",
                        CREDIT_QUALITY={"HY": "HYNR"}.get(a["quality"], a["quality"]) if is_ccs else "",
                        CREDIT_RELATED=(a["group"] if is_ccs and a["group"] != r.name else ""),
                        RISK_WEIGHT=a["rw"], SK_CVA=sk_cva, SK_HEDGE=sk_hdg,
                        NW_SENSITIVITY=a["rw"] * (sk_cva + sk_hdg), CCY=ccy or params["reporting_currency"]))
    df = pd.DataFrame(out, columns=RF_COLUMNS)
    if ((df["SK_CVA"] != 0) & (df["COUNTERPARTY_ID"] == "")).any():
        # some CVA is not attributed to a counterparty: write the risk-factor-level format
        df = df.drop(columns=["INTERNAL_ENTITY", "COUNTERPARTY_ID"])
    return df


def calc_tables(result: CapitalResult, inp: InputBook) -> dict[str, pd.DataFrame]:
    """CVA_CALC_RB (per risk bucket) and CVA_CALC_RT (per risk type) in the input sign convention."""
    def head(df: pd.DataFrame) -> pd.DataFrame:
        df.insert(0, "BUS_DT", inp.bus_dt)
        df.insert(0, "CALC_RUN_ID", inp.calc_run_id)
        return df

    b = result.by_bucket
    rb = head(pd.DataFrame({"RISK_MEASURE": b["measure"].str.upper(), "RISK_TYPE": b["risk_class"].map(RISK_TYPE_OUT),
                            "RISK_BUCKET": b["bucket"], "S_B": inp.ws_sign * b["S_b"], "K_B": b["K_b"]}))
    c = result.by_class
    rt = head(pd.DataFrame({"RISK_MEASURE": c["measure"].str.upper(), "RISK_TYPE": c["risk_class"].map(RISK_TYPE_OUT),
                            "K_RT": c["K"]}))
    order = {"DELTA": 0, "VEGA": 1}
    rb = rb.sort_values(["RISK_MEASURE", "RISK_TYPE"], key=lambda s: s.map(order) if s.name == "RISK_MEASURE" else s,
                        kind="stable").reset_index(drop=True)
    rt = rt.sort_values(["RISK_MEASURE", "RISK_TYPE"], key=lambda s: s.map(order) if s.name == "RISK_MEASURE" else s,
                        kind="stable").reset_index(drop=True)
    return {"CVA_CALC_RB": rb, "CVA_CALC_RT": rt}
