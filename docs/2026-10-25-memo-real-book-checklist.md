# RW methodology memo: figures to update on the real portfolio

Memo: `docs/2026-10-20-sa-cva-rw-methodology.docx`. Run on the production book:

```bash
sacva capital --book cva_calc_rf_yyyymmdd.csv --out out/cva_calc.xlsx
python examples/memo_figures.py --book cva_calc_rf_yyyymmdd.csv --out out/memo     # pip install matplotlib for the PNG
sacva weights --book cva_calc_rf_yyyymmdd.csv --out out/size/SACVAWeights.csv --method size_term
```

Source: `memo_numbers.csv` (MN), `table_3_4.csv`, `buckets_3_5.csv`, `curve_3_7.csv`, `table_4_2.csv`,
`grid_cells.csv` (GC), `figure_3_7.png`. The example-book value is in MN's `example_book_value` column; running the
script on `examples/cva_calc_rf.csv` reproduces the memo.

## Book-dependent: must change

| § | Memo text (example book) | Source |
|---|---|---|
| 3.2 | "Monotonicity held in all 48 cells tested on the example book" | MN 3.2 |
| 3.3 | "On the example book (CCS K = $3.76mm)" | MN K |
| 3.3 | "closed form reproduces the full recalculation within 4% in bucket 2 at every size" (example: 2.8% of stand-alone, 6.5% relative) | MN 3.3 (two metrics; state which) |
| 3.3 | "less accurate in buckets where the book is nearly flat" | MN 3.3 worst cell |
| 3.4 | all 24 table values (1a IG 1.49 / 1.49 / 5.12 / 1.68 / 46.0 / 10.8 … 7 HY) | `table_3_4.csv` |
| 3.4 | Choice of rows 1a IG, 2 IG, 3 HY, 7 HY (check they are populated / representative on the real book) | GC, `--table-cells` |
| 3.5 | "against $3.76mm of CCS capital for the whole example book" | MN K |
| 3.5 | "Bucket 6 HY shows the effect" (choice of offset bucket) | MN 3.5 offset cell |
| 3.5 | "Euler is 0.25 per $1" / "A $10k probe charges 232" | MN 3.5 |
| 3.5 | "The example book is small, so these magnitudes are not typical": rewrite, and add the 10,000 × s vs K_b / S_b comparison | `buckets_3_5.csv` |
| 3.6 | "new-name RW is 245 per $1 and the counterparty-based cell is 362" | MN 3.6 |
| 3.6 | Cons: "Cells with one counterparty are noisy", "near-zero cells in offset buckets": confirm on the real book | `SACVAWeights_detail.csv` (`ccs_n_cpty`) |
| 3.7 | Title "A $10k probe already prices 46-75% of stand-alone capital" | MN 3.7 title |
| 3.7 | Subtitle "Example book, CCS K = $3.76mm" | MN K |
| 3.7 | End labels: unhedged_delta 2 IG 49%, name_hedge 2 IG 45%, sector_hedge 2 IG 11%, unhedged_delta 6 HY 0.05% | MN 3.7 end labels |
| 3.7 | Annotation "46%-75% of stand-alone"; the curves themselves | `figure_3_7.png` / `curve_3_7.csv` |
| 3.7 | Caption "Source: sacva new-name grid on examples/cva_calc_rf.csv, 4 cells x 17 probe sizes" | file name / date |
| 3.7 | "reaches stand-alone by about $1mm" (example: lowest share at $1mm is 98%) | MN 3.7 text |
| 3.7 | "offset bucket (6 HY) moves furthest: from near zero to almost half of stand-alone" | MN 3.7 offset share |
| 4.1 | "On the example book, Keff ranges from $4.06mm to $4.23mm against a class-level CCS K of $3.76mm" | MN 4.1 (exhibit cells and all cells) |
| 4.2 | all 20 table values (full / fit at $1k and $31.6k, max error) | `table_4_2.csv` |
| 4.2 | Table rows (same 4 cells as 3.7) | `--curve-cells` |
| 4.2 | "In near-flat buckets such as 6 HY it overstates at small sizes" | MN 4.2 text |
| 4.4 | "When x·s exceeds 5–10% of Keff": number of cells where a $10k trade already exceeds this | MN 4.4 |
| 5 | "All figures in this memo come from the synthetic example book (... CCS K = $3.76mm)" | rewrite |
| 5 | "Real-book rerun. The Keff fit errors in section 4.2 need to be re-measured": now done; replace with result | MN 4.2 (all cells) |
| 5 | "in bucket 2 of the example book the two agree within 0.1%" (Euler vs $1 incremental) | MN 5 |
| 5 | "Production assumptions ... reference size": confirm $10k and 5Y profile, set `--ref` | — |
| 2.3 / 4.3 | Empty cells fall back to the new-name grid: how many of the 48 do on the real book | MN 2.3 / 3.6 |

## Not book-dependent: leave as is

* 1.4 RW table, ρ values, γ range, R = 1% ("10% of the unhedged charge"): regulatory (OSFI CAR Ch. 8).
* 2.1 hedging policy table: config, unless `hedging_policy.yaml` changes.
* 3.1 SK_CVA = 10,000 × 10,000 = 100mm normalised.
* 3.4 "about 84% of unhedged variance left with a proxy, 4% with the own name": follows from ratio 0.8 and ρ 50% / 100%.
* 3.5 "$5.0mm for 2 IG unhedged" = 10,000 × 5% × 10,000 (but its comparison with K changes).
* 4.3 column list: matches the `size_term` output (`eulerRw, standaloneRw, refRw, kEff, refSize`; vega columns are
  prefixed `vega`). Tenor columns and `vegaCost` carry `refRw`.
* $1k / $10k / $31.6k / $100mm and "17 sizes": the size ladder, unless changed.

## Minor discrepancy on the example book

4.2, unhedged_delta 2 IG max error: the memo says 0.04%, the script gives 0.05% (0.0514%).
