---
name: rb-status
description: Status of the riverbank acceptance runs (M4 demo, M3 shadow, M1) in one table, M1 interim report included. Use when the operator asks for a status summary ("สรุปสถานะ"), the M1 interim report, PnL so far, or when a scheduled status check fires.
---

# rb-status

Read-only. Restart, fix, push and live trading are out of scope here: report what needs action and ask.

## Steps

1. Run `python3 .claude/skills/rb-status/status.py` from the repo root (one pass, ~1 s). Done when it prints all six sections: containers, demo, shadow, prometheus, M1, host.
2. Reply in Thai with one table, columns **M4 demo | M3 shadow | M1**, rows:
   - สถานะ + รันมาแล้ว/ครบเมื่อ (elapsed of the window, end time in Thai time = UTC+7)
   - fill (demo: real + twin; shadow: paper)
   - กำไร/ขาดทุน: shadow in USDT and %; demo marked *(ไม่มีความหมาย, demo)*
   - realized edge: shadow fill-weighted avg, best/worst 3 with their fill counts
   - hedge / hedge ที่ถูกปฏิเสธ, risk events
   - M1 rows: L1–L5 match % (checks), undetected mismatches, gaps / reconnects / rolls / errors, rows written
   Done when every row is filled for every run that has the data.
3. Below the table, only what changed or looks wrong, each with its cause (dig into `docker logs` when the cause is not obvious). When nothing changed since the previous summary in this conversation, say so in one line.

## Reading the numbers

- **Window reset**: a `WINDOW_RESET` flag means that run's acceptance clock restarted; put it first. After any deliberate restart, update `start` in `windows.json`.
- **PnL**: demo runs on demo-exchange prices and fake money (OPEN_QUESTIONS B24), so only shadow PnL and edge carry information, and even those are noise until days of fills. Promotion needs 300 shadow fills per symbol.
- **REDUCE_ONLY** that recovers within minutes is the risk engine working (soft breach near a position/gross cap); HALTED, or REDUCE_ONLY past ~15 min, is a problem.
- **Shadow churn**: target 0–2 symbol changes per screen (median spread + hysteresis since `85a10fc`). A stream drop at the same second as a rescreen is the resubscribe, not a network drop.
- **M1**: the running M1 predates `bb2203d`, so its interim report says "Parquet readable: FAIL / Parquet files: 0": the interim skips the parquet scan; the final report scans for real. Resyncs that grow slowly with no detected gaps are the scheduled book refresh. First WS roll lands near hour 23. Once `summary.json` exists, report the final verdict instead.
- **Rate limits**: `http429_418` counts status fields only; bare 418/429 appear inside prices.
- **Demo fills pausing**: check `inventory_usdt` near ±50 (cap) with one-sided `open_orders`: quoting reduce-side only, not stuck.
