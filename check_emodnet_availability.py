"""EMODnet service availability checks used by the TSL generator."""

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests


DEFAULT_MONITOR_URL = "https://monitor.emodnet.eu/resource/55/json"
DEFAULT_REQUEST_TIMEOUT = 60
DISCORD_REQUEST_TIMEOUT = 5
STALE_REPORT_THRESHOLD_MINUTES = 60  # monitor report older than this can't be trusted
RELIABILITY_WARNING_THRESHOLD = 95.0  # percent, logged only, not a hard failure

# Real data host used by the generator, distinct from the host the monitor above tracks.
PLATFORM_DATASETS_URL = "https://platform-erddap.emodnet-physics.eu/api/parameters/{parameter}/datasets"
PLATFORM_API_URL = "https://platform-erddap.emodnet-physics.eu/api/parameters/{parameter}/data"
PROBE_PLATFORM_CODE = "cent2"  # known-good reference station
PROBE_WINDOW_DAYS_AGO = 3  # buoy reports lag behind "now" by a few days
PROBE_WINDOW_HOURS = 24

# --- Global state ---
STATUS_REPORT: Dict[str, List[Dict[str, str]]] = {"checks": []}


def record_result(name: str, passed: bool, details: str) -> None:
    """Registra o resultado do teste no estado global."""
    STATUS_REPORT["checks"].append(
        {
            "name": name,
            "status": "PASS" if passed else "FAIL",
            "details": details,
            "icon": "✅" if passed else "❌",
        }
    )


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
    except requests.exceptions.RequestException as error:
        return _unavailable("EMODnet Resource Monitor", f"Could not connect to the EMODnet monitor: {error}")

    if responseMonitor.status_code != 200:
        return _unavailable("EMODnet Resource Monitor", f"EMODnet monitor returned HTTP {responseMonitor.status_code}.")

    try:
        data = responseMonitor.json()
    except ValueError as error:
        return _unavailable("EMODnet Resource Monitor", f"EMODnet monitor returned invalid JSON: {error}")

    if not isinstance(data, dict):
        return _unavailable("EMODnet Resource Monitor", "EMODnet monitor returned an unexpected JSON payload.")

    # A stale report means the monitor stopped probing; its "status" can't be trusted.
    last_run = data.get("last_run")
    if last_run:
        try:
            last_run_dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
            age = datetime.now(timezone.utc) - last_run_dt
            if age > timedelta(minutes=STALE_REPORT_THRESHOLD_MINUTES):
                return _unavailable("EMODnet Resource Monitor", f"EMODnet monitor report is stale ({age} old).")
        except ValueError:
            pass

    if data.get("status") is not True:
        last_report = data.get("last_report") or {}
        message = last_report.get("message", "The monitor reported an unknown error.")
        return _unavailable("EMODnet Resource Monitor", f"EMODnet monitor reports an issue: {message}")

    reliability = data.get("reliability")
    if isinstance(reliability, (int, float)) and reliability < RELIABILITY_WARNING_THRESHOLD:
        print(f"Warning: EMODnet monitor reliability is degraded ({reliability:.1f}%).")

    print("EMODnet monitor is healthy.")
    record_result("EMODnet Resource Monitor", True, "Healthy")
    return True


def is_platform_datasets_available(
    parameter: str = "SLEV",
    timeout: Optional[float] = None,
) -> bool:
    """Check if platform datasets endpoint returns information."""
    timeout = timeout or float(os.getenv("EMODNET_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT))
    url = PLATFORM_DATASETS_URL.format(parameter=parameter)

    print("Checking EMODnet platform datasets API...")

    try:
        responseDatasets = requests.get(url, timeout=timeout)
        print(f"Querying EMODNET platform datasets API: {responseDatasets.url}")
        responseDatasets.raise_for_status()
    except requests.exceptions.RequestException as error:
        return _unavailable("Platform datasets API", f"Could not reach datasets API: {error}")

    if not responseDatasets.text.strip():
        return _unavailable("Platform datasets API", "Datasets API returned empty response.")

    # Header-only responseSpecificBuoy (no data rows) means the API is up but not serving data.
    lines = [line for line in responseDatasets.text.splitlines() if line.strip()]
    if len(lines) <= 1:
        return _unavailable("Platform datasets API", "EMODnet platform datasets API returned no data rows for the probe window.")

    print("✅ EMODnet platform datasets API is healthy.")
    record_result("Platform datasets API", True, "Information received")
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

    print("Checking EMODnet platform data API...")

    try:
        responseSpecificBuoy = requests.get(
            PLATFORM_API_URL.format(parameter="SLEV"), params=params, timeout=timeout
        )
        print(f"Querying EMODNET platform API: {responseSpecificBuoy.url}")
        responseSpecificBuoy.raise_for_status()
    except requests.exceptions.RequestException as error:
        return _unavailable("Platform data API", f"Could not reach the EMODnet platform data API: {error}")

    # Header-only responseSpecificBuoy (no data rows) means the API is up but not serving data.
    lines = [line for line in responseSpecificBuoy.text.splitlines() if line.strip()]
    if len(lines) <= 1:
        return _unavailable("Platform data API", "EMODnet platform data API returned no data rows for the probe window.")

    print("✅ EMODnet platform data API is healthy.")
    record_result("Platform data API", True, f"Healthy ({len(lines)-1} rows)")
    return True


def send_discord_alert() -> None:
    """Envia uma mensagem de texto simples sem card, usando a largura total do Discord."""
    webhook_url = os.getenv("EMODNET_DISCORD_WEBHOOK_URL")
    if not webhook_url:
        return

    checks = STATUS_REPORT["checks"]
    has_failure = any(item["status"] == "FAIL" for item in checks)

    # Montagem da tabela alinhada com colunas largas
    header = f"{'STATUS':<6} | {'TESTE':<30} | DETALHE"
    divisor = f"{'-'*6}-+-{'-'*23}-+-{'-'*50}"

    linhas = [header, divisor]
    for item in checks:
        icon = ":white_check_mark:" if item["status"] == "PASS" else ":x:"
        linhas.append(f"{icon:<6} | {item['name']:<30} | {item['details']}")

    tabela = "\n".join(linhas)
    titulo = "**EMODnet Pipeline Alert**" if has_failure else "✅ **EMODnet Pipeline OK**"

    # Enviando direto no 'content', sem embeds
    payload = {
        "content": f"{titulo}\n```text\n{tabela}\n```"
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





def _unavailable(name: str, message: str) -> bool:
    print(f"EMODnet is unavailable: {message}")
    record_result(name, False, message)
    return False


def print_summary_table() -> bool:
    """Print results table to stdout, GitHub Actions Step Summary, and send single Discord alert if needed."""
    checks = STATUS_REPORT["checks"]

    table_md = "| Status | Test Name | Details |\n| :---: | :--- | :--- |\n"
    for item in checks:
        table_md += f"| {item['icon']} | {item['name']} | {item['details']} |\n"

    # Terminal output
    print("\n" + "=" * 60)
    print(f"{'STATUS':<8} | {'TEST NAME':<25} | DETAILS")
    print("-" * 60)
    for item in checks:
        print(f"{item['icon']} {item['status']:<8} | {item['name']:<25} | {item['details']}")
    print("=" * 60 + "\n")

    # GitHub Actions summary tab (aqui o Markdown funciona perfeitamente)
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

# sys.exit(0 if all_passed else 1) for when the code will run inside of the main workflow
sys.exit(0)