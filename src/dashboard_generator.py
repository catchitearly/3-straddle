"""
Generates a single self-contained HTML dashboard with one tab per backtest day.

Lazy JSON-based rendering: each day's Plotly figure is only built (Plotly.newPlot)
the first time its tab is clicked, and only ever into a *visible* div. This is the
fix for the Plotly hidden-container sizing bug that showed up in the straddle
GEX dashboard - rendering into a display:none div gives you a 0-width chart.

No numpy/pandas - all data prep is plain python, embedded as JSON for the
browser-side Plotly.js (loaded from CDN) to render.
"""

import json

from src.ist_time import epoch_to_ist_time_str


def _day_payload(result):
    """Build the compact JSON payload for one day, consumed by client JS."""
    date_str = result["date"]

    if result.get("error") or not result.get("series"):
        return {
            "date": date_str,
            "error": result.get("error", "no_data"),
            "atm_strike": result.get("atm_strike"),
            "strikes": result.get("strikes"),
            "expiry": result.get("expiry"),
            "times": [],
            "price": [],
            "vwap": [],
            "entries": [],
            "day_pnl": 0.0,
        }

    series = result["series"]
    times = [epoch_to_ist_time_str(b["epoch"]) for b in series]
    price = [round(b["price"], 2) for b in series]
    vwap = [round(b["vwap"], 2) for b in series]

    entries = []
    for e in result.get("entries", []):
        entries.append({
            "basket_num": e["basket_num"],
            "entry_time": e["entry_time"],
            "entry_price": round(e["entry_price"], 2),
            "exit_time": e.get("exit_time"),
            "exit_price": round(e["exit_price"], 2) if e.get("exit_price") is not None else None,
            "exit_reason": e.get("exit_reason"),
            "pnl": round(e.get("pnl", 0.0), 2),
        })

    return {
        "date": date_str,
        "atm_strike": result.get("atm_strike"),
        "strikes": result.get("strikes"),
        "expiry": result.get("expiry"),
        "leg_symbols": result.get("leg_symbols"),
        "times": times,
        "price": price,
        "vwap": vwap,
        "entries": entries,
        "day_pnl": round(result.get("day_pnl", 0.0), 2),
        "num_baskets_deployed": result.get("num_baskets_deployed", 0),
    }


def generate_dashboard(day_results, output_path):
    payloads = [_day_payload(r) for r in day_results]

    total_pnl = sum(p["day_pnl"] for p in payloads)
    win_days = sum(1 for p in payloads if p["day_pnl"] > 0)
    loss_days = sum(1 for p in payloads if p["day_pnl"] < 0)
    flat_days = sum(1 for p in payloads if p["day_pnl"] == 0)
    total_baskets = sum(p.get("num_baskets_deployed", 0) for p in payloads)

    cum = 0.0
    equity_dates = []
    equity_curve = []
    for p in payloads:
        cum += p["day_pnl"]
        equity_dates.append(p["date"])
        equity_curve.append(round(cum, 2))

    summary = {
        "total_days": len(payloads),
        "win_days": win_days,
        "loss_days": loss_days,
        "flat_days": flat_days,
        "total_pnl": round(total_pnl, 2),
        "total_baskets": total_baskets,
        "equity_dates": equity_dates,
        "equity_curve": equity_curve,
    }

    html = _build_html(payloads, summary)
    with open(output_path, "w") as f:
        f.write(html)


def _build_html(payloads, summary):
    days_json = json.dumps(payloads)
    summary_json = json.dumps(summary)

    tab_buttons = "\n".join(
        f'<button class="tab-btn" data-idx="{i}" onclick="selectTab({i})">'
        f'{p["date"]}<span class="pnl-badge {"pos" if p["day_pnl"] >= 0 else "neg"}">'
        f'{p["day_pnl"]:+.0f}</span></button>'
        for i, p in enumerate(payloads)
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Nifty Straddle-VWAP Backtest</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/plotly.js/2.32.0/plotly.min.js"></script>
<style>
  :root {{
    --bg: #0f1115; --panel: #171a21; --border: #2a2f3a;
    --text: #e6e8ec; --muted: #8a90a0; --accent: #4f8cff;
    --green: #2ecc71; --red: #ff5c5c;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    background: var(--bg); color: var(--text);
    font-family: -apple-system, Segoe UI, Roboto, sans-serif;
    margin: 0; padding: 24px;
  }}
  h1 {{ font-size: 20px; margin: 0 0 4px 0; }}
  .subtitle {{ color: var(--muted); font-size: 13px; margin-bottom: 20px; }}

  .summary {{
    display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 20px;
  }}
  .stat {{
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 8px; padding: 12px 18px; min-width: 120px;
  }}
  .stat .label {{ color: var(--muted); font-size: 12px; margin-bottom: 4px; }}
  .stat .value {{ font-size: 18px; font-weight: 600; }}
  .stat .value.pos {{ color: var(--green); }}
  .stat .value.neg {{ color: var(--red); }}

  #equity-curve {{
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 8px; margin-bottom: 20px; padding: 8px;
  }}

  .tab-bar {{
    display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 12px;
    border-bottom: 1px solid var(--border); padding-bottom: 12px;
  }}
  .tab-btn {{
    background: var(--panel); color: var(--text); border: 1px solid var(--border);
    border-radius: 6px; padding: 6px 10px; font-size: 12px; cursor: pointer;
    display: flex; align-items: center; gap: 6px;
  }}
  .tab-btn:hover {{ border-color: var(--accent); }}
  .tab-btn.active {{ border-color: var(--accent); background: #1c2333; }}
  .pnl-badge {{ font-size: 11px; font-weight: 600; padding: 1px 6px; border-radius: 10px; }}
  .pnl-badge.pos {{ background: rgba(46,204,113,0.15); color: var(--green); }}
  .pnl-badge.neg {{ background: rgba(255,92,92,0.15); color: var(--red); }}

  .day-panel {{ display: none; }}
  .day-panel.active {{ display: block; }}

  .day-meta {{
    display: flex; gap: 20px; flex-wrap: wrap; margin-bottom: 10px;
    font-size: 13px; color: var(--muted);
  }}
  .day-meta b {{ color: var(--text); }}

  .chart-box {{
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 8px; padding: 8px; margin-bottom: 14px;
  }}

  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border); }}
  th {{ color: var(--muted); font-weight: 500; }}
  td.pnl-pos {{ color: var(--green); }}
  td.pnl-neg {{ color: var(--red); }}
  .no-data {{ color: var(--muted); padding: 20px; text-align: center; }}
</style>
</head>
<body>

<h1>Nifty 3-Straddle Basket &middot; VWAP Mean-Reversion Backtest</h1>
<div class="subtitle">ATM-100 / ATM / ATM+100 straddles fixed at 09:45 &middot; sell basket on downward VWAP cross (repeats on every fresh cross) &middot; square-off 15:15</div>

<div class="summary">
  <div class="stat"><div class="label">Total PnL</div>
    <div class="value {'pos' if summary['total_pnl'] >= 0 else 'neg'}">&#8377;{summary['total_pnl']:+.0f}</div></div>
  <div class="stat"><div class="label">Days</div><div class="value">{summary['total_days']}</div></div>
  <div class="stat"><div class="label">Win Days</div><div class="value pos">{summary['win_days']}</div></div>
  <div class="stat"><div class="label">Loss Days</div><div class="value neg">{summary['loss_days']}</div></div>
  <div class="stat"><div class="label">Baskets Sold</div><div class="value">{summary['total_baskets']}</div></div>
</div>

<div id="equity-curve"></div>

<div class="tab-bar" id="tab-bar">
  {tab_buttons}
</div>

<div id="panels"></div>

<script>
const DAYS = {days_json};
const SUMMARY = {summary_json};
const renderedTabs = new Set();

function drawEquityCurve() {{
  const trace = {{
    x: SUMMARY.equity_dates, y: SUMMARY.equity_curve,
    type: 'scatter', mode: 'lines+markers', name: 'Cumulative PnL',
    line: {{color: '#4f8cff', width: 2}}, marker: {{size: 5}}
  }};
  const layout = {{
    title: {{text: 'Cumulative PnL', font: {{color: '#e6e8ec', size: 13}}}},
    paper_bgcolor: '#171a21', plot_bgcolor: '#171a21',
    font: {{color: '#8a90a0', size: 11}},
    margin: {{l: 50, r: 20, t: 30, b: 30}}, height: 220,
    xaxis: {{gridcolor: '#2a2f3a'}}, yaxis: {{gridcolor: '#2a2f3a'}}
  }};
  Plotly.newPlot('equity-curve', [trace], layout, {{displayModeBar: false, responsive: true}});
}}

function buildDayPanel(idx) {{
  const d = DAYS[idx];
  const panel = document.createElement('div');
  panel.className = 'day-panel';
  panel.id = 'panel-' + idx;

  if (d.error || d.times.length === 0) {{
    panel.innerHTML = `<div class="no-data">No usable data for ${{d.date}}` +
      (d.strikes ? ` (strikes ${{d.strikes.join(', ')}})` : '') +
      `: ${{d.error || 'no data'}}</div>`;
    return panel;
  }}

  const entryRows = d.entries.map(e => `
    <tr>
      <td>#${{e.basket_num}}</td>
      <td>${{e.entry_time}}</td>
      <td>${{e.entry_price}}</td>
      <td>${{e.exit_time || '-'}}</td>
      <td>${{e.exit_price != null ? e.exit_price : '-'}}</td>
      <td>${{e.exit_reason || '-'}}</td>
      <td class="${{e.pnl >= 0 ? 'pnl-pos' : 'pnl-neg'}}">${{e.pnl >= 0 ? '+' : ''}}${{e.pnl}}</td>
    </tr>`).join('');

  panel.innerHTML = `
    <div class="day-meta">
      <div>Strikes: <b>${{d.strikes.join(' / ')}}</b> (ATM ${{d.atm_strike}})</div>
      <div>Expiry: <b>${{d.expiry || '-'}}</b></div>
      <div>Baskets sold: <b>${{d.num_baskets_deployed || 0}}</b></div>
      <div>Day PnL: <b class="${{d.day_pnl >= 0 ? 'pnl-pos' : 'pnl-neg'}}">${{d.day_pnl >= 0 ? '+' : ''}}${{d.day_pnl}}</b></div>
    </div>
    <div class="chart-box"><div id="price-chart-${{idx}}" style="height:340px;"></div></div>
    <div class="chart-box"><div id="pnl-chart-${{idx}}" style="height:220px;"></div></div>
    <div class="chart-box">
      <table>
        <thead><tr><th>#</th><th>Entry Time</th><th>Entry Price</th><th>Exit Time</th><th>Exit Price</th><th>Exit Reason</th><th>PnL</th></tr></thead>
        <tbody>${{entryRows || '<tr><td colspan="7" class="no-data">No entries fired this day</td></tr>'}}</tbody>
      </table>
    </div>`;
  return panel;
}}

function renderCharts(idx) {{
  const d = DAYS[idx];
  if (d.error || d.times.length === 0) return;

  const priceTrace = {{
    x: d.times, y: d.price, type: 'scatter', mode: 'lines',
    name: 'Combined Straddle', line: {{color: '#4f8cff', width: 1.6}}
  }};
  const vwapTrace = {{
    x: d.times, y: d.vwap, type: 'scatter', mode: 'lines',
    name: 'VWAP', line: {{color: '#ffb84f', width: 1.6, dash: 'dot'}}
  }};
  const entryTrace = {{
    x: d.entries.map(e => e.entry_time), y: d.entries.map(e => e.entry_price),
    type: 'scatter', mode: 'markers+text', name: 'Sell Basket',
    text: d.entries.map(e => '#' + e.basket_num),
    textposition: 'top center', textfont: {{color: '#ff5c5c', size: 10}},
    marker: {{color: '#ff5c5c', size: 9, symbol: 'triangle-down'}}
  }};

  const priceLayout = {{
    title: {{text: 'Combined 3-Straddle Basket Price vs VWAP', font: {{color: '#e6e8ec', size: 13}}}},
    paper_bgcolor: '#171a21', plot_bgcolor: '#171a21',
    font: {{color: '#8a90a0', size: 11}},
    margin: {{l: 50, r: 20, t: 30, b: 30}},
    xaxis: {{gridcolor: '#2a2f3a', nticks: 12}}, yaxis: {{gridcolor: '#2a2f3a'}},
    legend: {{orientation: 'h', y: -0.2}}
  }};
  Plotly.newPlot('price-chart-' + idx, [priceTrace, vwapTrace, entryTrace], priceLayout,
    {{displayModeBar: false, responsive: true}});

  const pnlTrace = {{
    x: d.entries.map(e => '#' + e.basket_num + ' (' + e.entry_time + ')'),
    y: d.entries.map(e => e.pnl), type: 'bar', name: 'PnL per basket',
    marker: {{color: d.entries.map(e => e.pnl >= 0 ? '#2ecc71' : '#ff5c5c')}}
  }};
  const pnlLayout = {{
    title: {{text: 'PnL per Basket', font: {{color: '#e6e8ec', size: 13}}}},
    paper_bgcolor: '#171a21', plot_bgcolor: '#171a21',
    font: {{color: '#8a90a0', size: 11}},
    margin: {{l: 50, r: 20, t: 30, b: 30}},
    xaxis: {{gridcolor: '#2a2f3a'}}, yaxis: {{gridcolor: '#2a2f3a'}}
  }};
  Plotly.newPlot('pnl-chart-' + idx, [pnlTrace], pnlLayout, {{displayModeBar: false, responsive: true}});
}}

function selectTab(idx) {{
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.day-panel').forEach(p => p.classList.remove('active'));
  document.querySelector(`.tab-btn[data-idx="${{idx}}"]`).classList.add('active');
  document.getElementById('panel-' + idx).classList.add('active');

  // Lazy render: only build the Plotly figure the first time this tab is
  // shown, and only now that the container is actually visible (avoids the
  // 0-width hidden-div rendering bug).
  if (!renderedTabs.has(idx)) {{
    renderCharts(idx);
    renderedTabs.add(idx);
  }} else {{
    // container was resized while hidden (e.g. window resize) - relayout
    Plotly.Plots.resize('price-chart-' + idx);
    Plotly.Plots.resize('pnl-chart-' + idx);
  }}
}}

const panelsContainer = document.getElementById('panels');
DAYS.forEach((d, idx) => panelsContainer.appendChild(buildDayPanel(idx)));

drawEquityCurve();
if (DAYS.length > 0) selectTab(0);
</script>
</body>
</html>
"""
