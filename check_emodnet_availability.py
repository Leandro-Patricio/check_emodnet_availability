"""EMODnet service availability checks used by the TSL generator."""

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests


DEFAULT_MONITOR_URL = "https://monitor.emodnet.eu/resource/55/json"
DEFAULT_REQUEST_TIMEOUT = 10
DISCORD_REQUEST_TIMEOUT = 5
STALE_REPORT_THRESHOLD_MINUTES = 60  # monitor report older than this can't be trusted
RELIABILITY_WARNING_THRESHOLD = 95.0  # percent, logged only, not a hard failure

# Real data host used by the generator, distinct from the host the monitor above tracks.
PLATFORM_API_URL = "https://platform-erddap.emodnet-physics.eu/api/parameters/{parameter}/data"
PROBE_PLATFORM_CODE = "cent2"  # known-good reference station
PROBE_WINDOW_DAYS_AGO = 3  # buoy reports lag behind "now" by a few days
PROBE_WINDOW_HOURS = 24


def is_physics_erddap_available(
    monitor_url: Optional[str] = None,
    timeout: Optional[float] = None,
) -> bool:
    """Return whether both the EMODnet monitor and the real data API are healthy."""
    monitor_url = monitor_url or os.getenv("EMODNET_MONITOR_URL", DEFAULT_MONITOR_URL)
    timeout = timeout or float(os.getenv("EMODNET_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT))

    print("Checking EMODnet monitor status...")

    try:
        response = requests.get(monitor_url, timeout=timeout)
    except requests.exceptions.RequestException as error:
        return _unavailable(f"Could not connect to the EMODnet monitor: {error}")

    if response.status_code != 200:
        return _unavailable(
            f"EMODnet monitor returned HTTP {response.status_code}."
        )

    try:
        data = response.json()
    except ValueError as error:
        return _unavailable(f"EMODnet monitor returned invalid JSON: {error}")

    if not isinstance(data, dict):
        return _unavailable("EMODnet monitor returned an unexpected JSON payload.")

    # A stale report means the monitor stopped probing; its "status" can't be trusted.
    last_run = data.get("last_run")
    if last_run:
        try:
            last_run_dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
            age = datetime.now(timezone.utc) - last_run_dt
            if age > timedelta(minutes=STALE_REPORT_THRESHOLD_MINUTES):
                return _unavailable(f"EMODnet monitor report is stale ({age} old).")
        except ValueError:
            pass

    if data.get("status") is not True:
        last_report = data.get("last_report") or {}
        message = last_report.get("message", "The monitor reported an unknown error.")
        return _unavailable(f"EMODnet monitor reports an issue: {message}")

    reliability = data.get("reliability")
    if isinstance(reliability, (int, float)) and reliability < RELIABILITY_WARNING_THRESHOLD:
        print(f"Warning: EMODnet monitor reliability is degraded ({reliability:.1f}%).")

    print("EMODnet monitor is healthy.")
    return is_platform_api_available(timeout=timeout)


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
        response = requests.get(
            PLATFORM_API_URL.format(parameter="SLEV"), params=params, timeout=timeout
        )
        print(f"Querying EMODNET platform API: {response.url}")
        response.raise_for_status()
    except requests.exceptions.RequestException as error:
        return _unavailable(f"Could not reach the EMODnet platform data API: {error}")

    # Header-only response (no data rows) means the API is up but not serving data.
    lines = [line for line in response.text.splitlines() if line.strip()]
    if len(lines) <= 1:
        return _unavailable("EMODnet platform data API returned no data rows for the probe window.")

    print("EMODnet platform data API is healthy.")
    return True


def send_discord_alert(message: str) -> None:
    """Send a failure notification when a Discord webhook is configured."""
    webhook_url = os.getenv("EMODNET_DISCORD_WEBHOOK_URL")
    if not webhook_url:
        return

    payload = {"content": f"**EMODnet pipeline alert**\n{message}"}

    try:
        response = requests.post(
            webhook_url,
            json=payload,
            timeout=DISCORD_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as error:
        print(f"Could not send Discord notification: {error}")


def _unavailable(message: str) -> bool:
    print(f"EMODnet is unavailable: {message}")
    send_discord_alert(message)
    return False


if __name__ == "__main__":
    sys.exit(0 if is_physics_erddap_available() else 1)