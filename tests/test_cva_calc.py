"""CVA_CALC_RF input / CVA_CALC_RB, CVA_CALC_RT output, reconciled to production figures (run 603008, 31-Aug-26)."""
from pathlib import Path

import pandas as pd
import pytest

from sacva.calibrate import ccs_representative_grid
from sacva.cva_calc import calc_tables, normalise_cva_calc_rf, read_input, to_cva_calc_rf
from sacva.engine import capital
from sacva.loader import BookValidationError, load_book

SAMPLE = Path(__file__).parent / "data" / "cva_calc_rf_sample.csv"


@pytest.fixture(scope="module")
def raw():
    return pd.read_csv(SAMPLE, dtype=str, keep_default_na=False)


@pytest.fixture(scope="module")
def inp(params):
    return read_input(SAMPLE, params)


def tables(inp, params):
    return calc_tables(capital(inp.book, params), inp)


def test_detects_format_and_metadata(inp):
    assert inp.fmt == "cva_calc_rf"
    assert (inp.calc_run_id, inp.bus_dt, inp.ccy, inp.ws_sign) == ("603008", "31-Aug-26", "CAD", -1)


def test_input_risk_weights_and_nw_reconcile(inp):
    assert len(inp.recon) == 54
    assert inp.recon_breaks.empty


def test_recon_flags_wrong_risk_weight(raw, params):
    bad = raw.copy()
    bad.loc[0, "RISK_WEIGHT"] = "0.01"   # 1_A IG must be 0.5%
    out = normalise_cva_calc_rf(bad, params)
    assert list(out.recon_breaks["ROW"]) == [2]


def test_sign_mapping_to_osfi_convention(inp):
    b = inp.book
    a = b[(b["name"] == "10000748") & (b["risk_factor"] == "5Y")].iloc[0]
    assert (a["source"], a["amount"], a["bucket"], a["quality"], a["group"]) == ("CVA", 1003804096.81, "1a", "IG", "257834")
    h = b[(b["name"] == "BBG:311698") & (b["risk_factor"] == "5Y")].iloc[0]
    assert (h["source"], h["amount"], h["quality"], h["group"]) == ("HDG", 61227378.63, "HY", "BBG:311698")


@pytest.mark.parametrize("measure,rtype,bucket,s_b,k_b", [
    ("DELTA", "CO", "2", -181921.81, 209776.61),
    ("DELTA", "CO", "6", -386338.19, 403156.58),
    ("DELTA", "CO", "7", -512904.44, 544437.42),
    ("DELTA", "EQ", "8", -889647.80, 889647.80),
    ("DELTA", "EQ", "12", -208686.90, 208686.90),
    ("DELTA", "FX", "USDCAD", -333787.47, 1006840.44),
])
def test_rb_matches_production(inp, params, measure, rtype, bucket, s_b, k_b):
    rb = tables(inp, params)["CVA_CALC_RB"].set_index(["RISK_MEASURE", "RISK_TYPE", "RISK_BUCKET"])
    row = rb.loc[(measure, rtype, bucket)]
    assert row["S_B"] == pytest.approx(s_b, abs=0.02)
    assert row["K_B"] == pytest.approx(k_b, abs=0.02)


def test_ccs_1a_1b_reported_as_bucket_1_in_input_sign(inp, params):
    rb = tables(inp, params)["CVA_CALC_RB"]
    ccs = rb[rb["RISK_TYPE"] == "CCS"]
    assert list(ccs["RISK_BUCKET"]) == ["1", "2"]
    nw1 = inp.recon[(inp.recon["RISK_TYPE"] == "CCS") & (inp.recon["RISK_BUCKET"].isin(["1a", "1b"]))]["NW_INPUT"].sum()
    s1, k1 = ccs.iloc[0]["S_B"], ccs.iloc[0]["K_B"]
    assert s1 == pytest.approx(max(-k1, min(nw1, k1)), abs=0.05)


def test_rt_matches_production_with_buckets_from_rb_screenshot(raw, params):
    # buckets present in production CVA_CALC_RB but not in the RF sample (single factor, no hedge: K_B = |S_B|)
    co, eq = raw.iloc[25].to_dict(), raw.iloc[27].to_dict()
    extra = [dict(co, RISK_SUB_BUCKET="3", RISK_BUCKET="3", RISK_WEIGHT="0.6", SK_CVA=str(57790.74 / 0.6), SK_HEDGE="0", NW_SENSITIVITY="57790.74"),
             dict(co, RISK_SUB_BUCKET="5", RISK_BUCKET="5", RISK_WEIGHT="0.4", SK_CVA=str(-140047.51 / 0.4), SK_HEDGE="0", NW_SENSITIVITY="-140047.51"),
             dict(eq, RISK_SUB_BUCKET="9", RISK_BUCKET="9", RISK_WEIGHT="0.7", SK_CVA=str(-1936.86 / 0.7), NW_SENSITIVITY="-1936.86")]
    full = normalise_cva_calc_rf(pd.concat([raw, pd.DataFrame(extra)], ignore_index=True), params)
    rt = tables(full, params)["CVA_CALC_RT"].set_index(["RISK_MEASURE", "RISK_TYPE"])["K_RT"]
    assert rt[("DELTA", "CO")] == pytest.approx(838415.81, abs=0.05)
    assert rt[("DELTA", "EQ")] == pytest.approx(1180549.32, abs=0.05)


def test_output_columns(inp, params):
    t = tables(inp, params)
    assert list(t["CVA_CALC_RB"].columns) == ["CALC_RUN_ID", "BUS_DT", "RISK_MEASURE", "RISK_TYPE", "RISK_BUCKET", "S_B", "K_B"]
    assert list(t["CVA_CALC_RT"].columns) == ["CALC_RUN_ID", "BUS_DT", "RISK_MEASURE", "RISK_TYPE", "K_RT"]


def test_round_trip_export(inp, params):
    out = to_cva_calc_rf(inp.book, params, "603008", "31-Aug-26")
    back = normalise_cva_calc_rf(out, params)
    assert back.recon_breaks.empty
    assert capital(back.book, params).total == pytest.approx(capital(inp.book, params).total, rel=1e-12)


def test_bad_row_reports_row_number(raw, params):
    bad = raw.copy()
    bad.loc[3, "CREDIT_QUALITY"] = "XX"
    with pytest.raises(BookValidationError, match="row 5: CREDIT_QUALITY"):
        normalise_cva_calc_rf(bad, params)


def test_load_book_autodetects_and_calibrates(params, policy_cfg):
    book = load_book(SAMPLE, params)
    g = ccs_representative_grid(book, params, policy_cfg)
    assert g[["incremental_eff_rw", "euler_eff_rw"]].notna().all().all()
