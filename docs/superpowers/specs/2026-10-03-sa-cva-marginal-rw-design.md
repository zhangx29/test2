# SA-CVA Marginal Capital & Effective Risk-Weight Calibration — Design

Date: 2026-10-03
Regulatory source: OSFI Capital Adequacy Requirements (CAR) 2027, Chapter 8 — CVA Risk,
section 8.3 (SA-CVA), effective Q1 2027. Sections 8.3.3-8.3.10 were verified identical (whitespace-insensitive
text comparison, 2026-10-03) to CAR 2026 Chapter 8, the version in force before 31 Jan 2027 (both
follow Basel MAR50). Paragraph references below are the Basel MAR references that OSFI quotes inline.

## 1. Purpose

The XVA desk needs the SA-CVA capital consumed by one unit of CVA sensitivity
(e.g. $1 CR01 on a counterparty, $1 DV01, $1 per 1% vega) so that capital cost can be charged in
pricing / KVA. The answer depends on

* the existing CVA + hedge book (diversification, hedging disallowance), and
* the hedging policy applied to the name: e.g. SSA/MDB names are not charged CVA, so the desk does
  not hedge their vega, so the same sensitivity attracts more capital.

Output is a table of *effective risk weights* (capital per unit sensitivity):

* CCS delta: hedging policy × SA-CVA sector bucket (1a, 1b, 2–7) × credit quality (IG / HY-NR)
* Market delta / vega: hedging policy × representative risk factor (IR, FX, RCS, EQ, CMDTY)

## 2. Regulatory mechanics implemented (verified against OSFI text)

| Item | Rule | Ref |
|---|---|---|
| Weighted sensitivity | WS_k^CVA = RW_k·s_k^CVA, WS_k^Hdg = RW_k·s_k^Hdg | MAR50.51 |
| Netting | WS_k = WS_k^CVA − WS_k^Hdg; CVA is a positive number, s^Hdg is the sensitivity of the hedge *market value*, so bought protection has positive s^Hdg and offsets | MAR50.53 |
| Bucket | K_b = sqrt( Σ WS_k² + Σ_{k≠l} ρ_kl WS_k WS_l + R·Σ (WS_k^Hdg)² ), R = 0.01 | MAR50.53 |
| Across buckets | K = m_CVA·sqrt( Σ K_b² + Σ_{b≠c} γ_bc S_b S_c ), S_b = max(−K_b, min(Σ WS_k, K_b)) | MAR50.53 |
| Multiplier | m_CVA = 1 (OSFI may require higher) | MAR50.41 |
| Total | Σ over risk classes of delta K + vega K; no CCS vega | MAR50.42–45 |
| Units | Absolute-bp factors: s = ΔV / 0.0001. Relative factors (FX spot, all vols, EQ, CMDTY): s = ΔV / 0.01 | MAR50.56–77 |
| CCS buckets | 1a sovereigns/central banks/MDBs, 1b local govt/govt-backed non-fin/education/public admin, 2 financials, 3–6 sectors, 7 other, 8 qualified indices (optional). 1a and 1b share bucket 1 for γ | MAR50.63 |
| CCS RW | IG: 0.5, 1.0, 5.0, 3.0, 3.0, 2.0, 1.5, 5.0, 1.5 %; HY/NR: 2.0, 4.0, 12.0, 7.0, 8.5, 5.5, 5.0, 12.0, 5.0 % | MAR50.65 |
| CCS ρ | ρ_tenor (100/90) · ρ_name (100 same / 90 legally related / 50) · ρ_quality (100/80); bucket 8: name 100 same index+series / 90 same index other series / 80 | MAR50.65 |
| Index hedges | Qualified indices may go in bucket 8; otherwise look-through to constituents in buckets 1–7 | MAR50.50, 50.63 |

Market risk classes (IR, FX, RCS, EQ, CMDTY) take all RW/ρ/γ values from MAR50.54–77 as reproduced
in `sacva/config/osfi_car_ch8.yaml`.

## 3. Marginal capital — two modes reported side by side

A *trade* is a set of sensitivity rows: the unit CVA sensitivity plus the hedge legs the
policy generates.

1. **Incremental**: ΔK = K(book + size·trade) − K(book), divided by size. Default size is 1 user unit.
   Captures the hedging-disallowance term even for new hedge factors.
2. **Euler (directional derivative)**: ∇K(book)·trade, using the closed-form gradient
   (chain rule through K_b, the S_b clip and the γ cross-terms).
   K is homogeneous of degree 1, so Σ s_k ∂K/∂s_k = K, which is a test invariant.
   Caveat: when a hedge factor is currently zero, Euler misses the R·(WS^Hdg)² disallowance.

Empty book (K = 0): Euler is undefined, so it falls back to the stand-alone value and logs a warning.

## 4. Hedging policy

`sacva/config/hedging_policy.yaml`:

* `policies`: policy → per `RISKCLASS.measure` → {`instrument`, `ratio`, `sizing`}
  * market instruments: `same_factor` (hedge sensitivity on the same risk factor)
  * CCS instruments: `single_name` (ρ_name 100%), `sector_proxy`
    (another name in the same bucket and quality: 50%), `qualified_index` (bucket 8, IG or HY by
    counterparty quality)
  * `sizing`: `cr01` (hedge sensitivity = ratio × CVA sensitivity) or `beta`
    (× RW_cpty / RW_hedge, i.e. risk-weight-matched)
* Defaults:
  * `unhedged`: nothing hedged.
  * `specific_hedge` (SSA/MDB): market delta 100%, vega 0%, CCS single-name 0%
    (no liquid CDS; configurable).
  * `sector_hedge`: delta 100%, vega 100%, CCS qualified index, beta-sized, 100%.
* `assignment`: policy per counterparty from a `policy` column in the book, then a counterparty
  list, then a CCS bucket rule (1a → specific_hedge), then a default (sector_hedge).

## 5. Calibration grid

* **Representative trade (main grid)**: a new synthetic name, so ρ_name = 50% against existing names.
  CR01 tenor profile from config (default 100% at 5Y). Market cells use `representative_factors`
  from config (e.g. IR delta CAD 5Y, IR vega CAD, FX USD).
* **Realised average (diagnostic, CCS only)**: for each existing counterparty, a unit trade with its
  own normalised CR01 tenor profile added to the same name (ρ_name = 100%), plus its policy hedge.
  Results are |CR01|-weighted averages per cell, and the cell is blank when there are no names.
  Market sensitivities are portfolio-level (not attributed to counterparties), so no realised
  column is produced for market cells.
* Reported per cell: capital per user unit and effective RW (capital per regulatory unit, which is
  directly comparable to the regulatory RW: a stand-alone unhedged name returns exactly its RW), for
  both incremental and Euler, plus a basis label.

## 6. Input format

CSV or Excel, one row per sensitivity:

`source` (CVA|HDG), `risk_class` (IR|FX|CCS|RCS|EQ|CMDTY), `measure` (delta|vega), `bucket`,
`risk_factor`, `name`, `quality` (IG|HY|NR), `group`, `series`, `amount`,
`amount_unit` (per_unit|per_bp|per_1pct), `policy` (optional).

Validation rejects unknown classes, buckets, tenors or units with row numbers. `per_bp` is
multiplied by 1e4 and `per_1pct` by 1e2 to reach regulatory units.

## 7. Testing

Hand-computed golden cases from the regulatory formulas, finite-difference vs analytic gradient,
Euler sum equals K, homogeneity, incremental → Euler as size → 0, stand-alone unhedged effective
RW equals regulatory RW, and policy ordering (unhedged ≥ specific ≥ sector for vega).

## 8. Counterparty-level calibration (added 2026-10-04)

Input: CVA_CALC_RF with `INTERNAL_ENTITY` and `COUNTERPARTY_ID`. CVA rows are attributed per counterparty
for all risk types, with SK_HEDGE = 0. Hedge rows are unattributed macro hedges, with blank ids and
SK_CVA = 0. Capital is consolidated; the counterparty unit is (INTERNAL_ENTITY, COUNTERPARTY_ID).

* Per counterparty × risk type, the trade is the counterparty's own net profile scaled to 10,000 gross
  normalised sensitivity, plus its policy hedge. RW = ΔK / 10,000, i.e. per unit of gross sensitivity.
  Incremental means trade-incremental, K(book + trade) − K(book); Euler and stand-alone are reported
  alongside. Leave-one-out was considered and dropped: it measures the whole relationship, not the
  cost of a new trade.
* Sector and IG/HY come from the counterparty's dominant CCS name. `sector_hedge` counterparties are
  clustered by sector × IG/HY × risk type (gross-weighted). Other policies are reported per counterparty.
* Euler reconciliation per risk type: counterparty CVA + unattributed CVA + macro hedge = K. The macro
  hedge is split into policy-implied (allocated to counterparties) and unallocated.
* Performance: the CCS correlation ρ = (a + b·δ_tenor)(name terms)(c + d·δ_quality) is applied as
  partition sums in O(n) rather than as a dense matrix. Each risk class is compiled once and evaluated
  with numpy per counterparty.
