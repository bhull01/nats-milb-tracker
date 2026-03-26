"""
HTML dashboard generator.

Produces a single self-contained HTML file with:
  - Date navigation (prev/next links if data exists)
  - Game results per affiliate with W-L record
  - Full box score stat lines (prospects highlighted)
  - Key Performers section
  - Season-to-date prospect stats with 7-day / 15-day trends
  - Multi-source ranking display (MLB / FG / BA)
"""

import logging
from datetime import datetime, timedelta

import db as database
from config import LEVEL_ORDER, prospect_lookup, load_prospects

log = logging.getLogger("nats_milb.dashboard")


def _fmt_avg(val: float) -> str:
    """Format a batting average as .XXX"""
    if val is None or val == 0:
        return ".000"
    return f"{val:.3f}"


def _trend_arrow(recent_avg: float | None, season_avg: float | None) -> str:
    """Return an HTML arrow comparing recent to season avg."""
    if recent_avg is None or season_avg is None:
        return ""
    diff = recent_avg - season_avg
    if diff > 0.030:
        return '<span class="trend-up" title="Hot">&#9650;</span>'
    elif diff < -0.030:
        return '<span class="trend-down" title="Cold">&#9660;</span>'
    return '<span class="trend-flat" title="Steady">&#9654;</span>'


def _trend_arrow_era(recent_era: float | None, season_era: float | None) -> str:
    if recent_era is None or season_era is None:
        return ""
    diff = recent_era - season_era
    if diff < -0.50:
        return '<span class="trend-up" title="Improving">&#9650;</span>'
    elif diff > 0.50:
        return '<span class="trend-down" title="Struggling">&#9660;</span>'
    return '<span class="trend-flat" title="Steady">&#9654;</span>'


def _rank_cell(prospect: dict) -> str:
    """Render the multi-source rank badge: MLB/FG/BA."""
    parts = []
    for src, label, color in [
        ("mlb_rank", "MLB", "#AB0003"),
        ("fg_rank", "FG", "#1a7a3a"),
        ("ba_rank", "BA", "#14225A"),
    ]:
        r = prospect.get(src)
        if r:
            parts.append(f'<span class="rank-pip" style="background:{color}" '
                         f'title="{label} Pipeline #{r}">{r}</span>')
        else:
            parts.append(f'<span class="rank-pip rank-na" title="{label}: N/A">—</span>')
    return f'<span class="rank-cluster">{" ".join(parts)}</span>'


def generate_dashboard(conn, date_str: str, out_path: str):
    """
    Generate the full HTML dashboard for a given date and write to out_path.
    """
    prospects = prospect_lookup()
    prospect_names = set(prospects.keys())

    # Navigation: find prev/next dates with data
    all_dates = database.dates_with_data(conn)
    prev_date = next_date = None
    if date_str in all_dates:
        idx = all_dates.index(date_str)
        if idx < len(all_dates) - 1:
            prev_date = all_dates[idx + 1]
        if idx > 0:
            next_date = all_dates[idx - 1]

    # Games — Nats affiliates only (source_org IS NULL)
    games = database.games_on_date(conn, date_str, affiliate_only=True)
    games_by_level = {}
    for g in games:
        games_by_level.setdefault(g["level"], []).append(g)

    # Build affiliate sections
    affiliates_html = ""
    for level in LEVEL_ORDER:
        level_games = games_by_level.get(level, [])
        if not level_games:
            # Check record even if no game today
            record = database.team_record(conn, level)
            affiliates_html += f"""
            <div class="affiliate-section">
                <div class="affiliate-header">
                    <h2><span class="level-badge">{level}</span>
                    <span class="no-game">No game scheduled</span></h2>
                    {f'<span class="record">({record["wins"]}-{record["losses"]})</span>' if record["games"] else ''}
                </div>
            </div>"""
            continue

        for g in level_games:
            record = database.team_record(conn, level)
            result_cls = {"W": "result-win", "L": "result-loss"}.get(g["result"], "result-pending")
            ha = "vs" if g["is_home"] else "@"

            if g["result"]:
                badge = f'<span class="result-badge {result_cls}">{g["result"]} {g["our_score"]}-{g["opp_score"]}</span>'
                summary = f'{g["team_name"]} {ha} {g["opponent"]} — {badge}'
            else:
                badge = f'<span class="result-badge result-pending">{g["status"]}</span>'
                summary = f'{g["team_name"]} — {badge}'

            record_str = f'<span class="record">({record["wins"]}-{record["losses"]})</span>' if record["games"] else ''

            # Hitting table — affiliate box score only (no external-org lines)
            hitters = database.hitting_for_game(conn, g["game_pk"], affiliate_only=True)
            hit_rows = ""
            for h in hitters:
                is_p = h["player_name"].lower() in prospect_names
                p_info = prospects.get(h["player_name"].lower(), {})
                cls = ' class="prospect-row"' if is_p else ""
                rank_html = _rank_cell(p_info) + " " if is_p else ""
                avg = _fmt_avg(h["h"] / h["ab"]) if h["ab"] else ".000"
                hit_rows += f"""<tr{cls}>
                    <td>{rank_html}{h['player_name']}</td><td>{h['position']}</td>
                    <td>{h['ab']}</td><td>{h['r']}</td><td>{h['h']}</td>
                    <td>{h['doubles']}</td><td>{h['triples']}</td><td>{h['hr']}</td>
                    <td>{h['rbi']}</td><td>{h['bb']}</td><td>{h['k']}</td>
                    <td>{h['sb']}</td><td>{avg}</td>
                </tr>"""

            # Pitching table — affiliate box score only
            pitchers = database.pitching_for_game(conn, g["game_pk"], affiliate_only=True)
            pitch_rows = ""
            for p in pitchers:
                is_p = p["player_name"].lower() in prospect_names
                p_info = prospects.get(p["player_name"].lower(), {})
                cls = ' class="prospect-row"' if is_p else ""
                rank_html = _rank_cell(p_info) + " " if is_p else ""
                pitch_rows += f"""<tr{cls}>
                    <td>{rank_html}{p['player_name']}</td><td>{p['position']}</td>
                    <td>{p['ip']:.1f}</td><td>{p['h']}</td><td>{p['r']}</td>
                    <td>{p['er']}</td><td>{p['bb']}</td><td>{p['k']}</td>
                    <td>{p['decision']}</td>
                </tr>"""

            hit_table = f"""<h3>Hitting</h3>
            <div class="table-wrap"><table class="stats-table">
            <thead><tr><th>Name</th><th>Pos</th><th>AB</th><th>R</th><th>H</th>
            <th>2B</th><th>3B</th><th>HR</th><th>RBI</th><th>BB</th><th>K</th>
            <th>SB</th><th>AVG</th></tr></thead>
            <tbody>{hit_rows}</tbody></table></div>""" if hit_rows else ""

            pitch_table = f"""<h3>Pitching</h3>
            <div class="table-wrap"><table class="stats-table">
            <thead><tr><th>Name</th><th>Pos</th><th>IP</th><th>H</th><th>R</th>
            <th>ER</th><th>BB</th><th>K</th><th>Dec</th></tr></thead>
            <tbody>{pitch_rows}</tbody></table></div>""" if pitch_rows else ""

            no_stats = '<p class="no-data">No stats available (game not yet completed or was postponed).</p>' if not hit_rows and not pitch_rows else ""

            affiliates_html += f"""
            <div class="affiliate-section">
                <div class="affiliate-header">
                    <h2><span class="level-badge">{level}</span> {summary} {record_str}</h2>
                </div>
                {hit_table}{pitch_table}{no_stats}
            </div>"""

    # ── Key performers ──────────────────────────────────────────────
    perf_html = _build_key_performers(conn, date_str, prospects, prospect_names)

    # ── Season prospect tracker ─────────────────────────────────────
    season_html = _build_season_tracker(conn, prospects)

    # ── Navigation ──────────────────────────────────────────────────
    date_display = datetime.strptime(date_str, "%Y-%m-%d").strftime("%A, %B %-d, %Y")
    nav_prev = f'<a href="nats_dashboard.html?date={prev_date}" class="nav-btn" onclick="return false;">&#9664; {prev_date}</a>' if prev_date else '<span class="nav-btn disabled">&#9664; Prev</span>'
    nav_next = f'<a href="nats_dashboard.html?date={next_date}" class="nav-btn" onclick="return false;">&#9654; {next_date}</a>' if next_date else '<span class="nav-btn disabled">Next &#9654;</span>'

    # Date list for the JS date picker
    date_options = "\n".join(f'<option value="{d}" {"selected" if d == date_str else ""}>{d}</option>' for d in all_dates)

    html = _full_html(date_str, date_display, nav_prev, nav_next, date_options,
                      perf_html, affiliates_html, season_html)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    log.info("Dashboard → %s", out_path)


def _build_key_performers(conn, date_str, prospects, prospect_names):
    """Build the Key Performers HTML section (Nats affiliate games only)."""
    games = database.games_on_date(conn, date_str, affiliate_only=True)
    performers = []

    for g in games:
        for h in database.hitting_for_game(conn, g["game_pk"], affiliate_only=True):
            if h["player_name"].lower() not in prospect_names:
                continue
            p = prospects[h["player_name"].lower()]
            score = (h["h"] * 1 + h["doubles"] * 0.5 + h["triples"] * 1
                     + h["hr"] * 2 + h["rbi"] * 0.5 + h["bb"] * 0.3
                     + h["sb"] * 0.5 + h["r"] * 0.3)
            if score > 0 or h["ab"] >= 3:
                extras = []
                if h["hr"]: extras.append(f'{h["hr"]} HR')
                if h["rbi"]: extras.append(f'{h["rbi"]} RBI')
                if h["doubles"]: extras.append(f'{h["doubles"]} 2B')
                if h["triples"]: extras.append(f'{h["triples"]} 3B')
                if h["bb"]: extras.append(f'{h["bb"]} BB')
                if h["sb"]: extras.append(f'{h["sb"]} SB')
                if h["r"]: extras.append(f'{h["r"]} R')
                line_str = f'{h["h"]}-{h["ab"]}'
                if extras:
                    line_str += f', {", ".join(extras)}'
                performers.append({
                    "name": h["player_name"],
                    "rank_html": _rank_cell(p),
                    "level": g["level"],
                    "team": g["team_name"],
                    "line": line_str,
                    "score": score,
                    "composite": p.get("composite_rank", 99),
                })

        for pt in database.pitching_for_game(conn, g["game_pk"], affiliate_only=True):
            if pt["player_name"].lower() not in prospect_names:
                continue
            p = prospects[pt["player_name"].lower()]
            if pt["ip"] >= 1.0:
                score = pt["ip"] * 1.0 + pt["k"] * 0.5 - pt["er"] * 1.0
                line_str = f'{pt["ip"]:.1f} IP, {pt["k"]} K, {pt["er"]} ER'
                if pt["decision"]:
                    line_str += f' {pt["decision"]}'
                performers.append({
                    "name": pt["player_name"],
                    "rank_html": _rank_cell(p),
                    "level": g["level"],
                    "team": g["team_name"],
                    "line": line_str,
                    "score": score,
                    "composite": p.get("composite_rank", 99),
                })

    performers.sort(key=lambda x: (-x["score"], x["composite"]))

    if not performers:
        return '<p class="no-data">No prospect performances to report for this date.</p>'

    cards = ""
    for pf in performers[:10]:
        cards += f"""
        <div class="performer-card">
            <div class="performer-rank">{pf['rank_html']}</div>
            <div class="performer-info">
                <div class="performer-name">{pf['name']}</div>
                <div class="performer-team">{pf['level']} – {pf['team']}</div>
            </div>
            <div class="performer-line">{pf['line']}</div>
        </div>"""
    return cards


def _build_season_tracker(conn, prospects):
    """Build the season-to-date prospect tracker with trends."""
    hit_rows = ""
    pitch_rows = ""

    sorted_prospects = sorted(prospects.values(), key=lambda p: p.get("composite_rank", 99))

    for p in sorted_prospects:
        name = p["name"]

        # Try hitting
        season = database.season_hitting_totals(conn, name)
        if season:
            last7 = database.rolling_hitting(conn, name, 7)
            last15 = database.rolling_hitting(conn, name, 15)
            arrow = _trend_arrow(
                last7["avg"] if last7 else None,
                season["avg"],
            )
            l7_str = f'{_fmt_avg(last7["avg"])} ({last7["games"]}g)' if last7 else "—"
            l15_str = f'{_fmt_avg(last15["avg"])} ({last15["games"]}g)' if last15 else "—"

            hit_rows += f"""<tr class="prospect-row">
                <td>{_rank_cell(p)} {name}</td>
                <td>{p.get('position','')}</td><td>{p.get('level','')}</td>
                <td>{season['games']}</td><td>{season['ab']}</td>
                <td>{_fmt_avg(season['avg'])}</td><td>{season['h']}</td>
                <td>{season['hr']}</td><td>{season['rbi']}</td>
                <td>{season['bb']}</td><td>{season['k']}</td>
                <td>{season['sb']}</td>
                <td>{_fmt_avg(season['obp'])}</td>
                <td>{_fmt_avg(season['slg'])}</td>
                <td>{season['ops']:.3f}</td>
                <td>{l7_str} {arrow}</td>
                <td>{l15_str}</td>
            </tr>"""

        # Try pitching
        pseason = database.season_pitching_totals(conn, name)
        if pseason:
            last7p = database.rolling_pitching(conn, name, 7)
            last15p = database.rolling_pitching(conn, name, 15)
            arrow = _trend_arrow_era(
                last7p["era"] if last7p else None,
                pseason["era"],
            )
            l7_str = f'{last7p["era"]:.2f} ({last7p["games"]}g)' if last7p else "—"
            l15_str = f'{last15p["era"]:.2f} ({last15p["games"]}g)' if last15p else "—"

            pitch_rows += f"""<tr class="prospect-row">
                <td>{_rank_cell(p)} {name}</td>
                <td>{p.get('position','')}</td><td>{p.get('level','')}</td>
                <td>{pseason['games']}</td><td>{pseason['ip']:.1f}</td>
                <td>{pseason['era']:.2f}</td><td>{pseason['whip']:.2f}</td>
                <td>{pseason['k']}</td><td>{pseason['bb']}</td>
                <td>{pseason['h']}</td><td>{pseason['er']}</td>
                <td>{pseason['k_per_9']:.1f}</td>
                <td>{l7_str} {arrow}</td>
                <td>{l15_str}</td>
            </tr>"""

    sections = ""
    if hit_rows:
        sections += f"""
        <h3>Position Prospect Season Stats</h3>
        <div class="table-wrap"><table class="stats-table">
        <thead><tr>
            <th>Name</th><th>Pos</th><th>Level</th><th>G</th><th>AB</th>
            <th>AVG</th><th>H</th><th>HR</th><th>RBI</th><th>BB</th>
            <th>K</th><th>SB</th><th>OBP</th><th>SLG</th><th>OPS</th>
            <th>Last 7d</th><th>Last 15d</th>
        </tr></thead>
        <tbody>{hit_rows}</tbody></table></div>"""

    if pitch_rows:
        sections += f"""
        <h3>Pitching Prospect Season Stats</h3>
        <div class="table-wrap"><table class="stats-table">
        <thead><tr>
            <th>Name</th><th>Pos</th><th>Level</th><th>G</th><th>IP</th>
            <th>ERA</th><th>WHIP</th><th>K</th><th>BB</th><th>H</th>
            <th>ER</th><th>K/9</th><th>Last 7d</th><th>Last 15d</th>
        </tr></thead>
        <tbody>{pitch_rows}</tbody></table></div>"""

    if not sections:
        return ""

    return f"""
    <div class="prospect-tracker">
        <h2>Season-to-Date Prospect Tracker</h2>
        <div class="rank-legend">
            Rankings: <span class="rank-pip" style="background:#AB0003">MLB</span>
            <span class="rank-pip" style="background:#1a7a3a">FG</span>
            <span class="rank-pip" style="background:#14225A">BA</span>
            &nbsp;|&nbsp;
            <span class="trend-up">&#9650;</span> Hot
            <span class="trend-flat">&#9654;</span> Steady
            <span class="trend-down">&#9660;</span> Cold
        </div>
        {sections}
    </div>"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  FULL HTML TEMPLATE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _full_html(date_str, date_display, nav_prev, nav_next, date_options,
               perf_html, affiliates_html, season_html):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Nats MiLB Tracker – {date_str}</title>
<style>
:root {{
    --nats-red: #AB0003; --nats-navy: #14225A; --nats-gold: #C4A049;
    --bg: #f4f5f7; --card-bg: #fff; --border: #e1e4e8;
    --text: #24292e; --muted: #586069;
    --green-light: #e6ffed; --red-light: #ffeef0;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
       background:var(--bg); color:var(--text); line-height:1.5; }}
.header {{ background:linear-gradient(135deg,var(--nats-navy),#1a2d6d);
           color:#fff; padding:20px 32px; text-align:center; }}
.header h1 {{ font-size:26px; font-weight:700; }}
.header .date {{ font-size:15px; opacity:.85; margin-top:2px; }}
.header .curly-w {{ font-size:38px; color:var(--nats-red);
                    text-shadow:0 0 20px rgba(171,0,3,.5); }}
.nav {{ display:flex; justify-content:center; align-items:center; gap:12px;
        padding:12px; background:#fff; border-bottom:1px solid var(--border); }}
.nav-btn {{ padding:6px 14px; border-radius:6px; text-decoration:none;
            font-size:13px; font-weight:600; border:1px solid var(--border);
            color:var(--nats-navy); background:#fff; cursor:pointer; }}
.nav-btn:hover {{ background:var(--green-light); }}
.nav-btn.disabled {{ opacity:.4; cursor:default; }}
.nav select {{ padding:5px 8px; border-radius:6px; border:1px solid var(--border);
               font-size:13px; }}
.container {{ max-width:1300px; margin:0 auto; padding:20px 16px; }}
.key-performers {{ background:var(--card-bg); border:2px solid var(--nats-red);
                   border-radius:12px; padding:18px 22px; margin-bottom:22px; }}
.key-performers h2 {{ color:var(--nats-red); font-size:18px; margin-bottom:14px; }}
.key-performers h2::before {{ content:"\\2B50 "; }}
.performer-card {{ display:flex; align-items:center; gap:14px;
                   padding:8px 12px; border-radius:8px; margin-bottom:6px;
                   background:#fafbfc; border:1px solid var(--border); }}
.performer-card:hover {{ background:var(--green-light); }}
.performer-rank {{ min-width:80px; }}
.performer-info {{ flex:1; }}
.performer-name {{ font-weight:600; font-size:14px; }}
.performer-team {{ font-size:11px; color:var(--muted); }}
.performer-line {{ font-family:'SF Mono',Menlo,monospace; font-size:12px;
                   color:var(--nats-navy); font-weight:600; white-space:nowrap; }}
.affiliate-section {{ background:var(--card-bg); border-radius:12px;
                      padding:18px 22px; margin-bottom:18px;
                      border:1px solid var(--border);
                      box-shadow:0 1px 3px rgba(0,0,0,.04); }}
.affiliate-header h2 {{ font-size:16px; display:flex; align-items:center;
                        gap:8px; flex-wrap:wrap; }}
.level-badge {{ background:var(--nats-navy); color:#fff; padding:2px 8px;
               border-radius:5px; font-size:11px; font-weight:600;
               text-transform:uppercase; letter-spacing:.5px; }}
.result-badge {{ padding:2px 8px; border-radius:5px; font-size:13px; font-weight:700; }}
.result-win {{ background:var(--green-light); color:#22863a; }}
.result-loss {{ background:var(--red-light); color:#cb2431; }}
.result-pending {{ background:#fff8e1; color:#856404; }}
.record {{ font-size:13px; color:var(--muted); font-weight:400; }}
.no-game {{ font-size:14px; color:var(--muted); font-weight:400; }}
h3 {{ font-size:13px; color:var(--muted); text-transform:uppercase;
      letter-spacing:.5px; margin:14px 0 6px; }}
.table-wrap {{ overflow-x:auto; }}
.stats-table {{ width:100%; border-collapse:collapse; font-size:12px; }}
.stats-table th {{ background:var(--nats-navy); color:#fff; padding:6px 8px;
                   text-align:left; font-weight:600; font-size:10px;
                   text-transform:uppercase; letter-spacing:.3px; white-space:nowrap; }}
.stats-table td {{ padding:5px 8px; border-bottom:1px solid var(--border);
                   white-space:nowrap; }}
.stats-table tbody tr:hover {{ background:#f6f8fa; }}
.prospect-row {{ background:#fffde7 !important; }}
.prospect-row:hover {{ background:#fff9c4 !important; }}
.rank-cluster {{ display:inline-flex; gap:2px; vertical-align:middle; }}
.rank-pip {{ display:inline-block; color:#fff; padding:1px 5px;
            border-radius:3px; font-size:9px; font-weight:700;
            min-width:18px; text-align:center; }}
.rank-na {{ background:#ccc !important; }}
.rank-legend {{ font-size:12px; color:var(--muted); margin-bottom:12px; }}
.rank-legend .rank-pip {{ font-size:9px; vertical-align:middle; }}
.trend-up {{ color:#22863a; font-weight:700; }}
.trend-down {{ color:#cb2431; font-weight:700; }}
.trend-flat {{ color:#6a737d; font-size:10px; }}
.prospect-tracker {{ background:var(--card-bg); border:2px solid var(--nats-gold);
                     border-radius:12px; padding:18px 22px; margin-top:22px; }}
.prospect-tracker h2 {{ color:var(--nats-navy); font-size:18px; margin-bottom:8px; }}
.no-data {{ color:var(--muted); font-style:italic; padding:10px 0; }}
.footer {{ text-align:center; padding:20px; color:var(--muted); font-size:11px; }}
@media(max-width:768px) {{
    .header {{ padding:14px; }}
    .header h1 {{ font-size:18px; }}
    .container {{ padding:10px 6px; }}
    .performer-card {{ flex-direction:column; gap:4px; text-align:center; }}
}}
</style>
</head>
<body>
<div class="header">
    <div class="curly-w">W</div>
    <h1>Nationals Minor League Tracker</h1>
    <div class="date">{date_display}</div>
</div>
<div class="nav">
    {nav_prev}
    <select id="date-picker" onchange="alert('Regenerate dashboard for ' + this.value + ' with: python3 nats_tracker.py dashboard ' + this.value)">
        {date_options}
    </select>
    {nav_next}
</div>
<div class="container">
    <div class="key-performers">
        <h2>Key Performers</h2>
        {perf_html}
    </div>
    {affiliates_html}
    {season_html}
</div>
<div class="footer">
    Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} &middot;
    Data from MLB Stats API &middot; Nats MiLB Tracker
</div>
</body>
</html>"""
