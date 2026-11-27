# Option Signal Checker — latest audit

Generated (UTC): 2026-11-27T12:30:38.136015+00:00

## Summary
- Total tracked signals: **15**
- Active: **1**
- STOPPED: **13**
- Trailing SL stopped: **1**
- Final target hit: **0**
- Other statuses: **0**
- Records with quality metrics: **3/15**
- Records missing a valid entry: **0**

## By coin

| Coin | Active | SL stopped | Trailing SL | Target hit | Other |
|---|---:|---:|---:|---:|---:|
| BTC | 0 | 7 | 1 | 0 | 0 |
| ETH | 1 | 6 | 0 | 0 | 0 |

## Per-signal detail

| Coin | Contract | Status | TP stage | Peak vs entry | Last vs entry | RSI | Volume x | Body % |
|---|---|---|---:|---:|---:|---:|---:|---:|
| ETH | ETH-27NOV26-1680-C | STOPPED | 0 | +6.17% | -49.09% | missing | missing | missing |
| BTC | BTC-27NOV26-62000-P | TRAIL_STOPPED | 1 | +28.33% | -0.33% | missing | missing | missing |
| ETH | ETH-27NOV26-1700-C | STOPPED | 0 | +0.00% | -32.24% | missing | missing | missing |
| BTC | BTC-27NOV26-61000-C | STOPPED | 0 | +0.00% | -49.62% | missing | missing | missing |
| BTC | BTC-27NOV26-59500-C | STOPPED | 0 | +0.00% | -33.81% | missing | missing | missing |
| ETH | ETH-27NOV26-1720-C | STOPPED | 0 | +0.00% | -53.56% | missing | missing | missing |
| BTC | BTC-27NOV26-61000-P | STOPPED | 0 | +0.00% | -33.11% | missing | missing | missing |
| ETH | ETH-27NOV26-1700-P | STOPPED | 0 | +0.00% | -39.06% | missing | missing | missing |
| ETH | ETH-27NOV26-1800-P | STOPPED | 0 | +2.36% | -33.55% | missing | missing | missing |
| BTC | BTC-27NOV26-60000-P | STOPPED | 0 | +0.00% | -36.20% | missing | missing | missing |
| ETH | ETH-27NOV26-1770-P | STOPPED | 0 | +0.00% | -29.27% | missing | missing | missing |
| BTC | BTC-27NOV26-62000-P | STOPPED | 0 | +0.00% | -25.53% | missing | missing | missing |
| BTC | BTC-27NOV26-61000-C | STOPPED | 0 | +0.00% | -40.72% | 61.9 | 1.53 | 82% |
| BTC | BTC-28NOV26-61500-C | STOPPED | 0 | +0.00% | -32.22% | 58.5 | 1.18 | 43% |
| ETH | ETH-28NOV26-1720-P | ACTIVE | 0 | +0.00% | n/a | 38.6 | 1.62 | 57% |

## Audit limitations

- This is a tracking-ledger audit, not proof of strategy accuracy or live executable P&L.
- ACTIVE records are not wins or losses; last premium is only a snapshot.
- STOPPED/TRAIL_STOPPED/TARGET_HIT counts are tracker outcomes, subject to quote availability and entry/exit spread.
- Legacy records without signal quality metrics cannot support indicator-based root-cause analysis.
- A high peak does not mean the target was executable; peak is the highest observed monitor quote.

## Improvement gate

No signal thresholds, expiry rules, spread limit, premium source, SL, or targets are changed by this checker.
Any proposed change must be tested separately against a sufficiently large paper-trading sample and compared with the unchanged baseline.
