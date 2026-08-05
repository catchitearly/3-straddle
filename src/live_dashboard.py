"""
Renders a single-page "live status" HTML view from live_notifier's
data/live_state.json - shows the ATM strike, and one section PER BASKET SET
(config.BASKET_SETS - "A", "B", "C" by default), each with its own current
price vs VWAP chart, entry/exit markers, and entries table. Same dark visual
style as dashboard_generator.py's backtest tabs, and links to/from it.

No ATR chart - ATR is still computed and used internally for each set's
trailing stop, just not plotted here.

Meant to be regenerated and redeployed on every live_notifier run, so it's
effectively a near-real-time view as long as the runner keeps firing. Also
pushed to Telegram as a document periodically (config.DASHBOARD_SEND_INTERVAL_SECONDS)
- see live_notifier_local.py / src/telegram_notifier.send_telegram_document.
"""

import json


STYLE = """
:root {
  --bg: #0f1115; --panel: #171a21; --border: #2a2f3a;
  --text: #e6e8ec; --muted: #8a90a0; --accent: #4f8cff;
  --green: #2ecc71; --red: #ff5c5c;
}
* { box-sizing: border-box; }
body {
  background: var(--bg); color: var(--text);
  font-family: -apple-system, Segoe UI, Roboto, sans-serif;
  margin: 0; padding: 24px;
}
h1 { font-size: 20px; margin: 0 0 4px 0; }
h2 { font-size: 16px; margin: 0 0 10px 0; }
.subtitle { color: var(--muted); font-size: 13px; margin-bottom: 20px; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.nav { margin-bottom: 16px; font-size: 13px; }
.summary { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 20px; }
.stat {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 8px; padding: 12px 18px; min-width: 120px;
}
.stat .label { color: var(--muted); font-size: 12px; margin-bottom: 4px; }
.stat .value { font-size: 18px; font-weight: 600; }
.stat .value.pos { color: var(--green); }
.stat .value.neg { color: var(--red); }
.chart-box {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 8px; padding: 14px 18px; margin-bottom: 14px;
}
.set-section {
  border: 1px solid var(--border); border-radius: 10px;
  padding: 16px; margin-bottom: 24px; background: #12141a;
}
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border); }
th { color: var(--muted); font-weight: 500; }
td.pnl-pos { color: var(--green); }
td.pnl-neg { color: var(--red); }
td.open-badge { color: var(--accent); font-weight: 600; }
.no-data { color: var(--muted); padding: 20px; text-align: center; }
.status-pill {
  display: inline-block; padding: 3px 10px; border-radius: 12px;
  font-size: 12px; font-weight: 600; margin-left: 8px;
}
.status-pill.live { background: rgba(79,140,255,0.15); color: var(--accent); }
.status-pill.done { background: rgba(46,204,113,0.15); color: var(--green); }
"""


def _fmt_pnl(v):
    if v is None:
        return "-"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}"


def _render_set_section(label, bs, date_str):
    strikes = bs.get("strikes")
    entries = bs.get("entries", [])
    squared_off = bs.get("squared_off")

    if not strikes:
        return f"""<div class="set-section">
          <h2>Set {label}</h2>
          <div class="no-data">Strikes not fixed yet.</div>
        </div>"""

    realized_pnl = sum(e["pnl"] for e in entries if e.get("pnl") is not None)
    open_entries = [e for e in entries if e.get("exit_price") is None]
    closed_entries = [e for e in entries if e.get("exit_price") is not None]
    status_label = "SQUARED OFF" if squared_off else "LIVE"
    status_class = "done" if squared_off else "live"

    rows = "\n".join(
        f"""<tr>
            <td>#{e['basket_num']}</td>
            <td>{e['entry_time']}</td>
            <td>{e['entry_price']:.2f}</td>
            <td>{e.get('exit_time') or '-'}</td>
            <td>{f"{e['exit_price']:.2f}" if e.get('exit_price') is not None else '-'}</td>
            <td>{e.get('exit_reason') or ('<span class="open-badge">OPEN</span>' if e.get('exit_price') is None else '-')}</td>
            <td class="{'pnl-pos' if (e.get('pnl') or 0) >= 0 else 'pnl-neg'}">{_fmt_pnl(e.get('pnl'))}</td>
        </tr>"""
        for e in entries
    )
    if not rows:
        rows = '<tr><td colspan="7" class="no-data">No entries yet today</td></tr>'

    last_price = bs.get("last_price")
    last_vwap = bs.get("last_vwap")
    price_line = (
        f"Combined price: <b>{last_price:.2f}</b> vs VWAP <b>{last_vwap:.2f}</b>"
        if last_price is not None and last_vwap is not None
        else "Waiting for first price update..."
    )

    chart_id = f"chart-{label}"
    series_json = json.dumps(bs.get("series", []))
    entries_json = json.dumps(entries)

    return f"""
    <div class="set-section">
      <h2>Set {label} — {strikes[0]} / {strikes[1]} / {strikes[2]}
        <span class="status-pill {status_class}">{status_label}</span>
      </h2>
      <div class="summary">
        <div class="stat"><div class="label">Realized PnL</div>
          <div class="value {'pos' if realized_pnl >= 0 else 'neg'}">&#8377;{realized_pnl:+.0f}</div></div>
        <div class="stat"><div class="label">Baskets Deployed</div><div class="value">{bs.get('baskets_deployed', 0)}</div></div>
        <div class="stat"><div class="label">Open</div><div class="value">{len(open_entries)}</div></div>
        <div class="stat"><div class="label">Closed</div><div class="value">{len(closed_entries)}</div></div>
      </div>
      <div class="chart-box">
        <div style="margin-bottom:8px; color: var(--muted); font-size: 13px;">
          Last update: <b style="color:var(--text)">{bs.get('last_updated') or '-'}</b> IST
        </div>
        <div style="font-size:14px;">{price_line}</div>
      </div>
      <div class="chart-box"><div id="{chart_id}" style="height:320px;"></div></div>
      <div class="chart-box">
        <table>
          <thead><tr><th>#</th><th>Entry Time</th><th>Entry Price</th><th>Exit Time</th><th>Exit Price</th><th>Exit Reason</th><th>PnL</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </div>
    <script>
    (function() {{
      const series = {series_json};
      const entries = {entries_json};
      if (series.length === 0) return;

      const times = series.map(b => b.time);
      const priceVals = series.map(b => b.price);
      const vwapVals = series.map(b => b.vwap);

      const priceTrace = {{
        x: times, y: priceVals, type: 'scatter', mode: 'lines',
        name: 'Combined Price', line: {{color: '#4f8cff', width: 1.6}}
      }};
      const vwapTrace = {{
        x: times, y: vwapVals, type: 'scatter', mode: 'lines',
        name: 'VWAP', line: {{color: '#ffb84f', width: 1.6, dash: 'dot'}}
      }};
      const entryTrace = {{
        x: entries.map(e => e.entry_time), y: entries.map(e => e.entry_price),
        type: 'scatter', mode: 'markers+text', name: 'Sell',
        text: entries.map(e => '#' + e.basket_num),
        textposition: 'top center', textfont: {{color: '#ff5c5c', size: 10}},
        marker: {{color: '#ff5c5c', size: 9, symbol: 'triangle-down'}}
      }};
      const exitTrace = {{
        x: entries.filter(e => e.exit_price != null).map(e => e.exit_time),
        y: entries.filter(e => e.exit_price != null).map(e => e.exit_price),
        type: 'scatter', mode: 'markers', name: 'Exit',
        marker: {{color: '#2ecc71', size: 8, symbol: 'triangle-up'}}
      }};

      const layout = {{
        title: {{text: 'Set {label} — Combined Price vs VWAP', font: {{color: '#e6e8ec', size: 13}}}},
        paper_bgcolor: '#171a21', plot_bgcolor: '#171a21',
        font: {{color: '#8a90a0', size: 11}},
        margin: {{l: 50, r: 20, t: 30, b: 30}},
        xaxis: {{gridcolor: '#2a2f3a', nticks: 12}}, yaxis: {{gridcolor: '#2a2f3a'}},
        legend: {{orientation: 'h', y: -0.25}}
      }};
      Plotly.newPlot('{chart_id}', [priceTrace, vwapTrace, entryTrace, exitTrace], layout,
        {{displayModeBar: false, responsive: true}});
    }})();
    </script>"""


def generate_live_page(state, output_path, backtest_index_exists=True):
    if state is None:
        html = _shell("No live data yet — the notifier hasn't run today.",
                       backtest_index_exists)
        with open(output_path, "w") as f:
            f.write(html)
        return

    date_str = state.get("date", "-")
    strike_fixed = state.get("strike_fixed")
    basket_sets = state.get("basket_sets", {})
    all_squared_off = bool(basket_sets) and all(bs.get("squared_off") for bs in basket_sets.values())

    overall_status = "SQUARED OFF" if all_squared_off else ("LIVE" if strike_fixed else "AWAITING STRIKES")
    overall_class = "done" if all_squared_off else "live"

    header = f"""
    <h1>Live Straddle Basket Status
      <span class="status-pill {overall_class}">{overall_status}</span>
    </h1>
    <div class="subtitle">{date_str} &middot; ATM {state.get('atm_strike') or '-'} &middot; expiry {state.get('expiry') or '-'} &middot; auto-refreshes every 60 seconds</div>
    """

    if not strike_fixed:
        body = '<div class="chart-box no-data">Strikes not fixed yet - waiting for 09:45 IST.</div>'
    else:
        body = "\n".join(
            _render_set_section(label, basket_sets[label], date_str)
            for label in sorted(basket_sets.keys())
        )

    html = _shell(header + body, backtest_index_exists)
    with open(output_path, "w") as f:
        f.write(html)


def _shell(body_html, backtest_index_exists):
    nav = (
        '<div class="nav"><a href="index.html">&larr; Full backtest dashboard</a></div>'
        if backtest_index_exists else
        '<div class="nav" style="color: var(--muted);">Backtest dashboard not generated yet</div>'
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="60">
<title>Live Straddle Status</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/plotly.js/2.32.0/plotly.min.js"></script>
<style>{STYLE}</style>
</head>
<body>
{nav}
{body_html}
</body>
</html>
"""
