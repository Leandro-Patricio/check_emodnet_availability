"""Generates an interactive HTML dashboard from history.json for EMODnet availability."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

DEFAULT_HISTORY_FILE = Path("history.json")
DEFAULT_OUTPUT_FILE = Path("dashboard.html")
DEFAULT_LOOKBACK_DAYS = 7


def generate_status_dashboard(
    history_file: Path = DEFAULT_HISTORY_FILE,
    output_file: Path = DEFAULT_OUTPUT_FILE,
    days: int = DEFAULT_LOOKBACK_DAYS,
) -> bool:
    """Read execution history, filter the last N days, and build an interactive HTML dashboard."""
    if not history_file.exists():
        print(f"Error: History file '{history_file}' not found.")
        return False

    try:
        with open(history_file, "r", encoding="utf-8") as f:
            raw = json.load(f)
            history = raw.get("history", []) if isinstance(raw, dict) else raw
    except (json.JSONDecodeError, OSError) as error:
        print(f"Error reading history file: {error}")
        return False

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    filtered = []

    for item in history:
        try:
            ts_str = item.get("ts", "").replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts_str)
            if dt >= cutoff:
                filtered.append({**item, "dt": dt.isoformat()})
        except ValueError:
            continue

    if not filtered:
        print(f"No records found within the last {days} days.")
        return False

    labels = [x["dt"] for x in filtered]
    overall_status = [1 if x.get("ok") else 0 for x in filtered]

    # Map individual check results for tooltips
    monitor_status = [
        "PASS" if x.get("tests", {}).get("monitor") is True else str(x.get("tests", {}).get("monitor", "FAIL"))
        for x in filtered
    ]
    data_status = [
        "PASS" if x.get("tests", {}).get("data") is True else str(x.get("tests", {}).get("data", "FAIL"))
        for x in filtered
    ]

    total_runs = len(filtered)
    successful_runs = sum(overall_status)
    uptime_pct = (successful_runs / total_runs * 100) if total_runs else 0.0

    html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>EMODnet Service Status Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background-color: #0f172a;
      color: #f8fafc;
      margin: 0;
      padding: 30px 20px;
      display: flex;
      flex-direction: column;
      align-items: center;
    }}
    .container {{
      max-width: 950px;
      width: 100%;
      background: #1e293b;
      padding: 24px;
      border-radius: 12px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.3);
    }}
    .header {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      border-bottom: 1px solid #334155;
      padding-bottom: 16px;
      margin-bottom: 24px;
    }}
    .metric {{
      font-size: 1.1rem;
      color: #94a3b8;
    }}
    .uptime-val {{
      color: #38bdf8;
      font-weight: bold;
      font-size: 1.4rem;
    }}
    .chart-container {{
      position: relative;
      height: 320px;
      width: 100%;
    }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div>
        <h2 style="margin: 0 0 6px 0;">EMODnet Service Availability</h2>
        <span style="color: #64748b; font-size: 0.9rem;">Window: Last {days} days</span>
      </div>
      <div class="metric">
        Uptime: <span class="uptime-val">{uptime_pct:.1f}%</span> ({successful_runs}/{total_runs} runs)
      </div>
    </div>
    <div class="chart-container">
      <canvas id="statusChart"></canvas>
    </div>
  </div>

  <script>
    const labels = {json.dumps(labels)};
    const overall = {json.dumps(overall_status)};
    const monitor = {json.dumps(monitor_status)};
    const data = {json.dumps(data_status)};

    const ctx = document.getElementById('statusChart').getContext('2d');
    new Chart(ctx, {{
      type: 'line',
      data: {{
        labels: labels.map(t => new Date(t).toLocaleString('en-US', {{ timeZone: 'UTC' }}) + ' UTC'),
        datasets: [{{
          label: 'System Status',
          data: overall,
          stepped: true,
          borderColor: '#38bdf8',
          borderWidth: 2,
          pointRadius: 4,
          pointHoverRadius: 6,
          pointBackgroundColor: overall.map(v => v === 1 ? '#22c55e' : '#ef4444'),
          fill: true,
          backgroundColor: 'rgba(56, 189, 248, 0.08)'
        }}]
      }},
      options: {{
        responsive: true,
        maintainAspectRatio: false,
        scales: {{
          y: {{
            min: -0.1,
            max: 1.1,
            ticks: {{
              stepSize: 1,
              callback: (v) => v === 1 ? 'UP' : (v === 0 ? 'DOWN' : ''),
              color: '#94a3b8'
            }},
            grid: {{ color: '#334155' }}
          }},
          x: {{
            ticks: {{ color: '#94a3b8', maxTicksLimit: 10 }},
            grid: {{ display: false }}
          }}
        }},
        plugins: {{
          legend: {{ display: false }},
          tooltip: {{
            callbacks: {{
              label: (ctx) => ctx.raw === 1 ? 'Overall Status: UP' : 'Overall Status: DOWN',
              afterBody: (ctx) => {{
                const idx = ctx[0].dataIndex;
                return [
                  `Monitor: ${{monitor[idx]}}`,
                  `API Data (cent2): ${{data[idx]}}`
                ];
              }}
            }}
          }}
        }}
      }}
    }});
  </script>
</body>
</html>
"""

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(html_template)

    print(f"Interactive status dashboard generated at: {output_file.resolve()}")
    return True


if __name__ == "__main__":
    generate_status_dashboard()