#!/usr/bin/env python3
"""Produce a browserless Codex Meter report from Codex auth and usage APIs."""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


JsonObject = dict[str, Any]
ANALYTICS_URL = "https://chatgpt.com/backend-api/wham/analytics/daily-workspace-usage-counts"
DEFAULT_USD_PER_CREDIT = 40 / 1000


class QuotaForecastError(RuntimeError):
    pass


class AppServerClient:
    def __init__(self, codex_bin: str) -> None:
        self.messages: queue.Queue[JsonObject] = queue.Queue()
        self.stderr_lines: list[str] = []
        self.process = subprocess.Popen(
            [codex_bin, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for raw_line in self.process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                self.messages.put(message)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        for raw_line in self.process.stderr:
            line = raw_line.strip()
            if line:
                self.stderr_lines.append(line)
                del self.stderr_lines[:-12]

    def send(self, message: JsonObject) -> None:
        if self.process.poll() is not None:
            raise QuotaForecastError(
                f"Codex app-server exited with code {self.process.returncode}"
            )
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def wait_for(self, request_id: int, deadline: float) -> JsonObject:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                detail = self.stderr_lines[-1] if self.stderr_lines else "no server detail"
                raise QuotaForecastError(f"Timed out waiting for RPC {request_id}: {detail}")
            try:
                message = self.messages.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                if self.process.poll() is not None:
                    detail = self.stderr_lines[-1] if self.stderr_lines else "no server detail"
                    raise QuotaForecastError(
                        f"Codex app-server exited with code {self.process.returncode}: {detail}"
                    )
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"]
                if isinstance(error, dict):
                    error = error.get("message") or json.dumps(error, ensure_ascii=False)
                raise QuotaForecastError(f"RPC {request_id} failed: {error}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise QuotaForecastError(f"RPC {request_id} returned no object result")
            return result

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)


def read_rate_limits(codex_bin: str, timeout: float) -> tuple[JsonObject, Path]:
    client = AppServerClient(codex_bin)
    deadline = time.monotonic() + timeout
    try:
        client.send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "codex-quota-forecast", "version": "2.0.0"},
                    "capabilities": {"experimentalApi": True},
                },
            }
        )
        initialized = client.wait_for(1, deadline)
        codex_home = initialized.get("codexHome")
        if not isinstance(codex_home, str) or not codex_home:
            raise QuotaForecastError("Codex app-server did not return codexHome")
        client.send({"method": "initialized"})
        client.send({"id": 2, "method": "account/rateLimits/read"})
        return client.wait_for(2, deadline), Path(codex_home)
    finally:
        client.close()


def load_chatgpt_auth(codex_home: Path) -> tuple[str, str]:
    auth_path = codex_home / "auth.json"
    try:
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
        tokens = auth["tokens"]
        access_token = tokens["access_token"]
        account_id = tokens["account_id"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise QuotaForecastError(
            "Codex ChatGPT OAuth credentials are unavailable; sign in to Codex and retry"
        ) from error
    if not isinstance(access_token, str) or not access_token:
        raise QuotaForecastError("Codex access token is empty; sign in to Codex and retry")
    if not isinstance(account_id, str) or not account_id:
        raise QuotaForecastError("Codex account id is empty; sign in to Codex and retry")
    return access_token, account_id


def detect_macos_https_proxy() -> str | None:
    scutil = shutil.which("scutil")
    if not scutil:
        return None
    try:
        result = subprocess.run(
            [scutil, "--proxy"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        match = re.match(r"\s*([A-Za-z]+)\s*:\s*(.*?)\s*$", line)
        if match:
            values[match.group(1)] = match.group(2)
    if values.get("HTTPSEnable") != "1":
        return None
    host = values.get("HTTPSProxy")
    port = values.get("HTTPSPort")
    if not host or not port:
        return None
    return f"http://{host}:{port}"


def resolve_https_proxy(explicit_proxy: str | None, no_proxy: bool) -> str | None:
    if no_proxy:
        return None
    if explicit_proxy:
        return explicit_proxy
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = os.environ.get(name)
        if value:
            return value
    if sys.platform == "darwin":
        return detect_macos_https_proxy()
    return None


def proxy_display_value(proxy_url: str | None) -> str | None:
    """Return a credential-free proxy description for report metadata."""
    if not proxy_url:
        return None
    try:
        parsed = urllib.parse.urlsplit(proxy_url)
        host = parsed.hostname
        if not host:
            return "configured"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        scheme = f"{parsed.scheme}://" if parsed.scheme else ""
        return f"{scheme}{host}{port}"
    except (TypeError, ValueError):
        return "configured"


def fetch_daily_usage(
    access_token: str,
    account_id: str,
    start_date: date,
    end_date: date,
    timeout: float,
    proxy_url: str | None,
) -> list[JsonObject]:
    query = urllib.parse.urlencode(
        {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "group_by": "day",
        }
    )
    url = f"{ANALYTICS_URL}?{query}"
    curl = shutil.which("curl")
    if curl:
        payload = fetch_json_with_curl(
            curl,
            url,
            access_token,
            account_id,
            timeout,
            proxy_url,
        )
    else:
        payload = fetch_json_with_urllib(
            url,
            access_token,
            account_id,
            timeout,
            proxy_url,
        )
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise QuotaForecastError("Analytics API response has no data list")
    return [row for row in rows if isinstance(row, dict)]


def curl_config_quote(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise QuotaForecastError("Refusing an unsafe newline in HTTP configuration")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def fetch_json_with_curl(
    curl: str,
    url: str,
    access_token: str,
    account_id: str,
    timeout: float,
    proxy_url: str | None,
) -> JsonObject:
    config_lines = [
        f'url = "{curl_config_quote(url)}"',
        f'header = "Authorization: Bearer {curl_config_quote(access_token)}"',
        f'header = "ChatGPT-Account-ID: {curl_config_quote(account_id)}"',
        'header = "Accept: application/json"',
        'header = "User-Agent: CodexQuotaForecast/2.0"',
        f"max-time = {max(1, int(timeout))}",
        f"connect-timeout = {max(1, min(15, int(timeout)))}",
        "silent",
        "show-error",
        "location",
    ]
    if proxy_url:
        config_lines.append(f'proxy = "{curl_config_quote(proxy_url)}"')
    marker = "\n__CODEX_QUOTA_HTTP_STATUS__:"
    try:
        result = subprocess.run(
            [curl, "--config", "-", "--write-out", f"{marker}%{{http_code}}"],
            input="\n".join(config_lines) + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout + 5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise QuotaForecastError(f"Could not execute the analytics request: {error}") from error
    body, separator, status_text = result.stdout.rpartition(marker)
    if not separator:
        detail = result.stderr.strip() or f"curl exited with code {result.returncode}"
        raise QuotaForecastError(f"Could not reach the analytics API: {detail}")
    try:
        status = int(status_text.strip())
    except ValueError as error:
        raise QuotaForecastError("Analytics request returned an invalid HTTP status") from error
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise QuotaForecastError("Analytics API returned invalid JSON") from error
    if status >= 400:
        detail = payload.get("detail") or payload.get("message") or payload.get("error")
        raise QuotaForecastError(f"Analytics API returned HTTP {status}: {detail or 'unknown error'}")
    if not isinstance(payload, dict):
        raise QuotaForecastError("Analytics API returned a non-object JSON payload")
    return payload


def fetch_json_with_urllib(
    url: str,
    access_token: str,
    account_id: str,
    timeout: float,
    proxy_url: str | None,
) -> JsonObject:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "ChatGPT-Account-ID": account_id,
            "Accept": "application/json",
            "User-Agent": "CodexQuotaForecast/2.0",
        },
    )
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read().decode("utf-8"))
            detail = body.get("detail") or body.get("message") or body.get("error")
        except Exception:
            detail = None
        raise QuotaForecastError(
            f"Analytics API returned HTTP {error.code}: {detail or error.reason}"
        ) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        proxy_note = f" via {proxy_url}" if proxy_url else " without a detected HTTPS proxy"
        raise QuotaForecastError(f"Could not reach the analytics API{proxy_note}: {error}") from error
    except json.JSONDecodeError as error:
        raise QuotaForecastError("Analytics API returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise QuotaForecastError("Analytics API returned a non-object JSON payload")
    return payload


def number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result


def integer(value: Any) -> int:
    return int(number(value))


def token_total(totals: JsonObject) -> int:
    provided = integer(totals.get("text_total_tokens"))
    if provided:
        return provided
    return (
        integer(totals.get("cached_text_input_tokens"))
        + integer(totals.get("uncached_text_input_tokens"))
        + integer(totals.get("text_output_tokens"))
    )


def token_input(totals: JsonObject) -> int:
    return integer(totals.get("cached_text_input_tokens")) + integer(
        totals.get("uncached_text_input_tokens")
    )


def cache_ratio(totals: JsonObject) -> float:
    cached = integer(totals.get("cached_text_input_tokens"))
    input_tokens = token_input(totals)
    return cached / input_tokens if input_tokens else 0.0


def normalize_client(client: JsonObject) -> JsonObject:
    return {
        "client_id": client.get("client_id"),
        "credits": number(client.get("credits")),
        "total_tokens": token_total(client),
        "input_tokens": token_input(client),
        "cache_hit_percent": cache_ratio(client) * 100,
        "turns": integer(client.get("turns")),
        "threads": integer(client.get("threads")),
    }


def normalize_model(model: JsonObject) -> JsonObject:
    return {
        "model": model.get("model"),
        "credits": number(model.get("credits")),
        "turns": integer(model.get("turns")),
        "threads": integer(model.get("threads")),
    }


def normalize_daily_row(row: JsonObject, usd_per_credit: float) -> JsonObject:
    totals = row.get("totals") if isinstance(row.get("totals"), dict) else {}
    credits = number(totals.get("credits"))
    cached = integer(totals.get("cached_text_input_tokens"))
    uncached = integer(totals.get("uncached_text_input_tokens"))
    output = integer(totals.get("text_output_tokens"))
    clients = row.get("clients") if isinstance(row.get("clients"), list) else []
    models = row.get("models") if isinstance(row.get("models"), list) else []
    return {
        "date": str(row.get("date") or ""),
        "credits": credits,
        "total_tokens": token_total(totals),
        "input_tokens": cached + uncached,
        "cached_input_tokens": cached,
        "uncached_input_tokens": uncached,
        "output_tokens": output,
        "cache_hit_percent": cache_ratio(totals) * 100,
        "estimated_value_usd": credits * usd_per_credit,
        "turns": integer(totals.get("turns")),
        "threads": integer(totals.get("threads")),
        "clients": [normalize_client(item) for item in clients if isinstance(item, dict)],
        "models": [normalize_model(item) for item in models if isinstance(item, dict)],
    }


def stats_for_rows(rows: list[JsonObject], usd_per_credit: float) -> JsonObject:
    credits = sum(number(row.get("credits")) for row in rows)
    total_tokens = sum(integer(row.get("total_tokens")) for row in rows)
    input_tokens = sum(integer(row.get("input_tokens")) for row in rows)
    cached = sum(integer(row.get("cached_input_tokens")) for row in rows)
    uncached = sum(integer(row.get("uncached_input_tokens")) for row in rows)
    output = sum(integer(row.get("output_tokens")) for row in rows)
    return {
        "credits": credits,
        "total_tokens": total_tokens,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "uncached_input_tokens": uncached,
        "output_tokens": output,
        "cache_hit_percent": cached / input_tokens * 100 if input_tokens else 0.0,
        "estimated_value_usd": credits * usd_per_credit,
        "turns": sum(integer(row.get("turns")) for row in rows),
        "threads": sum(integer(row.get("threads")) for row in rows),
        "credits_per_million_tokens": credits / (total_tokens / 1_000_000)
        if total_tokens
        else 0.0,
    }


def epoch_iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    zone = datetime.now().astimezone().tzinfo
    return datetime.fromtimestamp(epoch, zone).isoformat(timespec="seconds")


def select_weekly_window(snapshot: JsonObject) -> tuple[str | None, JsonObject | None]:
    candidates: list[tuple[str, JsonObject]] = []
    for name in ("primary", "secondary"):
        value = snapshot.get(name)
        if isinstance(value, dict):
            candidates.append((name, value))
    if not candidates:
        return None, None
    candidates.sort(
        key=lambda item: number(item[1].get("windowDurationMins")),
        reverse=True,
    )
    return candidates[0]


def summarize_bucket(bucket_id: str, snapshot: JsonObject) -> JsonObject:
    window_name, window = select_weekly_window(snapshot)
    used = number(window.get("usedPercent")) if window else 0.0
    reset_at = number(window.get("resetsAt")) if window else 0.0
    return {
        "limit_id": snapshot.get("limitId") or bucket_id,
        "limit_name": snapshot.get("limitName"),
        "plan_type": snapshot.get("planType"),
        "window": window_name,
        "used_percent": used,
        "remaining_percent": max(0.0, 100.0 - used),
        "window_duration_mins": number(window.get("windowDurationMins")) if window else None,
        "reset_at": epoch_iso(reset_at) if reset_at else None,
    }


def extract_primary_limit(rate_response: JsonObject) -> tuple[str, JsonObject, list[JsonObject]]:
    legacy = rate_response.get("rateLimits")
    by_id = rate_response.get("rateLimitsByLimitId")
    buckets: dict[str, JsonObject] = {}
    if isinstance(by_id, dict):
        buckets = {str(key): value for key, value in by_id.items() if isinstance(value, dict)}
    if isinstance(legacy, dict) and "codex" not in buckets:
        buckets[str(legacy.get("limitId") or "codex")] = legacy
    if not buckets:
        raise QuotaForecastError("The server returned no Codex rate-limit buckets")
    primary_id = "codex" if "codex" in buckets else next(iter(buckets))
    other = [
        summarize_bucket(bucket_id, snapshot)
        for bucket_id, snapshot in buckets.items()
        if bucket_id != primary_id
    ]
    return primary_id, buckets[primary_id], other


def median_positive(values: list[float]) -> float:
    sorted_values = sorted(value for value in values if value > 0)
    if not sorted_values:
        return 0.0
    middle = len(sorted_values) // 2
    if len(sorted_values) % 2:
        return sorted_values[middle]
    return (sorted_values[middle - 1] + sorted_values[middle]) / 2


def projection_confidence(
    used_percent: float,
    cycle_age_hours: float,
    estimate: float | None,
    current_credits: float,
    history_rows: list[JsonObject],
) -> str | None:
    if estimate is None:
        return None
    recent = sorted(history_rows, key=lambda row: row.get("date", ""), reverse=True)[:7]
    recent_credits = [number(row.get("credits")) for row in recent]
    recent_total = sum(recent_credits)
    recent_median = median_positive(recent_credits)
    daily_looks_incomplete = recent_median > 0 and current_credits < recent_median * 0.2
    estimate_looks_too_low = recent_total > 0 and estimate < recent_total * 0.25
    if (
        used_percent < 10
        or cycle_age_hours < 8
        or daily_looks_incomplete
        or estimate_looks_too_low
    ):
        return "low"
    if used_percent < 20 or cycle_age_hours < 24:
        return "medium"
    return "high"


def build_report(
    rate_response: JsonObject,
    raw_daily_rows: list[JsonObject],
    now: datetime,
    start_date: date,
    end_date: date,
    usd_per_credit: float,
    proxy_url: str | None,
) -> JsonObject:
    primary_id, primary, other_limits = extract_primary_limit(rate_response)
    window_name, window = select_weekly_window(primary)
    if not window:
        raise QuotaForecastError(f"Rate-limit bucket {primary_id!r} has no usable window")
    used = number(window.get("usedPercent"))
    duration_mins = number(window.get("windowDurationMins"))
    reset_at = number(window.get("resetsAt"))
    if not duration_mins or not reset_at:
        raise QuotaForecastError("The weekly window is missing duration or reset time")

    now_epoch = now.timestamp()
    duration_seconds = duration_mins * 60
    cycle_start_epoch = reset_at - duration_seconds
    cycle_start_local = datetime.fromtimestamp(cycle_start_epoch).astimezone()
    cycle_start_date = cycle_start_local.date()
    elapsed_seconds = max(0.0, min(now_epoch - cycle_start_epoch, duration_seconds))
    elapsed_fraction = elapsed_seconds / duration_seconds
    remaining = max(0.0, 100.0 - used)
    even_pace_used = elapsed_fraction * 100
    pace_multiple = used / even_pace_used if even_pace_used else None
    projected_end_percent = used / elapsed_fraction if elapsed_fraction else None
    exhaustion_at: float | None = None
    if used >= 100:
        exhaustion_at = now_epoch
    elif used > 0 and elapsed_seconds > 0:
        exhaustion_at = cycle_start_epoch + elapsed_seconds / (used / 100)
    will_exhaust = exhaustion_at is not None and exhaustion_at < reset_at

    normalized_rows = sorted(
        (normalize_daily_row(row, usd_per_credit) for row in raw_daily_rows),
        key=lambda row: row["date"],
    )
    current_rows = [row for row in normalized_rows if row["date"] >= cycle_start_date.isoformat()]
    history_rows = [row for row in normalized_rows if row["date"] < cycle_start_date.isoformat()]
    current_stats = stats_for_rows(current_rows, usd_per_credit)
    history_stats = stats_for_rows(history_rows, usd_per_credit)
    total_stats = stats_for_rows(normalized_rows, usd_per_credit)

    current_credits = number(current_stats.get("credits"))
    projected_weekly_credits = current_credits / (used / 100) if used > 0 and current_credits > 0 else None
    confidence = projection_confidence(
        used,
        elapsed_seconds / 3600,
        projected_weekly_credits,
        current_credits,
        history_rows,
    )
    latest_data_date = date.fromisoformat(normalized_rows[-1]["date"]) if normalized_rows else None
    data_lag_days = (now.astimezone().date() - latest_data_date).days if latest_data_date else None

    if used >= 100:
        status = "exhausted"
    elif will_exhaust:
        status = "likely_exhaust_before_reset"
    else:
        status = "likely_last_until_reset"

    return {
        "captured_at": now.astimezone().isoformat(timespec="seconds"),
        "account": {
            "plan_type": primary.get("planType"),
            "primary_limit_id": primary_id,
            "extra_credits": primary.get("credits"),
        },
        "quota": {
            "window": window_name,
            "used_percent": used,
            "remaining_percent": remaining,
            "cycle_start_at": epoch_iso(cycle_start_epoch),
            "cycle_start_date": cycle_start_date.isoformat(),
            "reset_at": epoch_iso(reset_at),
            "hours_until_reset": max(0.0, (reset_at - now_epoch) / 3600),
            "window_duration_mins": duration_mins,
        },
        "forecast": {
            "pace_multiple": pace_multiple,
            "projected_end_percent": projected_end_percent,
            "estimated_exhaustion_at": epoch_iso(exhaustion_at),
            "will_exhaust_before_reset": will_exhaust,
            "status": status,
            "projected_weekly_credits": projected_weekly_credits,
            "projected_weekly_value_usd": projected_weekly_credits * usd_per_credit
            if projected_weekly_credits is not None
            else None,
            "confidence": confidence,
            "credits_method": "current-cycle Credits divided by official used percent",
            "exhaustion_method": "linear official quota burn over elapsed cycle time",
        },
        "current_cycle": {
            "start_date": cycle_start_date.isoformat(),
            "latest_data_date": latest_data_date.isoformat() if latest_data_date else None,
            "stats": current_stats,
            "daily": list(reversed(current_rows)),
        },
        "history": {
            "start_date": history_rows[0]["date"] if history_rows else None,
            "end_date": history_rows[-1]["date"] if history_rows else None,
            "stats": history_stats,
            "daily": list(reversed(history_rows)),
        },
        "lookback": {
            "start_date": start_date.isoformat(),
            "end_date_exclusive": end_date.isoformat(),
            "days_returned": len(normalized_rows),
            "latest_data_date": latest_data_date.isoformat() if latest_data_date else None,
            "data_lag_days": data_lag_days,
            "stats": total_stats,
        },
        "other_limits": other_limits,
        "pricing": {"usd_per_credit": usd_per_credit},
        "transport": {
            "browser_opened": False,
            "analytics_endpoint": ANALYTICS_URL,
            "https_proxy_used": proxy_display_value(proxy_url),
            "token_exposed": False,
        },
    }


def format_number(value: Any) -> str:
    number_value = number(value)
    absolute = abs(number_value)
    if absolute >= 1_000_000_000:
        return f"{number_value / 1_000_000_000:.2f}B"
    if absolute >= 1_000_000:
        return f"{number_value / 1_000_000:.2f}M"
    if absolute >= 1_000:
        return f"{number_value / 1_000:.2f}K"
    return f"{number_value:,.0f}"


def format_percent(value: Any, digits: int = 1) -> str:
    value_number = number(value)
    return f"{value_number:.{digits}f}%"


def table_lines(rows: list[JsonObject], stats: JsonObject, limit: int | None = None) -> list[str]:
    visible = rows if limit is None else rows[:limit]
    lines = [
        "日期 | Credits | 总 Tokens | 输入 Tokens | 缓存命中 | 折算价值 | Turns",
        "--- | ---: | ---: | ---: | ---: | ---: | ---:",
    ]
    for row in visible:
        lines.append(
            f"{row['date']} | {row['credits']:.3f} | {format_number(row['total_tokens'])} | "
            f"{format_number(row['input_tokens'])} | {format_percent(row['cache_hit_percent'], 0)} | "
            f"$ {row['estimated_value_usd']:.2f} | {row['turns']}"
        )
    lines.append(
        f"合计 | {stats['credits']:.3f} | {format_number(stats['total_tokens'])} | "
        f"{format_number(stats['input_tokens'])} | {format_percent(stats['cache_hit_percent'], 0)} | "
        f"$ {stats['estimated_value_usd']:.2f} | {stats['turns']}"
    )
    if limit is not None and len(rows) > limit:
        lines.append(f"（另有 {len(rows) - limit} 天未在文本模式展开；JSON 模式包含全部记录。）")
    return lines


def render_text(report: JsonObject, history_limit: int) -> str:
    quota = report["quota"]
    forecast = report["forecast"]
    current = report["current_cycle"]
    current_stats = current["stats"]
    history = report["history"]
    confidence_labels = {"low": "低", "medium": "中", "high": "高", None: "不可用"}
    lines = [
        "Codex Meter 详细报告",
        "",
        "额度概览",
        f"- 已用：{format_percent(quota['used_percent'])}",
        f"- 剩余：{format_percent(quota['remaining_percent'])}",
        f"- 周期：{quota['cycle_start_at']} → {quota['reset_at']}",
        f"- 距离重置：{quota['hours_until_reset']:.1f} 小时",
        "",
        "本周期核心指标",
        f"- 已用 Credits：{current_stats['credits']:.3f}",
        f"- 总 Tokens：{format_number(current_stats['total_tokens'])}",
        f"- 输入 Tokens：{format_number(current_stats['input_tokens'])}",
        f"- 缓存输入：{format_number(current_stats['cached_input_tokens'])}",
        f"- 非缓存输入：{format_number(current_stats['uncached_input_tokens'])}",
        f"- 输出 Tokens：{format_number(current_stats['output_tokens'])}",
        f"- 输入缓存命中率：{format_percent(current_stats['cache_hit_percent'])}",
        f"- Turns / Threads：{current_stats['turns']} / {current_stats['threads']}",
        f"- 本周期折算价值：$ {current_stats['estimated_value_usd']:.2f}",
        "",
        "本周预测",
    ]
    if forecast["projected_weekly_credits"] is not None:
        lines.extend(
            [
                f"- 推算周总 Credits：{forecast['projected_weekly_credits']:.3f}",
                f"- 推算周总价值：$ {forecast['projected_weekly_value_usd']:.2f}",
                f"- Credits 预测可信度：{confidence_labels[forecast['confidence']]}",
            ]
        )
    else:
        lines.append("- 周总 Credits：暂不可估算")
    if forecast["pace_multiple"] is not None:
        lines.append(f"- 当前额度速度：均匀速度的 {forecast['pace_multiple']:.2f} 倍")
    if forecast["projected_end_percent"] is not None:
        lines.append(f"- 周期末额度需求：{forecast['projected_end_percent']:.1f}%")
    if forecast["will_exhaust_before_reset"]:
        lines.append(f"- 预计耗尽：{forecast['estimated_exhaustion_at']}（早于重置）")
    else:
        lines.append("- 预计结果：按当前速度可维持到重置")

    lines.extend(["", f"本周期每日用量（从 {current['start_date']} 起）"])
    lines.extend(table_lines(current["daily"], current_stats))

    lines.extend(
        [
            "",
            f"周期外历史用量（{history['start_date']} 至 {history['end_date']}）",
        ]
    )
    lines.extend(table_lines(history["daily"], history["stats"], history_limit or None))

    other_limits = report.get("other_limits") or []
    if other_limits:
        lines.extend(["", "其他独立额度"])
        for item in other_limits:
            label = item.get("limit_name") or item.get("limit_id")
            lines.append(
                f"- {label}：已用 {format_percent(item['used_percent'])}，"
                f"剩余 {format_percent(item['remaining_percent'])}，重置 {item['reset_at']}"
            )

    lookback = report["lookback"]
    lines.extend(
        [
            "",
            f"数据时间：{report['captured_at']}；明细最新到 {lookback['latest_data_date']}，"
            f"滞后 {lookback['data_lag_days']} 天。",
            "注：Credits 折算按 $40/1000；周总 Credits 与耗尽时间均为估算，不是服务端保证。",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Return a detailed Codex Meter report without opening ChatGPT Web."
    )
    parser.add_argument("--json", action="store_true", help="Print full machine-readable JSON")
    parser.add_argument("--timeout", type=float, default=90, help="RPC and HTTP timeout in seconds")
    parser.add_argument("--lookback-days", type=int, default=45, help="History lookback in days")
    parser.add_argument(
        "--history-limit",
        type=int,
        default=14,
        help="History rows shown in text mode; use 0 for all",
    )
    parser.add_argument(
        "--usd-per-credit",
        type=float,
        default=DEFAULT_USD_PER_CREDIT,
        help="USD value used for Credit conversion",
    )
    parser.add_argument("--proxy", help="Explicit HTTPS proxy URL")
    parser.add_argument("--no-proxy", action="store_true", help="Disable proxy auto-detection")
    parser.add_argument("--codex-bin", help="Path to the Codex binary")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.lookback_days < 1:
        print("codex-quota-forecast: --lookback-days must be positive", file=sys.stderr)
        return 2
    if args.history_limit < 0:
        print("codex-quota-forecast: --history-limit cannot be negative", file=sys.stderr)
        return 2
    codex_bin = args.codex_bin or shutil.which("codex")
    if not codex_bin:
        print("codex-quota-forecast: Codex executable not found", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)
    today = now.astimezone().date()
    start_date = today - timedelta(days=args.lookback_days)
    end_date = today + timedelta(days=1)
    try:
        rate_limits, codex_home = read_rate_limits(codex_bin, args.timeout)
        access_token, account_id = load_chatgpt_auth(codex_home)
        proxy_url = resolve_https_proxy(args.proxy, args.no_proxy)
        raw_rows = fetch_daily_usage(
            access_token,
            account_id,
            start_date,
            end_date,
            args.timeout,
            proxy_url,
        )
        report = build_report(
            rate_limits,
            raw_rows,
            now,
            start_date,
            end_date,
            args.usd_per_credit,
            proxy_url,
        )
    except (QuotaForecastError, OSError) as error:
        print(f"codex-quota-forecast: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report, args.history_limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
