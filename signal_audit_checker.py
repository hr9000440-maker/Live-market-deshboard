#!/usr/bin/env python3
"""Read-only audit of option signal tracking state. Never changes signals or orders."""
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone

SOURCE = "state/option_positions.json"
OUT_DIR = "reports"
MD_PATH = os.path.join(OUT_DIR, "signal-audit-latest.md")
JSON_PATH = os.path.join(OUT_DIR, "signal-audit-latest.json")

def pct(a, b):
    return ((a / b) - 1.0) * 100.0 if b else None

def main():
    try:
        with open(SOURCE, encoding="utf-8") as f:
            positions = json.load(f)
    except FileNotFoundError:
        positions = []
    if not isinstance(positions, list):
        raise SystemExit("Ledger must be a JSON list; refusing to create a misleading report.")

    statuses = Counter(str(p.get("status", "UNKNOWN")) for p in positions)
    coins = defaultdict(Counter)
    completed = []
    metric_count = 0
    missing_entry = 0
    rows = []
    for p in positions:
        coin = str(p.get("coin", "UNKNOWN"))
        status = str(p.get("status", "UNKNOWN"))
        coins[coin][status] += 1
        entry = float(p.get("entry") or 0)
        peak = float(p.get("peak") or entry)
        last = float(p.get("last_premium") or 0)
        stage = int(p.get("stage") or 0)
        if entry <= 0:
            missing_entry += 1
        if all(k in p for k in ("signal_rsi", "signal_ema9", "signal_ema21",
                                "signal_volume_ratio", "signal_body_ratio")):
            metric_count += 1
        if status != "ACTIVE":
            completed.append(p)
        rows.append({
            "coin": coin, "contract": p.get("contract", ""),
            "direction": p.get("market_signal", p.get("key", "")),
            "status": status, "stage": stage,
            "entry": entry or None, "peak": peak or None, "last_premium": last or None,
            "max_favorable_pct": round(pct(peak, entry), 2) if entry else None,
            "last_mark_pct": round(pct(last, entry), 2) if entry and last else None,
            "signal_time": p.get("created_at", ""),
            "has_quality_metrics": all(k in p for k in ("signal_rsi", "signal_ema9", "signal_ema21",
                                "signal_volume_ratio", "signal_body_ratio")),
            "rsi": p.get("signal_rsi"), "volume_ratio": p.get("signal_volume_ratio"),
            "candle_body_ratio": p.get("signal_body_ratio"),
            "macd": p.get("signal_macd"), "macd_signal": p.get("signal_macd_signal"),
            "macd_hist": p.get("signal_macd_hist"), "adx": p.get("signal_adx"),
            "atr": p.get("signal_atr"), "atr_pct": p.get("signal_atr_pct"),
            "market_data_source": p.get("market_data_source", ""),
            "option_quote_source": p.get("source", "")
        })

    total = len(positions)
    final_count = sum(v for k, v in statuses.items() if k != "ACTIVE")
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "total_signals": total, "status_counts": dict(statuses),
        "active_signals": statuses.get("ACTIVE", 0),
        "closed_or_resolved_signals": final_count,
        "signals_with_quality_metrics": metric_count,
        "legacy_or_incomplete_metric_records": total - metric_count,
        "records_missing_valid_entry": missing_entry,
        "by_coin": {k: dict(v) for k, v in sorted(coins.items())},
        "positions": rows,
        "interpretation": [
            "This is a tracking-ledger audit, not proof of strategy accuracy or live executable P&L.",
            "ACTIVE records are not wins or losses; last premium is only a snapshot.",
            "STOPPED/TRAIL_STOPPED/TARGET_HIT counts are tracker outcomes, subject to quote availability and entry/exit spread.",
            "Legacy records without signal quality metrics cannot support indicator-based root-cause analysis.",
            "A high peak does not mean the target was executable; peak is the highest observed monitor quote."
        ]
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")

    lines = [
        "# Option Signal Checker — latest audit", "",
        f"Generated (UTC): {report['generated_at_utc']}", "",
        "## Summary",
        f"- Total tracked signals: **{total}**",
        f"- Active: **{statuses.get('ACTIVE', 0)}**",
        f"- STOPPED: **{statuses.get('STOPPED', 0)}**",
        f"- Trailing SL stopped: **{statuses.get('TRAIL_STOPPED', 0)}**",
        f"- Final target hit: **{statuses.get('TARGET_HIT', 0)}**",
        f"- Other statuses: **{sum(v for k, v in statuses.items() if k not in ('ACTIVE','STOPPED','TRAIL_STOPPED','TARGET_HIT'))}**",
        f"- Records with quality metrics: **{metric_count}/{total}**",
        f"- Records missing a valid entry: **{missing_entry}**", "",
        "## By coin", "",
        "| Coin | Active | SL stopped | Trailing SL | Target hit | Other |",
        "|---|---:|---:|---:|---:|---:|"
    ]
    for coin, counts in sorted(coins.items()):
        other = sum(v for k, v in counts.items() if k not in ("ACTIVE","STOPPED","TRAIL_STOPPED","TARGET_HIT"))
        lines.append(f"| {coin} | {counts.get('ACTIVE',0)} | {counts.get('STOPPED',0)} | {counts.get('TRAIL_STOPPED',0)} | {counts.get('TARGET_HIT',0)} | {other} |")
    lines += ["", "## Per-signal detail", "",
              "| Coin | Contract | Status | TP stage | Peak vs entry | Last vs entry | RSI | Volume x | Body % | ADX | MACD hist | ATR % |",
              "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        peak = f"{r['max_favorable_pct']:+.2f}%" if r["max_favorable_pct"] is not None else "n/a"
        last = f"{r['last_mark_pct']:+.2f}%" if r["last_mark_pct"] is not None else "n/a"
        rsi = f"{float(r['rsi']):.1f}" if r["rsi"] is not None else "missing"
        vol = f"{float(r['volume_ratio']):.2f}" if r["volume_ratio"] is not None else "missing"
        body = f"{float(r['candle_body_ratio'])*100:.0f}%" if r["candle_body_ratio"] is not None else "missing"
        adx = f"{float(r['adx']):.1f}" if r["adx"] is not None else "missing"
        macd_hist = f"{float(r['macd_hist']):.5f}" if r["macd_hist"] is not None else "missing"
        atr_pct = f"{float(r['atr_pct']):.2f}%" if r["atr_pct"] is not None else "missing"
        lines.append(f"| {r['coin']} | {r['contract']} | {r['status']} | {r['stage']} | {peak} | {last} | {rsi} | {vol} | {body} | {adx} | {macd_hist} | {atr_pct} |")
    lines += ["", "## Audit limitations", ""]
    lines.extend(f"- {item}" for item in report["interpretation"])
    lines += ["", "## Improvement gate", "",
              "No signal thresholds, expiry rules, spread limit, premium source, SL, or targets are changed by this checker.",
              "Any proposed change must be tested separately against a sufficiently large paper-trading sample and compared with the unchanged baseline.", ""]
    with open(MD_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Audit created: {MD_PATH} and {JSON_PATH}; tracked={total}; metrics={metric_count}; active={statuses.get('ACTIVE',0)}")

if __name__ == "__main__":
    main()
