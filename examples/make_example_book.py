"""Generate a synthetic counterparty-level book (examples/cva_calc_rf.csv) and a sample trade
(examples/trade_rf.csv) in the bank CVA_CALC_RF format.

* CVA rows: INTERNAL_ENTITY + COUNTERPARTY_ID populated, SK_CVA != 0, SK_HEDGE = 0 (all risk types).
* Hedge rows: INTERNAL_ENTITY / COUNTERPARTY_ID blank, SK_CVA = 0, SK_HEDGE != 0 (macro hedges).

Sizes below are written in $ per bp (CR01 / DV01) and $ per 1% move (FX / vol) for readability. The
output holds normalised sensitivities (dV / 0.0001 for bp factors, dV / 0.01 for 1% relative factors)
in the CVA_CALC_RF sign convention (CVA as a negative value).
"""
from pathlib import Path

import numpy as np

from sacva.cva_calc import to_cva_calc_rf
from sacva.loader import rows as to_book
from sacva.params import load_params

rng = np.random.default_rng(42)
HERE = Path(__file__).parent
P = load_params()
TENORS = ["0.5Y", "1Y", "3Y", "5Y", "10Y"]
TENOR_SHAPE = np.array([0.05, 0.10, 0.25, 0.35, 0.25])

# internal entity, counterparty id, CCS name, bucket, quality, parent, total CR01 ($/bp), market scale
COUNTERPARTIES = [
    ("BANK_CA", "WB001", "World Bank", "1a", "IG", "", 4000, 3.0),
    ("BANK_CA", "EIB01", "EIB", "1a", "IG", "", 2500, 2.0),
    ("BANK_US", "CHL01", "Republic of Chile", "1a", "IG", "", 1800, 1.0),
    ("BANK_CA", "ONT01", "Province of Ontario", "1b", "IG", "", 3500, 2.5),
    ("BANK_CA", "MTL01", "City of Montreal", "1b", "IG", "", 900, 0.5),
    ("BANK_CA", "BBA01", "Big Bank A", "2", "IG", "BBA", 6000, 4.0),
    ("BANK_US", "BBA02", "Big Bank A Securities", "2", "IG", "BBA", 1500, 1.5),
    ("BANK_CA", "INS01", "Insurer B", "2", "IG", "", 2200, 1.0),
    ("BANK_US", "HFC01", "Hedge Fund C", "2", "HY", "", 800, 1.0),
    ("BANK_CA", "PIP01", "Pipeline Co", "3", "IG", "", 3000, 1.5),
    ("BANK_CA", "MIN01", "Mining Co", "3", "HY", "", 1200, 0.8),
    ("BANK_US", "AIR01", "Airline Co", "4", "HY", "", 1000, 0.6),
    ("BANK_CA", "RET01", "Retailer Co", "4", "IG", "", 1400, 0.7),
    ("BANK_CA", "TEL01", "Telecom Co", "5", "IG", "", 2600, 1.2),
    ("BANK_CA", "UTI01", "Utility Co", "6", "IG", "", 3200, 2.0),
    ("BANK_CA", "PEN01", "Pension Fund D", "7", "IG", "", 2000, 1.5),
]

recs, totals = [], {}


def cva(cpty, **kw):
    recs.append(dict(source="CVA", counterparty=cpty, **kw))
    if kw["risk_class"] != "CCS":
        k = (kw["risk_class"], kw["measure"], kw["bucket"], kw.get("risk_factor", ""), kw["amount_unit"])
        totals[k] = totals.get(k, 0.0) + kw["amount"]


for ie, cp, name, b, q, grp, cr01, scale in COUNTERPARTIES:
    cpty = f"{ie}|{cp}"
    for t, w in zip(TENORS, TENOR_SHAPE * rng.uniform(0.8, 1.2, 5)):
        cva(cpty, risk_class="CCS", measure="delta", bucket=b, risk_factor=t, name=name, quality=q, group=grp,
            amount=round(cr01 * w, 2), amount_unit="per_bp")
    for ccy, ccy_scale in (("CAD", 1.0), ("USD", 0.8)):
        for t, w in zip(["1Y", "2Y", "5Y", "10Y", "30Y"], [0.1, 0.15, 0.35, 0.3, 0.1]):
            cva(cpty, risk_class="IR", measure="delta", bucket=ccy, risk_factor=t,
                amount=round(-1500 * scale * ccy_scale * w * rng.uniform(0.5, 1.5), 2), amount_unit="per_bp")
        cva(cpty, risk_class="IR", measure="vega", bucket=ccy, risk_factor="IRVOL",
            amount=round(2500 * scale * ccy_scale * rng.uniform(0.5, 1.5), 2), amount_unit="per_1pct")
    cva(cpty, risk_class="FX", measure="delta", bucket="USDCAD", amount=round(3500 * scale * rng.normal(1, 0.5), 2),
        amount_unit="per_1pct")
    cva(cpty, risk_class="FX", measure="vega", bucket="USDCAD", amount=round(700 * scale * rng.uniform(0.5, 1.5), 2),
        amount_unit="per_1pct")

# macro hedges (not attributed): rates delta 90%, rates vega 60%, FX delta 95%, FX vega 50% of the book
HEDGE = {("IR", "delta"): 0.9, ("IR", "vega"): 0.6, ("FX", "delta"): 0.95, ("FX", "vega"): 0.5}
for (rc, m, b, f, unit), amt in totals.items():
    recs.append(dict(source="HDG", risk_class=rc, measure=m, bucket=b, risk_factor=f,
                     amount=round(amt * HEDGE[(rc, m)], 2), amount_unit=unit))
# credit hedges: single-name protection on Big Bank A, index protection for the general population
recs.append(dict(source="HDG", risk_class="CCS", measure="delta", bucket="2", risk_factor="5Y", name="Big Bank A",
                 quality="IG", group="BBA", amount=2000, amount_unit="per_bp"))
for idx, q, amt in (("CDX.NA.IG", "IG", 9000), ("CDX.NA.HY", "HY", 1200)):
    recs.append(dict(source="HDG", risk_class="CCS", measure="delta", bucket="8", risk_factor="5Y", name=idx,
                     quality=q, series="1", amount=amt, amount_unit="per_bp"))

RUN = dict(calc_run_id="1", bus_dt="31-Aug-26")
to_cva_calc_rf(to_book(recs, P), P, **RUN).to_csv(HERE / "cva_calc_rf.csv", index=False)

trade = [dict(source="CVA", counterparty="BANK_CA|NEW01", risk_class="CCS", measure="delta", bucket="2",
              risk_factor="5Y", name="New Bank", quality="IG", amount=1.0, amount_unit="per_bp"),
         dict(source="CVA", counterparty="BANK_CA|NEW01", risk_class="IR", measure="vega", bucket="CAD",
              risk_factor="IRVOL", amount=1.0, amount_unit="per_1pct")]
to_cva_calc_rf(to_book(trade, P), P, **RUN).to_csv(HERE / "trade_rf.csv", index=False)
print(f"wrote {HERE / 'cva_calc_rf.csv'} and {HERE / 'trade_rf.csv'}")
