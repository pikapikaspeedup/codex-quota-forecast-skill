---
name: codex-quota-forecast
description: Return a detailed Codex Meter report without opening ChatGPT Web, including official quota percentage and reset time, current-cycle and historical daily Credits, total/input/cached/uncached/output Tokens, cache hit rate, Turns, Threads, estimated USD value, projected weekly Credits/value, burn rate, and likely exhaustion time. Use when the user asks about Codex quota, remaining weekly allowance, usage details, token or Credit consumption, cache rate, Codex cost/value, daily history, weekly forecast, or whether the quota will last.
---

# Codex Quota Forecast

Read official quota through Codex's local app-server, then use the refreshed Codex OAuth credential in memory to request the same private daily analytics endpoint as Codex Meter. Never print, return, persist, or put the access token in a command argument.

## Run the check

1. Resolve `scripts/quota_forecast.py` relative to this `SKILL.md`.
2. Run:

   ```bash
   python3 scripts/quota_forecast.py --json
   ```

3. The JSON result contains the full 45-day lookback. Use text mode only when a ready-made Markdown-style report is more convenient:

   ```bash
   python3 scripts/quota_forecast.py
   ```

4. Report the primary `codex` bucket first. Report other named buckets separately because model-specific quotas do not share the primary percentage.

## Return the complete report

Present these sections unless the user explicitly asks for a shorter answer:

1. Quota overview: used, remaining, cycle start, reset time, and time until reset.
2. Current-cycle core metrics: Credits, total Tokens, input Tokens, cached/uncached input, output Tokens, cache hit rate, Turns, Threads, and estimated value.
3. Weekly forecast: projected total Credits, projected value, confidence, current pace multiple, projected quota demand, and estimated exhaustion time.
4. Current-cycle daily table with date, Credits, total Tokens, input Tokens, cache hit, estimated value, and Turns, followed by totals.
5. Usage outside the cycle: history range, aggregate totals, and recent daily rows. Include all rows only when requested; otherwise show the latest 7-14 rows and say the JSON contains the full lookback.
6. Other independent quota buckets.
7. Capture time, latest analytics date, data lag, the `$40/1000 Credits` conversion basis, and a forecast disclaimer.

## Interpret the result

- Treat `quota.used_percent`, `quota.remaining_percent`, and `quota.reset_at` as server-provided facts.
- Treat `current_cycle.stats` and daily rows as analytics data returned by the private endpoint.
- Treat `forecast.projected_weekly_credits` as current-cycle Credits divided by official used percentage, matching Codex Meter.
- Treat `forecast.pace_multiple`, `forecast.projected_end_percent`, and `forecast.estimated_exhaustion_at` as linear forecasts, not guaranteed limits.
- State `captured_at` because quota changes while Codex is in use.
- If `forecast.will_exhaust_before_reset` is true, lead with the estimated exhaustion time and remaining percentage.
- Preserve percentages above 100 in `forecast.projected_end_percent`; they express demand relative to the available weekly allowance.
- Mention confidence. The script labels early-cycle or low-usage forecasts as lower confidence.
- Daily analytics can lag. State `lookback.latest_data_date` and `lookback.data_lag_days`.
- Keep the cycle boundary caveat: daily rows are calendar-day buckets, so the first cycle day can include usage before the exact reset hour.

## Failure handling

- If Codex is not logged in, ask the user to sign in to Codex and rerun the check.
- If the RPC times out, rerun once with `--timeout 150`.
- If direct TLS fails on macOS, allow the script to auto-detect the system HTTPS proxy. Use `--proxy URL` only when auto-detection fails.
- Do not fall back to opening a browser.
- Do not expose bearer credentials in output, logs, shell arguments, or files.
