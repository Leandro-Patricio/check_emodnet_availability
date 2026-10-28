"""EMODnet service availability checks used by the TSL generator."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
from generate_status_dashboard import generate_status_dashboard

import requests


DEFAULT_MONITOR_URL = "https://monitor.emodnet.eu/resource/55/json"
DEFAULT_REQUEST_TIMEOUT = 60
DISCORD_REQUEST_TIMEOUT = 10
STALE_REPORT_THRESHOLD_MINUTES = 60  # monitor report older than this can't be trusted
RELIABILITY_WARNING_THRESHOLD = 95.0  # percent, logged only, not a hard failure

# Real data host used by the generator, distinct from the host the monitor above tracks.
PLATFORM_API_URL = "https://platform-erddap.emodnet-physics.eu/api/parameters/{parameter}/data"
DASHBOARD_URL = "https://leandro-patricio.github.io/check_emodnet_availability/dashboard.html"
PROBE_PLATFORM_CODE = "cent2"  # known-good reference station
PROBE_WINDOW_DAYS_AGO = 3  # buoy reports lag behind "now" by a few days
PROBE_WINDOW_HOURS = 24

HISTORY_FILE = Path("history.json")

# --- Global state ---
STATUS_REPORT: Dict[str, List[Dict[str, str]]] = {"checks": []}


def record_result(name: str, passed: bool, details: str, url: Optional[str] = None) -> None:
    """Record test result into global status report with optional target URL."""
    STATUS_REPORT["checks"].append(
        {
            "name": name,
            "status": "PASS" if passed else "FAIL",
            "details": details,
            "icon": "✅" if passed else "❌",
            "url": url,
        }
    )


def update_execution_history() -> None:
    """Load full historical log, append compact record permanently, and persist."""
    history = []

    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    history = data
                elif isinstance(data, dict):
                    history = data.get("history", [])
        except (json.JSONDecodeError, OSError):
            history = []

    attempt_number = len(history) + 1
    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")

    alias_map = {
        "Resource Monitor": "monitor",
        # "API Datasets": "datasets",
        "API Data (cent2)": "data",
    }

    compact_tests = {}
    for item in STATUS_REPORT["checks"]:
        key = alias_map.get(item["name"], item["name"])
        compact_tests[key] = True if item["status"] == "PASS" else item["details"]

    current_status = all(item["status"] == "PASS" for item in STATUS_REPORT["checks"])

    record = {
        "attempt": attempt_number,
        "ts": current_time,
        "ok": current_status,
        "tests": compact_tests,
    }

    history.append(record)

    # Update totals
    # total_rounds = len(history)
    # total_true = sum(1 for entry in history if entry.get("ok") is True)
    # total_false = total_rounds - total_true

    metadata = {
            "last_status": current_status,
            "last_updated": current_time,
            # "total_runs": total_rounds,
            # "successful_runs": total_true,
            # "failed_runs": total_false,
        }

    payload = {
        "metadata": metadata,
        "history": history,
    }

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    print(f"History successfully appended: attempt #{attempt_number} (total records: {len(history)})")


def is_monitor_available(
    monitor_url: Optional[str] = None,
    timeout: Optional[float] = None,
) -> bool:
    """Return whether both the EMODnet monitor and the real data API are healthy."""
    monitor_url = monitor_url or os.getenv("EMODNET_MONITOR_URL", DEFAULT_MONITOR_URL)
    timeout = timeout or float(os.getenv("EMODNET_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT))

    print("Checking EMODnet monitor status...")

    try:
        responseMonitor = requests.get(monitor_url, timeout=timeout)
    except requests.exceptions.RequestException as error:
        return _unavailable("EMODnet Monitor", f"Could not connect to the EMODnet monitor: {error}", url=monitor_url)

    if responseMonitor.status_code != 200:
        return _unavailable("EMODnet Monitor", f"EMODnet monitor returned HTTP {responseMonitor.status_code}.", url=monitor_url)

    try:
        data = responseMonitor.json()
    except ValueError as error:
        return _unavailable("EMODnet Monitor", f"EMODnet monitor returned invalid JSON: {error}", url=monitor_url)

    if not isinstance(data, dict):
        return _unavailable("EMODnet Monitor", "EMODnet monitor returned an unexpected JSON payload.", url=monitor_url)

    # A stale report means the monitor stopped probing; its "status" can't be trusted.
    last_run = data.get("last_run")
    if last_run:
        try:
            last_run_dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
            age = datetime.now(timezone.utc) - last_run_dt
            if age > timedelta(minutes=STALE_REPORT_THRESHOLD_MINUTES):
                return _unavailable("EMODnet Monitor", f"EMODnet monitor report is stale ({age} old).", url=monitor_url)
        except ValueError:
            pass

    if data.get("status") is not True:
        last_report = data.get("last_report") or {}
        message = last_report.get("message", "The monitor reported an unknown error.")
        return _unavailable("EMODnet Monitor", f"EMODnet monitor reports an issue: {message}", url=monitor_url)

    reliability = data.get("reliability")
    if isinstance(reliability, (int, float)) and reliability < RELIABILITY_WARNING_THRESHOLD:
        print(f"Warning: EMODnet monitor reliability is degraded ({reliability:.1f}%).")

    print("EMODnet monitor is healthy.")
    record_result("EMODnet Monitor", True, "Healthy", url=monitor_url)
    return True



def is_individual_buoy_available(
    platform_code: str = PROBE_PLATFORM_CODE,
    timeout: Optional[float] = None,
) -> bool:
    """Probe the actual data host with a small, recent SLEV request."""
    timeout = timeout or float(os.getenv("EMODNET_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT))

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=PROBE_WINDOW_DAYS_AGO)
    params = {
        "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_time": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platform_code": platform_code,
        "format": "csv",
    }

    print(f"Checking EMODnet API Data " + platform_code + " status...")

    try:
        responseSpecificBuoy = requests.get(
            PLATFORM_API_URL.format(parameter="SLEV"), params=params, timeout=timeout
        )
        print(f"Querying EMODNET platform API: {responseSpecificBuoy.url}")
        responseSpecificBuoy.raise_for_status()
    except requests.exceptions.RequestException as error:
        return _unavailable(f"Could not reach the EMODnet platform data API: {error}", url=responseSpecificBuoy.url)

    lines = [line for line in responseSpecificBuoy.text.splitlines() if line.strip()]
    if len(lines) <= 1:
         return _unavailable("EMODnet platform data API returned no data rows for the probe window.", url=responseSpecificBuoy.url)

    print("✅ EMODnet API Data", platform_code, " is healthy.")
    record_result("API Data" + platform_code, True, f"Healthy ({len(lines)-1} rows)", url=responseSpecificBuoy.url)
    return True


def send_discord_alert() -> None:
    """Send plain text table alert using full Discord message width."""
    webhook_url = os.getenv("EMODNET_DISCORD_WEBHOOK_URL")
    if not webhook_url:
        return

    checks = STATUS_REPORT["checks"]
    has_failure = any(item["status"] == "FAIL" for item in checks)

    header = f"{'STATUS':<6} | {'TEST NAME':<25} | DETAILS"
    divisor = f"{'-'*7}-+-{'-'*25}-+-{'-'*45}"

    lines = [header, divisor]
    links = [f"🔗 [Dashboard]({DASHBOARD_URL})"]
    for item in checks:
        icon = "✅ PASS" if item["status"] == "PASS" else "❌ FAIL"
        lines.append(f"{icon:<6} | {item['name']:<25} | {item['details']}")
        
        # Create clickable links for Discord if a URL is provided
        url = item.get("url")
        if url:
            links.append(f"🔗 [{item['name']}]({url})")

    table = "\n".join(lines)
    date_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    title = f"**EMODnet Pipeline Alert** - {date_time}" if has_failure else f"✅ **EMODnet Pipeline OK** - {date_time}"
    
    # Links clickable in Discord using Markdown formatting
    links_text = " • ".join(links)

    payload = {
        "content": f"{title}\n```text\n{table}\n```\n{links_text}"
    }

    try:
        response = requests.post(
            webhook_url,
            json=payload,
            timeout=DISCORD_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as error:
        print(f"Could not send Discord notification: {error}")



def _unavailable(name: str, message: str, url: Optional[str] = None) -> bool:
    print(f"EMODnet is unavailable: {message}")
    record_result(name, False, message, url=url)
    return False

def print_summary_table() -> bool:
    """Print results table to stdout, GitHub Actions Step Summary, and send single Discord alert if needed."""
    checks = STATUS_REPORT["checks"]

    table_md = "| Status | Test Name | Details |\n| :---: | :--- | :--- |\n"
    for item in checks:
        table_md += f"| {item['icon']} | {item['name']} | {item['details']} |\n"

    print("\n" + "=" * 60)
    print(f"{'STATUS':<8} | {'TEST NAME':<25} | DETAILS")
    print("-" * 60)
    for item in checks:
        print(f"{item['icon']} {item['status']:<8} | {item['name']:<25} | {item['details']}")
    print("=" * 60 + "\n")

    step_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write("### EMODnet Checks Summary\n\n")
            f.write(table_md)

    has_failure = any(item["status"] == "FAIL" for item in checks)
    if has_failure:
        send_discord_alert()

    return not has_failure


if __name__ == "__main__":
    monitor_ok = is_monitor_available()
    # datasets_ok = is_platform_datasets_available()
    data_ok = is_individual_buoy_available()
    update_execution_history()
    # generate_status_dashboard()

    all_passed = print_summary_table()

    sys.exit(0)