# sacva — SA-CVA marginal capital & effective risk-weight calibration (OSFI CAR Ch. 8)

Computes SA-CVA capital for a CVA + hedge sensitivity book under OSFI CAR Chapter 8, section 8.3
(Basel MAR50). The 2026 and 2027 versions of sections 8.3.3–8.3.10 are textually identical. It then calibrates *effective risk weights*: the SA-CVA capital consumed
per unit of new CVA sensitivity (e.g. per $1 CR01). The hedging policy applied to the name is taken
into account.

Design spec: `docs/superpowers/specs/2026-10-03-sa-cva-marginal-rw-design.md`.

## Quick start

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"     # (bin/ on Linux/macOS)
python examples/make_example_book.py                     # synthetic CVA_CALC_RF book + trade
sacva capital   --book examples/cva_calc_rf.csv --out out/cva_calc.xlsx      # CVA_CALC_RB / CVA_CALC_RT
sacva marginal  --book examples/cva_calc_rf.csv --trade examples/trade_rf.csv --policy specific_hedge
sacva calibrate --book examples/cva_calc_rf.csv --out out/rw_grid.xlsx       # or --out out/csv_dir
sacva weights   --book examples/cva_calc_rf.csv --out out/SACVAWeights.csv   # production pricing weights
pytest
```

Excel inputs are read from sheet `CVA_CALC_RF` when present (override with `--sheet`).

## Input format: CVA_CALC_RF (primary)

Sensitivities are already normalised (ΔV/0.0001 for bp factors, ΔV/0.01 for 1% relative factors),
mapped to sector and rating, and converted to CCY. CVA and hedge sensitivities are on separate rows:

* **CVA rows:** one per counterparty and risk factor. `INTERNAL_ENTITY` and `COUNTERPARTY_ID` are
  populated, `SK_CVA ≠ 0` and `SK_HEDGE = 0`, for all risk types (CCS and market).
* **Hedge rows:** one per risk factor. `INTERNAL_ENTITY` and `COUNTERPARTY_ID` are blank, `SK_CVA = 0`
  and `SK_HEDGE ≠ 0`. Hedges are macro hedges and are not attributed to counterparties.

Rows breaking this contract are rejected with their row numbers. Files without the two id columns (one
row per risk factor) are still accepted: CCS CVA is then attributed to its `NAME`, and market CVA is
unattributed.

| column | use |
|---|---|
| `CALC_RUN_ID`, `BUS_DT`, `CCY` | carried to the outputs (one value per file) |
| `INTERNAL_ENTITY`, `COUNTERPARTY_ID` | counterparty of a CVA row; the pair is the counterparty unit for marginal / Euler RWs. Capital is consolidated across internal entities |
| `RISK_MEASURE` | `DELTA` / `VEGA` |
| `RISK_TYPE` | `CCS`, `IR`, `FX`, `EQ`, `CO` (commodity), `RCS` |
| `RISK_SUB_BUCKET` | CCS sector incl. `1_A` / `1_B` (drives the RW). 1_A and 1_B aggregate as bucket 1 |
| `RISK_BUCKET` | aggregation bucket: CCS 1–8, IR currency, FX pair (`USDCAD`), EQ 1–13, CO 1–11 |
| `RISK_FACTOR` | IR delta tenor (`1Y 2Y 5Y 10Y 30Y`, `INFL`; `PARALLEL` for other currencies); IR vega `IR` / `INFL`. Ignored for CCS (NAME + TENOR are used) and single-factor types |
| `TENOR` | CCS tenor `0.5Y 1Y 3Y 5Y 10Y` |
| `NAME` | counterparty / hedge reference entity identifier (CCS) |
| `CREDIT_QUALITY` | `IG` or `HYNR`: selects the CCS risk weight and ρ_quality (`CREDIT_RTG` is informational) |
| `CREDIT_RELATED` | parent entity identifier: names with the same parent (or a name equal to another's parent) are "legally related" (ρ_name 90%) |
| `SK_CVA`, `SK_HEDGE` | CVA and hedge sensitivities in CCY |
| `RISK_WEIGHT`, `NW_SENSITIVITY` | reconciled, not used: RW is re-derived from OSFI tables and NW recomputed; mismatches go to a `RECON_BREAKS` sheet and a warning |

**Sign convention:** in this format NW_SENSITIVITY = RISK_WEIGHT × (SK_CVA + SK_HEDGE), with CVA
expressed as a negative value. OSFI (MAR50.53) writes WS = RW·(s^CVA − s^Hdg) with CVA positive, so
internally s^CVA = −SK_CVA and s^Hdg = SK_HEDGE. Capital is unaffected by this global sign. S_B is
reported back in the input convention, and a unit CCS trade means SK_CVA = −10,000 (adds CVA risk).

### Outputs of `sacva capital --out` (also included in `calibrate`)

| sheet | columns |
|---|---|
| `CVA_CALC_RB` | `CALC_RUN_ID, BUS_DT, RISK_MEASURE, RISK_TYPE, RISK_BUCKET, S_B, K_B` per risk bucket (e.g. CCS sectors 1–7) |
| `CVA_CALC_RT` | `CALC_RUN_ID, BUS_DT, RISK_MEASURE, RISK_TYPE, K_RT` per risk type (e.g. CCS DELTA) |
| `RECON_BREAKS` | only when input RISK_WEIGHT / NW_SENSITIVITY disagree with the OSFI-derived values |
| `NAME_CONFLICTS` | only when a CCS NAME carries different CREDIT_QUALITY / RISK_SUB_BUCKET / CREDIT_RELATED on different rows (e.g. CVA vs hedge feed); resolved from the CVA rows, and a blank parent never overrides a populated one |

## Building the input: `sacva prep`

```bash
sacva prep --calc-rf CVA_CALC_RF_20260930.csv --ld-sens CVA_LD_SENS_20260930.csv            --mkt-fx CVA_LD_MKT_FX_20260930.csv --uen-map CPTY_UEN.csv --out cva_calc_rf_20260930.csv
```

Builds the counterparty-level CVA_CALC_RF from the bank feeds, read with their own column names:

* **CVA_CALC_RF**: risk-factor level, with CVA and hedges already mapped and no counterparty. It supplies
  the hedge rows and the attribute / RW lookups.
* **CVA_LD_SENS**: counterparty-level CVA sensitivities before mapping.
* **CVA_LD_MKT_FX**: FX rates. `CADUSD 0.702765` means USD per 1 CAD.
* **CPTY_CD → UEN map**: `UEN, CPTY_CD`, the distinct pairs from CVA_LD_TRADE.

Static mappings (RISK_TYPE table, the 7 major IR currencies) are in `sacva/config/input_mapping.yaml`
(`--mapping` to override).

| output rows | rule |
|---|---|
| hedges | CVA_CALC_RF rows with `SK_HEDGE ≠ 0`. `SK_CVA = 0`, `NW = SK_HEDGE × RW`, ids blank |
| market CVA | `RISK_MEASURE` / `RISK_TYPE` from the RISK_TYPE mapping. `RISK_SUB_BUCKET = RISK_BUCKET` = `BUCKET` for CO/EQ, else `QUALIFIER`. `RISK_FACTOR` = `TENOR` for IR delta in AUD CAD EUR GBP JPY SEK USD, `PARALLEL` for other IR delta, else the RISK_TYPE (IR, FX, EQ, CO). TENOR / NAME / CREDIT_* blank. RW looked up in CVA_CALC_RF on measure + type + sub-bucket + bucket + factor (`IR` and `PARALLEL` both match an other-currency curve) |
| CCS CVA | UEN via `COUNTERPARTY_ID`, or `CP:<CPTY_CD>` when unmapped. Sub-bucket, bucket, CREDIT_RTG / QUALITY / RELATED and RW from the CVA_CALC_RF CCS rows with `NAME = UEN`. `NAME = UEN`, `RISK_FACTOR = <UEN>_<TENOR>` |
| all CVA | `SK_CVA` = `SENSITIVITY_AMOUNT` converted to CAD (× `cva_sign` × `amount_scale`, default 1). `SK_HEDGE = 0`, `NW = SK_CVA × RW`. Summed per INTERNAL_ENTITY, COUNTERPARTY_ID and factor |

Companion files:

* `<out>_recon.csv`: Σ counterparty SK_CVA vs CVA_CALC_RF SK_CVA, per factor.
* `<out>_recon_summary.csv`: the same per risk type, with `RATIO`.
* `<out>_exceptions.csv`: rows dropped, e.g. an unmapped RISK_TYPE or a UEN with no CCS row in CVA_CALC_RF,
  and factors priced at the OSFI RW because they are absent from CVA_CALC_RF.

`RATIO` should be 1 for every risk type:

| `RATIO` | meaning |
|---|---|
| ≈ 0.49 or ≈ 2.0 | FX direction is inverted |
| ≈ 1e4 or ≈ 1e2 | a missing normalisation (set `amount_scale`) |
| ≈ −1 | a sign difference (set `cva_sign`) |

## SACVAWeights.csv: `sacva weights`

Writes the production weight file:

* column header `cptyCode, hedgeType, bucket, igHy, vegaCost, 6m, 1y, 3y, 5y, 10y`;
* a type row `string, string, object, string, double?, …`;
* 48 sector rows: hedge type × IG/HY × `1_A, 1_B, 2…7`;
* one row per name in `sacva_weights.own_lines`.

Every counterparty is priced under each hedge type, regardless of its assigned policy (`sacva_weights.hedge_types`):

| hedgeType (production label) | policy | market delta | vega | CCS |
|---|---|---|---|---|
| `specfic_hedge` (sic) | `name_hedge` | 100% | 90% | 80%, own name |
| `sector_hedge` | `sector_hedge` | 100% | 90% | 80%, dummy same-sector name |
| `unhedged` | `unhedged_delta` | 100% | 0% | 0% |

* **Tenor columns** (equal across 6m…10y): Σ(incremental RW × gross CR01) / Σ gross CR01 over the counterparties
  in the sector × IG/HY cell (sector / quality of the counterparty's dominant CCS name). Per $1/bp CR01.
* **`vegaCost`**: the same over all vega risk types (IR, FX, EQ, CO). Per $1 per 1% vega.
* **Market delta RWs** are not part of the file.
* **Empty cells** fall back to the new-name CCS grid and the hedge type's portfolio-wide vegaCost.
* **`own_lines`** (`{COUNTERPARTY_ID: policy}`, e.g. IPL: name_hedge, the SSAs: unhedged_delta): one line each, with
  `hedgeType / bucket / igHy = NA`, priced under that policy. These counterparties also stay in their sector rows.

`<out>_detail.csv` gives the policy, the source (counterparties / new-name grid / fallback), the number of
counterparties and the gross sensitivity behind every value.

### Methodology switch: `current` vs `size_term`

`sacva_weights.method` in the policy config (or `sacva weights --method`) selects the pricing methodology:

| method | file | trade of CR01 x |
|---|---|---|
| `current` (default) | as above: one RW per $1 per cell | capital = RW × x |
| `size_term` | adds `eulerRw, standaloneRw, refRw, kEff, refSize` and `vegaEulerRw … vegaRefSize`; tenor columns and `vegaCost` carry `refRw` | capital = RW(x) × x |

`size_term` is the Euler base plus a size term (memo section 4, `sacva/sizeterm.py`). Per cell, e (the `rw` column,
by default the $1 incremental RW), s (stand-alone) and i_ref (incremental RW at `ref_size`, default $10k/bp and
$10k per 1% vega) are gross-CR01-weighted over the counterparties (or taken from the new-name grid), and

    K_eff = x_ref (s² − i_ref²) / (2 (i_ref − e))        RW(x) = (√(K_eff² + 2 x K_eff e + x² s²) − K_eff) / x

RW(x) runs from e at small x through i_ref at x_ref to s for large x. A cell with no curvature (i_ref ≤ e) uses the
class-level book K. `sacva price` applies either file to a trade:

```bash
sacva weights --book book.csv --out out/SACVAWeights.csv --method size_term
sacva price --weights out/SACVAWeights.csv --hedge-type sector_hedge --bucket 2 --ig-hy IG --cr01 25000 --vega 5000
sacva price --weights out/SACVAWeights.csv --cpty IPL --cr01 25000          # own line
```

`--floor f` floors RW(x) at f × stand-alone (off by default; memo 4.4). The output also shows x·s / K_eff: above
5–10%, run a full pre-deal `sacva marginal` instead.

## Memo figures on a real book

`python examples/memo_figures.py --book <CVA_CALC_RF> --out out/memo` regenerates every book-dependent number of
`docs/2026-10-20-sa-cva-rw-methodology.docx` (tables 3.4 and 4.2, the 3.7 exhibit, and the inline numbers in
`memo_numbers.csv` keyed by section). The 3.7 PNG needs matplotlib. `grid_cells.csv` holds every cell to help choose
the memo's example cells (`--table-cells`, `--curve-cells`, `--offset-cell`, `--cpty-cell`).

## Input format: generic (internal, used by tests)

| column | values |
|---|---|
| `source` | `CVA` (sensitivity of regulatory CVA, CVA expressed as a positive number) or `HDG` (sensitivity of the hedge's market value) |
| `risk_class` | `IR`, `FX`, `CCS` (counterparty credit spread), `RCS` (reference credit spread), `EQ`, `CMDTY` |
| `measure` | `delta` / `vega` (no CCS vega) |
| `bucket` | IR/FX: currency. CCS: `1a`,`1b`,`2`…`7`, `8` (qualified index). RCS 1–17, EQ 1–13, CMDTY 1–11 |
| `risk_factor` | IR delta: `1Y 2Y 5Y 10Y 30Y INFL` (specified currencies) or `PARALLEL INFL` (others). IR vega: `IRVOL INFLVOL`. CCS: `0.5Y 1Y 3Y 5Y 10Y`. Blank for FX/RCS/EQ/CMDTY |
| `name`, `quality`, `group`, `series` | CCS only: entity, `IG`/`HY`/`NR` (NR is treated as HY), legal-entity group (ρ_name 90%), index series (bucket 8) |
| `amount`, `amount_unit` | Default (column absent or blank) = `per_unit`: amounts already **normalised** as OSFI defines them, i.e. ΔV/0.0001 for bp factors (a $1/bp CR01 is entered as 10,000) and ΔV/0.01 for 1% relative factors (FX spot, all vols, EQ, CMDTY). `per_bp` (× 1e4) and `per_1pct` (× 1e2) are available for raw $/bp or $/1% inputs |
| `policy` | optional hedging-policy override for a counterparty (CVA_CALC_RF has no such column: use `assignment` in the policy config) |

**Sign convention (MAR50.53):** WS_k = RW_k·s_k^CVA − RW_k·s_k^Hdg. Bought protection on a
counterparty has a positive CCS hedge sensitivity and offsets the positive CVA sensitivity.
Enter hedge sensitivities with the sign of the hedge's own market value.

## Methodology

* **Capital:** per risk class and measure, compute K_b = sqrt(WSᵀρWS + R·Σ(WS^Hdg)²) with R = 0.01, then
  K = m_CVA·sqrt(ΣK_b² + Σγ S_b S_c) with m_CVA = 1, and sum across classes and measures. CCS buckets
  1a and 1b share bucket 1 for γ. All parameters are in `sacva/config/osfi_car_ch8.yaml`, with MAR refs.
* **Trade:** the unit CVA sensitivity plus the hedge legs generated by the hedging policy
  (`sacva/config/hedging_policy.yaml`).
* **Incremental marginal:** (K(book + size·trade) − K(book)) / size.
* **Euler marginal:** the directional derivative ∇K(book)·trade, using a closed-form gradient.
  Contributions sum to K. For a risk class with no book capital it equals the stand-alone value.
  It omits the second-order R·(WS^Hdg)² disallowance of hedge factors that are new to the book.
* **Stand-alone:** K(trade) with no book. Unhedged stand-alone effective RW = regulatory RW.
* **RW (`*_rw`)** = capital change per **$1 of user-unit sensitivity**: per $1/bp CR01 or DV01 (= 10,000
  normalised) for bp factors, per $1 per 1% move (= 100 normalised) for 1% factors. So capital ≈ RW × $
  sensitivity; for CCS delta the RW is the capital of a $1 CR01 (SK_CVA of 10,000).
* **Effective RW (`*_eff_rw`)** = capital change per 1 unit of *normalised* sensitivity = RW / 10,000 or / 100.
  It is directly comparable with the regulatory RW.

### Calibration outputs (`sacva calibrate`)

| sheet | content |
|---|---|
| `CPTY_RW` | per counterparty (`INTERNAL_ENTITY\|COUNTERPARTY_ID`) × risk type (e.g. CCS DELTA, IR DELTA, IR VEGA, FX DELTA): incremental, Euler and stand-alone RW per $1 (`*_rw`) and per normalised unit (`*_eff_rw`), the `unit`, gross sensitivity (normalised and in user units), policy, sector and IG/HY, and its Euler allocation |
| `CPTY_RW_CLUSTER` | `sector_hedge` counterparties clustered by sector × IG/HY × risk type; RW = Σ(RW × gross) / Σ gross. Other policies (e.g. SSA/MDB under `specific_hedge`) are priced per counterparty from `CPTY_RW` |
| `CPTY_EULER_RECON` | per risk type: K_RT = counterparty CVA Euler + unattributed CVA Euler + macro-hedge Euler. The macro hedge is split into the part implied by counterparty policies (inside the counterparty allocations) and the unallocated remainder |
| `CCS_Incremental_RW`, `CCS_Euler_RW`, `CCS_Standalone_RW` | RW per $1/bp CR01; new-name grid: policy × bucket × IG/HY for a representative **new** counterparty (ρ_name 50% to book names), tenor profile from config |
| `Market_Grid` | policy × representative factor (`calibration.representative_factors`) × `direction` (delta priced for +1 and −1 units, vega for +1: `calibration.market_directions`) |
| `Euler_Contrib_Cpty`, `Euler_Contrib_Factor` | Euler allocation of the current book on actual sensitivities: CVA per counterparty × risk type, macro hedges as one row per risk type, and the same per factor. Check rows show Σ contributions = K |
| `Counterparty_Policy` | each counterparty's sector and IG/HY (from its dominant CCS name by \|CR01\|; `UNMAPPED` if it has no CCS) and its policy |
| `Base_Capital`, `Info` | book capital by class, run parameters and definitions |

### Hedging policies (defaults, all configurable)

| policy | example | market delta | market vega | CCS |
|---|---|---|---|---|
| `specific_hedge` (CCS bucket 1a by default) | WORLD, the 7 SSAs | 100% | 0% | 80%, different (dummy) name in the same sector & IG/HY |
| `name_hedge` | IPL | 100% | 90% | 80%, its own name |
| `sector_hedge` (default population) | most counterparties | 100% | 90% | 80%, different (dummy) name in the same sector & IG/HY |
| `unhedged_delta` | a few names | 100% | 0% | 0% |
| `unhedged` | a few names | 0% | 0% | 0% |

The CCS hedge mirrors the CVA tenor profile. The dummy name is `PROXY:<bucket>:<quality>` (ρ_name 50% to the
counterparty). Qualified-index hedges (`instrument: qualified_index`, optionally RW-matched with `sizing: beta`)
remain available in the config.

Assignment order: `policy` column (generic format) → `assignment.counterparties` (key
`INTERNAL_ENTITY|COUNTERPARTY_ID` or `COUNTERPARTY_ID`) → `assignment.ccs_buckets` (counterparty sector)
→ `assignment.default`.

### Counterparty RW method (`CPTY_RW`)

For counterparty c and risk type rt, let s_c be c's net CVA sensitivities on the factors of rt, with
gross size G_c = Σ|s_c|. The trade is s_c scaled to a gross size of `calibration.counterparty.trade_size`
user units (default $1: $1/bp = 10,000 normalised for CCS/IR/RCS delta, $1 per 1% = 100 normalised for
FX/EQ/CMDTY delta and all vega; set per risk type in `calibration.counterparty.units`), plus the hedge c's
policy puts on it. Then, per $1 (`*_eff_rw`: divide by 10,000 or 100 instead):

* incremental RW = [K(book + trade) − K(book)] / trade_size
* Euler RW = ∇K(book)·trade / trade_size
* stand-alone RW = K(trade) / trade_size
* reverse incremental RW (`incremental_rw_reverse`) = the same for a trade that *reduces* the existing
  exposure (opposite profile and hedge). Its Euler counterpart is exactly −Euler RW. The RWs apply to
  trades shaped like the counterparty's existing profile.

RW × the gross $ sensitivity (`gross_sens_user`) of a new trade with c, of similar profile, approximates its
capital. The counterparty's Euler allocation is `gross_sens_user` × Euler RW: its CVA plus its policy-implied hedge.

## Reading the results

* Marginal RWs depend on the book. They can be **negative** when the trade offsets existing risk,
  e.g. +$1 IR DV01 against a net-short book, or an index hedge added to an under-hedged bucket 8.
  Decide whether pricing uses them floored, or uses the stand-alone column.
* Index hedges in bucket 8 correlate with sector buckets at γ = 45% (0% with bucket 7). On a
  stand-alone basis a 100% RW-matched index hedge *raises* capital: the stand-alone optimum is about
  γ/(1+R) ≈ 45% of the RW-matched size. Their value comes from netting against the whole portfolio.
* Single-factor market risk types (e.g. FX DELTA USDCAD) have the same profile for every counterparty,
  so their RW varies only by policy. Multi-factor types (IR DELTA across currencies and tenors, CCS)
  differ by counterparty profile.
* A positive `unallocated_hedge` in `CPTY_EULER_RECON` means the actual macro hedges deliver less
  offset than the policies assume (e.g. vega hedged 60% vs a 90% sector_hedge policy). A negative
  value means they deliver more.
* Runtime is about 1–2 ms per counterparty × risk type; 10,000 counterparties take about 1.5 minutes.

## Layout

```
sacva/config/osfi_car_ch8.yaml   regulatory parameters (OSFI CAR 2027 Ch. 8 / MAR50)
sacva/config/hedging_policy.yaml policies, assignment, calibration settings
sacva/params.py     YAML loading
sacva/loader.py     book loading, normalisation, validation, unit conversion
sacva/riskclass.py  per-class RW / rho / gamma rules
sacva/engine.py     capital + closed-form gradient
sacva/policy.py     policy assignment and hedge generation
sacva/marginal.py   incremental / Euler / stand-alone marginal capital
sacva/calibrate.py  effective-RW grids
sacva/prep.py       CVA_LD_SENS + CVA_CALC_RF + FX + UEN map -> counterparty-level CVA_CALC_RF
sacva/weights.py    SACVAWeights.csv
sacva/config/input_mapping.yaml  CVA_LD_SENS -> CVA_CALC_RF mapping tables
sacva/report.py     Excel / CSV output
sacva/cli.py        command line
```
