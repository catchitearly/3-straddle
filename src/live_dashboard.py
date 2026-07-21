"""
Renders a single-page "live status" HTML view from live_notifier's
data/live_state.json - shows today's strike, current price vs VWAP, and every
basket entry/exit so far. Same dark visual style as dashboard_generator.py's
backtest tabs, and links to/from it.

Meant to be regenerated and redeployed to GitHub Pages on every live_notifier
run (every ~2 minutes), so it's effectively a near-real-time view as long as
the workflow keeps firing.
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


def generate_live_page(state, output_path, backtest_index_exists=True):
    if state is None:
        html = _shell("No live data yet — the notifier hasn't run today.",
                       backtest_index_exists)
        with open(output_path, "w") as f:
            f.write(html)
        return

    date_str = state.get("date", "-")
    strike_fixed = state.get("strike_fixed")
    squared_off = state.get("squared_off")
    entries = state.get("entries", [])

    realized_pnl = sum(e["pnl"] for e in entries if e.get("pnl") is not None)
    open_entries = [e for e in entries if e.get("exit_price") is None]
    closed_entries = [e for e in entries if e.get("exit_price") is not None]

    status_label = "SQUARED OFF" if squared_off else ("LIVE" if strike_fixed else "AWAITING STRIKE FIX")
    status_class = "done" if squared_off else "live"

    if not strike_fixed:
        body = f"""
        <div class="chart-box no-data">
          Strike not fixed yet for {date_str} — waiting for {state.get('last_updated') or '09:45'} IST.
        </div>"""
    else:
        strikes = state.get("strikes") or [None, None, None]
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

        last_price = state.get("last_price")
        last_vwap = state.get("last_vwap")
        price_line = (
            f"Combined price: <b>{last_price:.2f}</b> vs VWAP <b>{last_vwap:.2f}</b>"
            if last_price is not None and last_vwap is not None
            else "Waiting for first price update..."
        )

        body = f"""
        <div class="summary">
          <div class="stat"><div class="label">Realized PnL</div>
            <div class="value {'pos' if realized_pnl >= 0 else 'neg'}">&#8377;{realized_pnl:+.0f}</div></div>
          <div class="stat"><div class="label">Baskets Deployed</div><div class="value">{state.get('baskets_deployed', 0)}</div></div>
          <div class="stat"><div class="label">Open</div><div class="value">{len(open_entries)}</div></div>
          <div class="stat"><div class="label">Closed</div><div class="value">{len(closed_entries)}</div></div>
        </div>
        <div class="chart-box">
          <div style="margin-bottom:8px; color: var(--muted); font-size: 13px;">
            Strikes: <b style="color:var(--text)">{strikes[0]} / {strikes[1]} / {strikes[2]}</b>
            &nbsp;&middot;&nbsp; Expiry: <b style="color:var(--text)">{state.get('expiry','-')}</b>
            &nbsp;&middot;&nbsp; Last update: <b style="color:var(--text)">{state.get('last_updated') or '-'}</b> IST
          </div>
          <div style="font-size:14px;">{price_line}</div>
        </div>
        <div class="chart-box">
          <table>
            <thead><tr><th>#</th><th>Entry Time</th><th>Entry Price</th><th>Exit Time</th><th>Exit Price</th><th>Exit Reason</th><th>PnL</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>"""

    header = f"""
    <h1>Live Straddle Basket Status
      <span class="status-pill {status_class}">{status_label}</span>
    </h1>
    <div class="subtitle">{date_str} &middot; auto-refreshes every 2 minutes while the notifier is running</div>
    """

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
<style>{STYLE}</style>
</head>
<body>
{nav}
{body_html}
</body>
</html>
"""
