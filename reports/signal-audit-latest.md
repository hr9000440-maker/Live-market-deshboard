# Option Signal Checker — latest audit

Generated (UTC): 2026-11-27T22:57:23.582988+00:00

## Summary
- Total tracked signals: **23**
- Active: **2**
- STOPPED: **14**
- Trailing SL stopped: **1**
- Final target hit: **6**
- Other statuses: **0**
- Records with quality metrics: **11/23**
- Records missing a valid entry: **0**

## By coin

| Coin | Active | SL stopped | Trailing SL | Target hit | Other |
|---|---:|---:|---:|---:|---:|
| BTC | 2 | 7 | 1 | 3 | 0 |
| ETH | 0 | 7 | 0 | 3 | 0 |

## Per-signal detail

| Coin | Contract | Status | TP stage | Peak vs entry | Last vs entry | RSI | Volume x | Body % | ADX | MACD hist | ATR % | Confirmation |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| ETH | ETH-27NOV26-1680-C | STOPPED | 0 | +6.17% | -49.09% | missing | missing | missing | missing | missing | missing | missing |
| BTC | BTC-27NOV26-62000-P | TRAIL_STOPPED | 1 | +28.33% | -0.33% | missing | missing | missing | missing | missing | missing | missing |
| ETH | ETH-27NOV26-1700-C | STOPPED | 0 | +0.00% | -32.24% | missing | missing | missing | missing | missing | missing | missing |
| BTC | BTC-27NOV26-61000-C | STOPPED | 0 | +0.00% | -49.62% | missing | missing | missing | missing | missing | missing | missing |
| BTC | BTC-27NOV26-59500-C | STOPPED | 0 | +0.00% | -33.81% | missing | missing | missing | missing | missing | missing | missing |
| ETH | ETH-27NOV26-1720-C | STOPPED | 0 | +0.00% | -53.56% | missing | missing | missing | missing | missing | missing | missing |
| BTC | BTC-27NOV26-61000-P | STOPPED | 0 | +0.00% | -33.11% | missing | missing | missing | missing | missing | missing | missing |
| ETH | ETH-27NOV26-1700-P | STOPPED | 0 | +0.00% | -39.06% | missing | missing | missing | missing | missing | missing | missing |
| ETH | ETH-27NOV26-1800-P | STOPPED | 0 | +2.36% | -33.55% | missing | missing | missing | missing | missing | missing | missing |
| BTC | BTC-27NOV26-60000-P | STOPPED | 0 | +0.00% | -36.20% | missing | missing | missing | missing | missing | missing | missing |
| ETH | ETH-27NOV26-1770-P | STOPPED | 0 | +0.00% | -29.27% | missing | missing | missing | missing | missing | missing | missing |
| BTC | BTC-27NOV26-62000-P | STOPPED | 0 | +0.00% | -25.53% | missing | missing | missing | missing | missing | missing | missing |
| BTC | BTC-27NOV26-61000-C | STOPPED | 0 | +0.00% | -40.72% | 61.9 | 1.53 | 82% | missing | missing | missing | missing |
| BTC | BTC-28NOV26-61500-C | STOPPED | 0 | +0.00% | -32.22% | 58.5 | 1.18 | 43% | missing | missing | missing | missing |
| ETH | ETH-28NOV26-1720-P | TARGET_HIT | 0 | +192.75% | +192.75% | 38.6 | 1.62 | 57% | missing | missing | missing | missing |
| ETH | ETH-28NOV26-1720-P | TARGET_HIT | 0 | +204.90% | +204.90% | 37.8 | 0.59 | 17% | 18.5 | -1.49779 | 0.27% | missing |
| BTC | BTC-28NOV26-61000-P | TARGET_HIT | 0 | +374.27% | +374.27% | 47.7 | 0.58 | 45% | 16.7 | -30.06589 | 0.22% | missing |
| BTC | BTC-28NOV26-61500-P | TARGET_HIT | 0 | +275.51% | +275.51% | 47.7 | 0.58 | 45% | 16.7 | -30.06589 | 0.22% | CAUTION — ADX 20 से कम: trend कमजोर; Volume average से कम: participation कमजोर |
| ETH | ETH-28NOV26-1680-P | TARGET_HIT | 0 | +326.74% | +326.74% | 37.8 | 0.59 | 17% | 18.5 | -1.49779 | 0.27% | CAUTION — ADX 20 से कम: trend कमजोर; Volume average से कम: participation कमजोर |
| BTC | BTC-28NOV26-61000-P | TARGET_HIT | 0 | +374.13% | +374.13% | 39.9 | 1.67 | 12% | 24.8 | -38.96355 | 0.23% | GOOD — ADX/MACD/Volume aligned |
| BTC | BTC-28NOV26-62000-P | ACTIVE | 0 | +0.00% | -12.49% | 31.1 | 0.40 | 41% | 68.0 | -40.90097 | 0.90% | CAUTION — Volume average से कम: participation कमजोर |
| ETH | ETH-28NOV26-1650-P | STOPPED | 0 | +0.00% | -36.70% | 29.4 | 0.77 | 60% | 62.9 | -1.03889 | 1.00% | CAUTION — Volume average से कम: participation कमजोर |
| BTC | BTC-28NOV26-62000-P | ACTIVE | 0 | +0.00% | -3.98% | 37.3 | 0.17 | 31% | 52.5 | 21.47508 | 0.52% | CAUTION — Volume average से कम: participation कमजोर; MACD momentum signal की दिशा में confirm नहीं |

## Audit limitations

- This is a tracking-ledger audit, not proof of strategy accuracy or live executable P&L.
- ACTIVE records are not wins or losses; last premium is only a snapshot.
- STOPPED/TRAIL_STOPPED/TARGET_HIT counts are tracker outcomes, subject to quote availability and entry/exit spread.
- Legacy records without signal quality metrics cannot support indicator-based root-cause analysis.
- A high peak does not mean the target was executable; peak is the highest observed monitor quote.

## Improvement gate

No signal thresholds, expiry rules, spread limit, premium source, SL, or targets are changed by this checker.
Any proposed change must be tested separately against a sufficiently large paper-trading sample and compared with the unchanged baseline.
