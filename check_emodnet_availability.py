"""EMODnet service availability checks used by the TSL generator."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlencode

import requests

DEFAULT_MONITOR_URL = "https://monitor.emodnet.eu/resource/55/json"
DEFAULT_REQUEST_TIMEOUT = 60
DISCORD_REQUEST_TIMEOUT = 5
STALE_REPORT_THRESHOLD_MINUTES = 60
RELIABILITY_WARNING_THRESHOLD = 95.0

PLATFORM_DATASETS_URL = "https://platform-erddap.emodnet-physics.eu/api/parameters/{parameter}/datasets"
PLATFORM_API_URL = "https://platform-erddap.emodnet-physics.eu/api/parameters/{parameter}/data"
PROBE_PLATFORM_CODE = "cent2"
PROBE_WINDOW_DAYS_AGO = 3
PROBE_WINDOW_HOURS = 24

HISTORY_FILE = Path("history.json")

# --- Global state ---
STATUS_REPORT: Dict[str, List[Dict[str, Optional[str]]]] = {"checks": []}


def record_result(name: str, passed: bool, details: str, url: Optional[str] = None) -> None:
    """Record test result into global status report."""
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
    """Load history, append one-line compact record permanently, and persist."""
    history = []

    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        except (json.JSONDecodeError, OSError):
            history = []

    attempt_number = len(history) + 1
    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")

    alias_map = {
        "EMODnet Resource Monitor": "monitor",
        "Platform datasets API": "datasets",
        "Platform data API": "data",
    }

    compact_tests = {}
    for item in STATUS_REPORT["checks"]:
        key = alias_map.get(item["name"], item["name"])
        compact_tests[key] = True if item["status"] == "PASS" else item["details"]

    record = {
        "attempt": attempt_number,
        "ts": current_time,
        "ok": all(item["status"] == "PASS" for item in STATUS_REPORT["checks"]),
        "tests": compact_tests,
    }

    history.append(record)

    lines = [f"  {json.dumps(entry, separators=(', ', ': '))}" for entry in history]
    compact_json = "[\n" + ",\n".join(lines) + "\n]\n"

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        f.write(compact_json)

    print(f"History successfully appended: attempt #{attempt_number} (total records: {len(history)})")


def is_physics_erddap_available(
    monitor_url: Optional[str] = None,
    timeout: Optional[float] = None,
) -> bool:
    """Return whether both the EMODnet monitor and the real data API are healthy."""
    monitor_url = monitor_url or os.getenv("EMODNET_MONITOR_URL", DEFAULT_MONITOR_URL)
    timeout = timeout or float(os.getenv("EMODNET_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT))

    print("Checking EMODnet monitor status...")

    try:
        responseMonitor = requests.get(monitor_url, timeout=timeout)
    except requests.exceptions.Timeout:
        return _unavailable("EMODnet Resource Monitor", "Timeout", url=monitor_url)
    except requests.exceptions.RequestException:
        return _unavailable("EMODnet Resource Monitor", "Connection error", url=monitor_url)

    if responseMonitor.status_code != 200:
        return _unavailable("EMODnet Resource Monitor", f"HTTP {responseMonitor.status_code}", url=monitor_url)

    try:
        data = responseMonitor.json()
    except ValueError:
        return _unavailable("EMODnet Resource Monitor", "Invalid JSON", url=monitor_url)

    if not isinstance(data, dict):
        return _unavailable("EMODnet Resource Monitor", "Invalid payload", url=monitor_url)

    last_run = data.get("last_run")
    if last_run:
        try:
            last_run_dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
            age = datetime.now(timezone.utc) - last_run_dt
            if age > timedelta(minutes=STALE_REPORT_THRESHOLD_MINUTES):
                return _unavailable("EMODnet Resource Monitor", "Stale report", url=monitor_url)
        except ValueError:
            pass

    if data.get("status") is not True:
        return _unavailable("EMODnet Resource Monitor", "Degraded status", url=monitor_url)

    reliability = data.get("reliability")
    if isinstance(reliability, (int, float)) and reliability < RELIABILITY_WARNING_THRESHOLD:
        print(f"Warning: EMODnet monitor reliability is degraded ({reliability:.1f}%).")

    print("EMODnet monitor is healthy.")
    record_result("EMODnet Resource Monitor", True, "Healthy", url=monitor_url)
    return True


def is_platform_datasets_available(
    parameter: str = "SLEV",
    timeout: Optional[float] = None,
) -> bool:
    """Check if platform datasets endpoint returns valid dataset records."""
    timeout = timeout or float(os.getenv("EMODNET_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT))
    url = PLATFORM_DATASETS_URL.format(parameter=parameter)

    print("Checking EMODnet platform datasets API...")

    try:
        responseDatasets = requests.get(url, timeout=timeout)
        print(f"Querying EMODNET platform datasets API: {responseDatasets.url}")
        if responseDatasets.status_code != 200:
            return _unavailable("Platform datasets API", f"HTTP {responseDatasets.status_code}", url=url)
    except requests.exceptions.Timeout:
        return _unavailable("Platform datasets API", "Timeout", url=url)
    except requests.exceptions.RequestException:
        return _unavailable("Platform datasets API", "Connection error", url=url)

    try:
        payload = responseDatasets.json()
    except ValueError:
        return _unavailable("Platform datasets API", "Invalid JSON", url=url)

    datasets = payload.get("datasets") if isinstance(payload, dict) else None
    dataset_count = payload.get("datasetCount", 0) if isinstance(payload, dict) else 0

    if not isinstance(datasets, list) or len(datasets) == 0:
        return _unavailable("Platform datasets API", "0 datasets", url=url)

    print(f"✅ EMODnet platform datasets API is healthy ({dataset_count} datasets found).")
    record_result("Platform datasets API", True, f"Healthy ({dataset_count} datasets)", url=url)
    return True


def is_platform_api_available(
    platform_code: str = PROBE_PLATFORM_CODE,
    timeout: Optional[float] = None,
) -> bool:
    """Probe the actual data host with a small, recent SLEV request."""
    timeout = timeout or float(os.getenv("EMODNET_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT))
    end = datetime.now(timezone.utc) - timedelta(days=PROBE_WINDOW_DAYS_AGO)
    start = end - timedelta(hours=PROBE_WINDOW_HOURS)
    params = {
        "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_time": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platform_code": platform_code,
        "format": "csv",
    }

    base_url = PLATFORM_API_URL.format(parameter="SLEV")
    full_url = f"{base_url}?{urlencode(params)}"
    print(f"Checking EMODnet platform data API: {full_url}")

    try:
        responseSpecificBuoy = requests.get(base_url, params=params, timeout=timeout)
        target_url = responseSpecificBuoy.url or full_url

        if responseSpecificBuoy.status_code != 200:
            return _unavailable("Platform data API", f"HTTP {responseSpecificBuoy.status_code}", url=target_url)

    except requests.exceptions.Timeout:
        return _unavailable("Platform data API", "Timeout", url=full_url)
    except requests.exceptions.RequestException as error:
        status_code = getattr(getattr(error, "response", None), "status_code", None)
        detail = f"HTTP {status_code}" if status_code else "Connection error"
        return _unavailable("Platform data API", detail, url=full_url)

    lines = [line for line in responseSpecificBuoy.text.splitlines() if line.strip()]
    if len(lines) <= 1:
        return _unavailable("Platform data API", "No data rows", url=target_url)

    print("✅ EMODnet platform data API is healthy.")
    record_result("Platform data API", True, f"Healthy ({len(lines)-1} rows)", url=target_url)
    return True


def send_discord_alert() -> None:
    """Send Discord alert where test names are clickable markdown hyperlinks."""
    webhook_url = os.getenv("EMODNET_DISCORD_WEBHOOK_URL")
    if not webhook_url:
        return

    checks = STATUS_REPORT["checks"]
    has_failure = any(item["status"] == "FAIL" for item in checks)

    lines = []
    for item in checks:
        icon = "✅" if item["status"] == "PASS" else "❌"
        url = item.get("url")
        test_link = f"[{item['name']}]({url})" if url else item["name"]

        if item["status"] == "PASS":
            lines.append(f"{icon} **{test_link}** — {item['details']}")
        else:
            lines.append(f"{icon} **{test_link}** — `{item['details']}`")

    body = "\n".join(lines)
    date_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    title = f"🚨 **EMODnet Pipeline Alert** - {date_time}" if has_failure else f"✅ **EMODnet Pipeline OK** - {date_time}"

    payload = {"content": f"{title}\n\n{body}"}

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
        name_cell = f"[{item['name']}]({item['url']})" if item.get("url") else item["name"]
        table_md += f"| {item['icon']} | {name_cell} | {item['details']} |\n"

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
    monitor_ok = is_physics_erddap_available()
    datasets_ok = is_platform_datasets_available()
    data_ok = is_platform_api_available()

    all_passed = print_summary_table()
    update_execution_history()

    sys.exit(0)