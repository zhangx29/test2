"""Loading, normalising and validating sensitivity books.

Canonical columns of a normalised book (one row per sensitivity):

source       CVA | HDG
risk_class   IR | FX | CCS | RCS | EQ | CMDTY
measure      delta | vega
bucket       currency (IR/FX), bucket id (CCS 1a,1b,2..8; RCS 1..17; EQ 1..13; CMDTY 1..11)
risk_factor  IR tenor / INFL / PARALLEL / IRVOL / INFLVOL, CCS tenor; blank for single-factor classes
name         CCS entity (counterparty, hedge reference name or index family); blank otherwise
quality      IG | HY  (NR is mapped to HY; CCS only)
group        legal-entity group id for 'legally related' names (CCS)
series       index series (CCS bucket 8)
amount       sensitivity in regulatory units (dV per unit absolute / relative shift)
policy       optional hedging policy override (CVA CCS rows)
counterparty counterparty the CVA row belongs to ("<INTERNAL_ENTITY>|<COUNTERPARTY_ID>" for CVA_CALC_RF input;
             defaults to the CCS name for CCS CVA rows); blank for hedges (macro, not attributed)

Input ``amount_unit``: per_unit (already regulatory), per_bp (x 1e4), per_1pct (x 1e2).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from . import riskclass

COLUMNS = ["source", "risk_class", "measure", "bucket", "risk_factor", "name", "quality",
           "group", "series", "amount", "policy", "counterparty"]
UNIT_SCALE = {"per_unit": 1.0, "per_bp": 1e4, "per_1pct": 1e2}


class BookValidationError(ValueError):
    pass


def _clean_bucket(v: Any) -> str:
    s = str(v).strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def normalise(raw: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
    """Normalise and validate a raw sensitivity table. Raises BookValidationError listing bad rows."""
    df = raw.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    for col in ("source", "risk_class", "measure", "amount"):
        if col not in df.columns:
            raise BookValidationError(f"missing required column '{col}'")
    for col in ("bucket", "risk_factor", "name", "quality", "group", "series", "policy", "counterparty"):
        if col not in df.columns:
            df[col] = ""
    if "amount_unit" not in df.columns:
        df["amount_unit"] = "per_unit"

    def s(col: str) -> pd.Series:
        return df[col].fillna("").astype(str).str.strip()

    df["source"] = s("source").str.upper().replace({"HEDGE": "HDG"})
    df["risk_class"] = s("risk_class").str.upper()
    df["measure"] = s("measure").str.lower()
    df["bucket"] = df["bucket"].fillna("").map(_clean_bucket)
    df["risk_factor"] = s("risk_factor").str.upper()
    df["name"] = s("name")
    df["quality"] = s("quality").str.upper().replace({"NR": "HY"})
    df["group"] = s("group")
    df["series"] = df["series"].fillna("").map(_clean_bucket)
    df["policy"] = s("policy")
    df["counterparty"] = s("counterparty")
    unit = s("amount_unit").str.lower().replace({"": "per_unit"})
    df["bucket"] = df.apply(lambda r: r["bucket"].upper() if r["risk_class"] in ("IR", "FX") else r["bucket"].lower(), axis=1)

    errors: list[str] = []
    for i, r in df.iterrows():
        rowno = i + 2 if isinstance(i, int) else i  # header is line 1 in CSV/Excel
        if r["source"] not in ("CVA", "HDG"):
            errors.append(f"row {rowno}: source must be CVA or HDG, got '{r['source']}'")
            continue
        if unit[i] not in UNIT_SCALE:
            errors.append(f"row {rowno}: amount_unit '{unit[i]}' not in {list(UNIT_SCALE)}")
            continue
        msg = riskclass.validate(r["risk_class"], r["measure"], r["bucket"], r["risk_factor"],
                                 r["name"], r["quality"], params)
        if msg:
            errors.append(f"row {rowno}: {msg}")
    if errors:
        raise BookValidationError("invalid sensitivity rows:\n  " + "\n  ".join(errors[:50]))

    df["amount"] = pd.to_numeric(df["amount"], errors="raise").astype(float) * unit.map(UNIT_SCALE)
    single = df["risk_class"].isin(riskclass.SINGLE_FACTOR_CLASSES)
    df.loc[single, "risk_factor"] = ""
    non_ccs = df["risk_class"] != "CCS"
    df.loc[non_ccs, ["name", "quality", "group", "series"]] = ""
    df.loc[df["source"] == "HDG", "counterparty"] = ""
    default_cpty = (df["source"] == "CVA") & ~non_ccs & (df["counterparty"] == "")
    df.loc[default_cpty, "counterparty"] = df.loc[default_cpty, "name"]
    return df[COLUMNS].reset_index(drop=True)


def load_book(path: str | Path, params: dict[str, Any], sheet: str | None = None) -> pd.DataFrame:
    """Load a sensitivity file: CVA_CALC_RF format (auto-detected) or the generic format above."""
    from .cva_calc import read_input  # local import: cva_calc builds on this module
    return read_input(path, params, sheet).book


def rows(records: Iterable[dict[str, Any]], params: dict[str, Any]) -> pd.DataFrame:
    """Build a normalised book from dicts (convenience for trades and tests)."""
    return normalise(pd.DataFrame(list(records)), params)


def empty_book() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=float if c == "amount" else object) for c in COLUMNS})
