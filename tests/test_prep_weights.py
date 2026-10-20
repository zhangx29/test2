import copy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from sacva.counterparty import counterparty_rw, counterparty_table
from sacva.cva_calc import normalise_cva_calc_rf, read_input
from sacva.engine import capital
from sacva.loader import BookValidationError
from sacva.prep import build_cva_calc_rf, fx_multipliers, load_mapping
from sacva.weights import sacva_weights, write_weights

EXAMPLE = Path(__file__).parents[1] / "examples" / "cva_calc_rf.csv"
CADUSD = 0.702765
LD_TYPE = {("DELTA", "CCS"): "Risk_Credit", ("DELTA", "IR"): "Risk_IRCurve", ("VEGA", "IR"): "Risk_IRVol",
           ("DELTA", "FX"): "Risk_FX", ("VEGA", "FX"): "Risk_FXVol", ("DELTA", "EQ"): "Risk_Equity",
           ("VEGA", "EQ"): "Risk_EquityVol", ("DELTA", "CO"): "Risk_Commodity", ("VEGA", "CO"): "Risk_CommodityVol"}


def _feeds(unmapped_cp="MTL01"):
    """Split the counterparty-level example back into the bank feeds: a risk-factor-level CVA_CALC_RF (CVA and
    hedge netted on one row, as in production), CVA_LD_SENS in USD, the FX rate and the CPTY_CD -> UEN map."""
    ex = pd.read_csv(EXAMPLE, dtype=str, keep_default_na=False)
    for c in ("SK_CVA", "SK_HEDGE", "RISK_WEIGHT"):
        ex[c] = ex[c].astype(float)
    cva = ex[ex["SK_CVA"] != 0]
    # the unmapped counterparty is named CP:<CPTY_CD> in CVA_CALC_RF
    cp_name = cva.loc[cva["COUNTERPARTY_ID"] == unmapped_cp, "NAME"].iloc[0]
    ex.loc[ex["NAME"] == cp_name, "RISK_FACTOR"] = ex.loc[ex["NAME"] == cp_name, "RISK_FACTOR"].str.replace(
        cp_name, f"CP:{unmapped_cp}")
    ex.loc[ex["NAME"] == cp_name, "NAME"] = f"CP:{unmapped_cp}"
    cva = ex[ex["SK_CVA"] != 0]
    keys = ["CALC_RUN_ID", "BUS_DT", "RISK_MEASURE", "RISK_TYPE", "RISK_SUB_BUCKET", "RISK_BUCKET", "RISK_FACTOR",
            "TENOR", "NAME", "CREDIT_RTG", "CREDIT_QUALITY", "CREDIT_RELATED", "RISK_WEIGHT", "CCY"]
    calc = ex.groupby(keys, as_index=False)[["SK_CVA", "SK_HEDGE"]].sum()
    calc["NW_SENSITIVITY"] = calc["RISK_WEIGHT"] * (calc["SK_CVA"] + calc["SK_HEDGE"])
    ld = pd.DataFrame(dict(
        DATA_RUN_ID="620", BUS_DT=cva["BUS_DT"], INTERNAL_ENTITY=cva["INTERNAL_ENTITY"],
        COUNTERPARTY_ID=cva["COUNTERPARTY_ID"],
        RISK_TYPE=[LD_TYPE[k] for k in zip(cva["RISK_MEASURE"], cva["RISK_TYPE"])],
        TENOR=np.where(cva["RISK_TYPE"] == "CCS", cva["TENOR"],
                       np.where((cva["RISK_TYPE"] == "IR") & (cva["RISK_MEASURE"] == "DELTA"), cva["RISK_FACTOR"], "")),
        BUCKET=np.where(cva["RISK_TYPE"].isin(["EQ", "CO", "CCS"]), cva["RISK_BUCKET"], ""),
        QUALIFIER=np.where(cva["RISK_TYPE"].isin(["IR", "FX"]), cva["RISK_BUCKET"], ""),
        SENSITIVITY_AMOUNT=(cva["SK_CVA"] * CADUSD).map(repr), SENSITIVITY_AMOUNT_CURRENCY="USD",
        REGIME_LEGAL_ENTITY="BFG"))
    fx = pd.DataFrame([dict(BUS_DT="31-Aug-26", CCY_PAIR="CADUSD", FX_SRC="WSS", RATE=str(CADUSD), DATA_RUN_ID="620")])
    names = cva[cva["RISK_TYPE"] == "CCS"][["COUNTERPARTY_ID", "NAME"]].drop_duplicates()
    uen = names[names["COUNTERPARTY_ID"] != unmapped_cp].rename(columns={"NAME": "UEN", "COUNTERPARTY_ID": "CPTY_CD"})
    return ex, calc.astype(str), ld.astype(str), fx, uen.astype(str)


@pytest.fixture(scope="module")
def mapping():
    return load_mapping()


def test_fx_multipliers():
    fx = pd.DataFrame(dict(CCY_PAIR=["CADUSD", "EURCAD"], RATE=["0.702765", "1.5"]))
    m = fx_multipliers(fx, "CAD")
    assert m["USD"] == pytest.approx(1 / 0.702765) and m["EUR"] == 1.5 and m["CAD"] == 1.0


def test_prep_round_trip(params, mapping):
    ex, calc, ld, fx, uen = _feeds()
    res = build_cva_calc_rf(calc, ld, fx, uen, mapping, params)
    assert res.exceptions.empty
    assert res.recon["DIFF"].abs().max() < 1e-4
    assert np.allclose(res.recon_summary["RATIO"], 1.0)
    rf = res.rf
    # contract of the counterparty-level format: CVA and hedge rows separate, hedges unattributed
    assert ((rf["SK_CVA"] != 0) & (rf["SK_HEDGE"] != 0)).sum() == 0
    assert (rf.loc[rf["SK_HEDGE"] != 0, "COUNTERPARTY_ID"] == "").all()
    inp = normalise_cva_calc_rf(rf, params)
    assert inp.recon_breaks.empty
    orig = read_input(EXAMPLE, params).book
    assert capital(inp.book, params).total == pytest.approx(capital(orig, params).total, rel=1e-9)
    # unmapped CPTY_CD gets the CP: name, mapped ones their UEN
    ccs = rf[(rf["RISK_TYPE"] == "CCS") & (rf["SK_CVA"] != 0)]
    assert set(ccs.loc[ccs["COUNTERPARTY_ID"] == "MTL01", "NAME"]) == {"CP:MTL01"}
    assert (ccs["RISK_FACTOR"] == ccs["NAME"] + "_" + ccs["TENOR"]).all()
    # counterparty-level CVA per counterparty x type matches the example
    a = rf[rf["SK_CVA"] != 0].groupby(["COUNTERPARTY_ID", "RISK_TYPE"])["SK_CVA"].sum()
    b = ex[ex["SK_CVA"] != 0].groupby(["COUNTERPARTY_ID", "RISK_TYPE"])["SK_CVA"].sum()
    pd.testing.assert_series_equal(a, b, check_exact=False, rtol=1e-9)


def test_prep_ir_other_currency_and_exceptions(params, mapping):
    _, calc, ld, fx, uen = _feeds()
    brl = dict(RISK_MEASURE="DELTA", RISK_TYPE="IR", RISK_SUB_BUCKET="BRL", RISK_BUCKET="BRL", RISK_FACTOR="IR",
               TENOR="", NAME="", CREDIT_RTG="", CREDIT_QUALITY="", CREDIT_RELATED="", RISK_WEIGHT="0.0158",
               SK_CVA="-300", SK_HEDGE="0", NW_SENSITIVITY="-4.74", CALC_RUN_ID="1", BUS_DT="31-Aug-26", CCY="CAD")
    calc = pd.concat([calc, pd.DataFrame([brl])], ignore_index=True)
    row = ld.iloc[0].to_dict()
    extra = [dict(row, COUNTERPARTY_ID="EIB01", RISK_TYPE="Risk_IRCurve", TENOR=t, BUCKET="", QUALIFIER="BRL",
                  SENSITIVITY_AMOUNT=str(-150 * CADUSD)) for t in ("2Y", "10Y")]
    extra += [dict(row, COUNTERPARTY_ID="EIB01", RISK_TYPE="Risk_Inflation"),
              dict(row, COUNTERPARTY_ID="ZZZ99", RISK_TYPE="Risk_Credit", TENOR="5Y", BUCKET="", QUALIFIER=""),
              dict(row, COUNTERPARTY_ID="EIB01", RISK_TYPE="Risk_FX", QUALIFIER="NOKCAD", BUCKET="", TENOR="",
                   SENSITIVITY_AMOUNT="7")]
    ld = pd.concat([ld, pd.DataFrame(extra)], ignore_index=True)
    res = build_cva_calc_rf(calc, ld, fx, uen, mapping, params)
    brl_rows = res.rf[res.rf["RISK_BUCKET"] == "BRL"]
    cva_brl = brl_rows[brl_rows["SK_CVA"] != 0]
    assert len(cva_brl) == 1 and cva_brl["RISK_FACTOR"].iloc[0] == "PARALLEL"
    assert cva_brl["SK_CVA"].iloc[0] == pytest.approx(-300) and cva_brl["RISK_WEIGHT"].iloc[0] == 0.0158
    issues = set(res.exceptions["issue"])
    assert "RISK_TYPE not in mapping" in issues
    assert any(i.startswith("CCS: UEN") for i in issues)
    assert "factor not in CVA_CALC_RF: OSFI RW used" in issues    # NOKCAD absent from CVA_CALC_RF
    assert normalise_cva_calc_rf(res.rf, params).book is not None  # 'PARALLEL' and 'IR' both load


def test_prep_rejects_ambiguous_uen(params, mapping):
    _, calc, ld, fx, uen = _feeds()
    uen = pd.concat([uen, uen.head(1).assign(UEN="OTHER")], ignore_index=True)
    with pytest.raises(BookValidationError, match="several UENs"):
        build_cva_calc_rf(calc, ld, fx, uen, mapping, params)


# ---- SACVAWeights ---------------------------------------------------------------------

@pytest.fixture(scope="module")
def weights_run(params, policy_cfg):
    cfg = copy.deepcopy(policy_cfg)
    cfg["sacva_weights"]["own_lines"] = {"WB001": "unhedged_delta", "BBA01": "name_hedge", "NOPE": "name_hedge"}
    book = read_input(EXAMPLE, params).book
    return book, cfg, sacva_weights(book, params, cfg)


def test_weights_layout(weights_run, tmp_path):
    _, cfg, res = weights_run
    w = res["weights"]
    assert list(w.columns) == ["cptyCode", "hedgeType", "bucket", "igHy", "vegaCost", "6m", "1y", "3y", "5y", "10y"]
    assert len(w) == 48 + 3
    sect = w.iloc[:48]
    assert list(sect["hedgeType"].unique()) == ["specfic_hedge", "sector_hedge", "unhedged"]
    assert list(sect["bucket"].iloc[:8]) == ["1_A", "1_B", "2", "3", "4", "5", "6", "7"]
    assert list(sect["igHy"].iloc[[0, 8]]) == ["IG", "HY"]
    assert (sect["cptyCode"] == "NA").all()
    own = w.iloc[48:]
    assert list(own["cptyCode"]) == ["WB001", "BBA01", "NOPE"]
    assert (own[["hedgeType", "bucket", "igHy"]] == "NA").all().all()
    path = write_weights(w, tmp_path / "SACVAWeights.csv")
    lines = path.read_text().splitlines()
    assert lines[0] == "cptyCode,hedgeType,bucket,igHy,vegaCost,6m,1y,3y,5y,10y"
    assert lines[1] == "string,string,object,string,double?,double?,double?,double?,double?,double?"
    assert lines[2].startswith("NA,specfic_hedge,1_A,IG,") and lines[-1].startswith("NOPE,NA,NA,NA,")


def test_weights_values(weights_run, params):
    book, cfg, res = weights_run
    w, d = res["weights"], res["detail"]
    # every cell = gross-weighted incremental RW of the counterparties under that policy
    attrs = counterparty_table(book, cfg)
    r = counterparty_rw(book, params, cfg, attrs=attrs.assign(policy="sector_hedge"))
    c = r[(r["risk_class"] == "CCS") & (r["sector"] == "2") & (r["quality"] == "IG")]
    v = r[(r["measure"] == "vega") & (r["sector"] == "2") & (r["quality"] == "IG")]
    row = w[(w["hedgeType"] == "sector_hedge") & (w["bucket"] == "2") & (w["igHy"] == "IG")].iloc[0]
    assert row["5y"] == pytest.approx((c["incremental_rw"] * c["gross_sens_user"]).sum() / c["gross_sens_user"].sum())
    assert row["vegaCost"] == pytest.approx((v["incremental_rw"] * v["gross_sens_user"]).sum() / v["gross_sens_user"].sum())
    assert (w.iloc[:48][["6m", "1y", "3y", "5y", "10y"]].nunique(axis=1) == 1).all()
    # vega: 90% hedged (sector / name hedge) far below unhedged; CCS: unhedged above hedged
    sect = w.iloc[:48].set_index(["hedgeType", "bucket", "igHy"]).sort_index()
    assert (sect.loc["unhedged", "vegaCost"] > 5 * sect.loc["sector_hedge", "vegaCost"]).all()
    assert (sect.loc["unhedged", "5y"] >= sect.loc["sector_hedge", "5y"] - 1e-9).all()
    # empty cells (e.g. 1_A HY) come from the new-name grid
    assert d.loc[(d["hedgeType"] == "unhedged") & (d["bucket"] == "1_A") & (d["igHy"] == "HY"), "ccs_source"].iloc[0] \
        == "new-name grid"
    own = d.set_index("cptyCode")
    assert own.loc["WB001", "ccs_source"] == "counterparty" and own.loc["WB001", "vegaCost"] > 50
    assert own.loc["NOPE", "ccs_source"] == "none: COUNTERPARTY_ID not in book" and np.isnan(own.loc["NOPE", "ccs_rw"])
