# SA-CVA calibration — full-portfolio run review (2026-10-05)

Review of the `sacva calibrate` output on the actual portfolio (workbook `rw_grid_20260831`), based on
screenshots of `CCS_*_RW` / `CCS_Detail`, `Market_Grid`, `CPTY_RW` and `CPTY_RW_CLUSTER`. The output itself
was not available on this machine, so figures below are read from photos.

## 1. Blocking issue: CCS Euler RW ≠ incremental RW for some counterparties

For a $1/bp trade, incremental and Euler RW should agree almost exactly. They don't for several CCS rows:

| Row | Incremental | Reverse | Euler |
|---|---|---|---|
| HINS (3 factors, sector_hedge) | 0.000542 | −0.00054 | **−0.00019** |
| RENTBK (5 factors, sector_hedge) | 0.000567 | −0.00057 | **−0.000073** |
| Cluster 2 IG CCS | 0.001736 | −0.00174 | **0.001217** |
| Cluster 2 HY / 3 HY / 3 IG | −0.00819 / −0.02481 / −0.01129 | ≈ −incremental | −0.00933 / −0.02544 / −0.01163 |

- Incremental and reverse are exact mirror images, so capital is smooth there. This is not a kink or
  second-order effect: the gradient should match.
- The pattern points at the index-hedge legs. Single-factor sector_hedge counterparties match (BARCI, BNP,
  GSI, JEFFGH, TPICA). Multi-factor specific_hedge counterparties also match (CNTMT, NARODNAB); that policy
  puts on no CCS hedge legs. Only multi-factor sector_hedge counterparties break.
- Not reproducible on the example book, or on a synthetic book with related-name chains, mixed signs,
  mixed IG/HY and several buckets (agreement to ~1e-5). The CCS correlation operator is symmetric. The
  trigger exists only in the real book.
- **Until resolved, don't use CCS `euler_rw`, `euler_alloc` or the CCS cluster Euler column.** Incremental RW
  is usable. Market risk types are unaffected.
- A zero check in `CPTY_EULER_RECON` does not clear this: a gradient error can still sum to K.

**Diagnostic:** `examples/diag_cpty.py`

```
python examples/diag_cpty.py --book <CVA_CALC_RF file> --policy-config <yaml used> \
    --cpty "BMOIRELAND|HINS" "BMOIRELAND|RENTBK" "BMOIRELAND|BARCI" "BMOIRELAND|CNTMT"
```

BARCI and CNTMT are controls. The script prints incremental RW at sizes 1–1e6 in both directions next to
Euler RW, a per-factor gradient vs finite-difference check, book names sharing a name or parent with the
counterparty, and a correlation symmetry check per touched bucket. If a per-factor check disagrees, the bug
is in the gradient; if all factors agree but incremental ≠ Euler, it is a position / alignment bug.

## 2. Findings on the rest of the output

### Market_Grid
- 100%-hedged stand-alone RW = √R × RW = 10% of regulatory RW, as expected (FX 0.011, IR vega 0.1,
  RCS 0.005).
- RCS stand-alone equals the regulatory RW: the book has no RCS.
- IR delta direction matters: CAD 5Y +1 unit → −0.00079 (reduces capital); USD 5Y +1 → +0.0041
  (~55% of RW). Price from the signed grid rows.
- Unhedged effective RW as a share of regulatory RW:

  | Risk type | Share |
  |---|---|
  | FX vega | 96% |
  | IR vega | 71% (CAD), 96% (USD) |
  | CO vega | 59% |
  | CO delta | 45% |
  | EQ vega | 43% |
  | EQ delta | 38% |

- EQ delta/vega and CO vega have Euler RW of exactly 0 under both hedge policies: the book holds no actual
  hedges on those factors, so the 100% hedge assumption prices unhedged risk at zero (→ use actual hedges
  instead of hypothetical ones).

### CCS new-name grid
- Unhedged and specific_hedge are identical (specific_hedge CCS hedge ratio is 0). *(Config changed 2026-10-14:
  specific_hedge now hedges CCS 80% with a dummy same-sector name. RWs are now reported per $1 (`*_rw`), with the
  per-normalised values in `*_eff_rw`.)*
- Effective RW as a share of regulatory RW:

  | Bucket | Share |
  |---|---|
  | 1a | ~26% |
  | 2 | 49–59% |
  | 3 | ~16% |
  | 4 | ~8% |
  | 5 | ~29% |
  | 6 | ~10% |
  | 7 | ≈ 0% |

- Bucket 7 diversifies away almost entirely; needs a floor if used for pricing.
- Sector_hedge is negative in every bucket, from −0.0017 (1a IG) to −0.0713 (7 HY). Incremental and Euler
  agree here, so it is genuine: the book is under-hedged at index level, so each new name plus its index
  hedge reduces capital. Stand-alone sector_hedge ≈ 1.05 × RW.

### CPTY_RW and policy assignment
- Policy is assigned by CCS sector, so agencies in sector 2 (e.g. RENTBK) fall under sector_hedge. If the
  desk treats agencies as SSA, add them under `assignment.counterparties`.
- CRITEO and TDGFUC are UNMAPPED (FX risk, no CCS) and default to sector_hedge. Check whether CCS rows are
  missing from the feed.
- SSA vega: filter `CPTY_RW` on policy = specific_hedge, measure = vega, sum `euler_alloc`. Visible so far:
  CNTMT FX vega 1,173; NARODNAB IR vega 806, FX vega −249; BKLV FX vega 141.
- Low priority: gross sizes 138.555, 692.775, 6.92775 are round amounts × 1.38555 (≈ USDCAD) — tiny,
  possibly placeholder rows from the feed.

## 3. Incremental, Euler and stand-alone RW

All three use the same trade: the counterparty's CVA sensitivity plus its policy hedge, scaled to one unit
($1/bp CR01 = 10,000 normalised; 1% for vega).

| | Formula | Measures | Properties |
|---|---|---|---|
| Incremental | [K(book + trade) − K(book)] / size | Actual capital change from adding the trade to today's book | Exact for that size; order-dependent, Σ incrementals ≠ K; captures convexity for large trades |
| Euler | ∇K(book) · trade / size | Slope of capital in the trade's direction (incremental for a very small trade) | Additive: Σ sensitivity × Euler RW = K; linear, reverse = −Euler; slightly understates large trades (incremental ≥ Euler) |
| Stand-alone | K(trade alone) / size | Capital as if the trade were the only position | Ignores the book; unhedged single-factor = regulatory RW; 100% same-factor hedge = √R × RW; RW-matched index hedge ≈ 1.05 × RW (γ = 45%) |

### How the CCS RWs by policy × bucket × IG/HY are generated

Two outputs are cut this way, built differently. Neither bumps counterparties in parallel.

**New-name grid — `CCS_Incremental_RW` / `CCS_Euler_RW` / `CCS_Standalone_RW`**
(`sacva/calibrate.py`, `ccs_representative_grid`). No actual counterparties are used.
- One synthetic name per policy × bucket × IG/HY cell (`__NEW__{bucket}_{quality}`): $1/bp CR01
  (10,000 normalised) at 5Y (`calibration.ccs.tenor_profile`), no parent, so ρ_name = 50% to every book name.
- The policy hedge is added: sector_hedge → RW-matched CDX IG/HY index in bucket 8; specific_hedge and
  unhedged → no CCS hedge.
- Each cell is evaluated on its own against the actual book (incremental, Euler, stand-alone).
- Represents a **new** counterparty in that sector and rating.

**Existing counterparties — `CPTY_RW_CLUSTER`** (sector_hedge only; `sacva/counterparty.py`,
`counterparty_rw` → `cluster_rw`).
1. Each counterparty c is bumped on its own: trade = its own net CCS profile across all names and tenors,
   scaled to $1/bp gross, u_c = s_c / G_c × 10,000 with G_c = Σ|s_c|, plus its policy hedge. RW is computed
   against the book with only that trade added.
2. RWs are averaged within sector × IG/HY × risk type, weighted by gross:
   RW_cluster = Σ_c RW_c · G_c / Σ_c G_c. Sector and IG/HY come from the counterparty's dominant CCS name
   by |CR01|; the trade still includes all its names, even in other buckets.

**Comparison with a parallel bump**
- Euler: linear, so Σ_c G_c · Euler_c = ∇K · Σ_c s_c × 10,000. The cluster Euler RW equals a *proportional*
  parallel bump (every counterparty in the cluster grows by the same %), per unit of gross — not +1 unit
  each. Large counterparties dominate (e.g. RENTBK 27.9M gross vs CCDBP 138).
- Incremental: not a parallel bump. Each counterparty's incremental ignores the others' bumps; since capital
  is convex, a true simultaneous bump would give a somewhat higher RW. Small at $1/bp against this book.

| Option | Weighting | Change needed |
|---|---|---|
| Current method | By gross (= proportional parallel bump for Euler) | none |
| Equal-weighted average | Each counterparty counts the same | One option in `cluster_rw` |
| True parallel incremental | +$1/bp to every counterparty at once: [K(book + Σ_c u_c + hedges) − K(book)] / (n × 10,000) | New calculation, ~one revaluation per cell |

Gross weighting suits pricing marginal growth of the existing book; equal weighting suits a typical
counterparty in the cell; the new-name grid is the reference for a genuinely new name.

## 4. Using the output for pricing

1. Pick the RW:
   - Existing counterparty: `CPTY_RW`, its row per risk type.
   - New counterparty under sector_hedge: `CPTY_RW_CLUSTER` (sector × IG/HY × risk type).
   - New SSA/MDB or unhedged name: `CCS_*_RW` for credit, `Market_Grid` for market risk.
   - Market delta: the `direction` row matching the trade's CVA sensitivity versus the book.
2. Capital ≈ Σ over risk types of RW × gross normalised sensitivity ($/bp × 10,000; $ per 1% × 100).
   Risk types add, as in SA-CVA.
3. KVA = Σ_t capital(t) × cost of capital × discount factor × Δt, with capital(t) from RW × the expected
   sensitivity profile.
4. Which RW:
   - Euler as default (consistent with the portfolio allocation).
   - Incremental for trades large relative to the bucket (> ~5–10% of bucket gross).
   - Stand-alone as ceiling / reference.
   - Charge max(Euler or incremental, floor), floor = 0 or a % of stand-alone.
5. Recalibrate monthly or quarterly; RWs depend on the current book.

Example, new 2 IG name with $5,000/bp CR01 (5e7 normalised):

| Basis | RW | Capital |
|---|---|---|
| Unhedged / SSA, Euler | 0.0293 | $1.47M |
| Stand-alone | 0.05 | $2.5M |
| sector_hedge, Euler | −0.0005 | ≈ −$25k |

## 5. Negative RWs

A negative RW means the trade plus its hedge reduces book capital at the margin. Two sources:

1. **Market delta offsetting the book** (e.g. +1 CAD 5Y DV01 against a book net the other way): genuine
   diversification, but book-dependent and can flip over the trade's life.
2. **The sector_hedge index leg** (all negative CCS RWs): the CVA leg adds capital in its sector bucket; the
   RW-matched bucket-8 index hedge (γ = 45% to every sector) reduces capital across the whole CCS book.
   With the book under-hedged at index level, the hedge benefit outweighs the CVA cost.

Problems with case 2 for pricing:
- The hedge is hypothetical; the credit is real only if the protection is actually bought.
- The benefit belongs to the hedge, not the client trade — the desk could buy index protection directly.
- It is marginal and shrinks as more index protection is added; pricing many trades at a negative RW
  overstates the total.

**Recommendation**
- CCS: don't let the hedge leg push the charge below zero. Floor CCS RW at 0, or price the CVA-leg Euler RW ×
  (1 − actual hedge effectiveness), using `unallocated_hedge` in `CPTY_EULER_RECON`.
- Market delta: negative RWs from genuine book offsets get partial credit or a 0 floor, per policy.
- Split `CPTY_RW` into CVA-leg and hedge-leg columns (index hedge decomposition) once the CCS Euler issue is
  resolved.

## 6. Open items / still needed
1. Text output of `diag_cpty.py` for HINS, RENTBK, BARCI, CNTMT.
2. `CVA_CALC_RT` and `Base_Capital`.
3. `CPTY_EULER_RECON`, especially `unallocated_hedge` by risk type.
4. Console warnings (NAME_CONFLICTS and RECON_BREAKS counts).
5. Counterparty counts by policy and sector from `Counterparty_Policy`.
6. Rotate the password visible in the shared Notepad++ screenshot.
7. Decide whether to add equal-weighted and parallel-bump columns to `CPTY_RW_CLUSTER`.
