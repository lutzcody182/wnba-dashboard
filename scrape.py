#!/usr/bin/env python3
"""
Scrapes a handful of hoopsjunkie.io WNBA stat pages and writes data.js,
which the dashboard loads to override its embedded snapshot data with
fresh numbers. Run daily by .github/workflows/refresh-data.yml (and
runnable manually from the Actions tab any time).

Covers: standings, jump balls won, jump ball win %, early offense
frequency, the 19-column player Advanced Stats table, and the 44-column
Team Advanced Stats table (17 advanced + 27 scoring categories). Each of
those last two is assembled by fetching one hoopsjunkie.io leaderboard
page per column (paginated for players) and merging by player slug /
team code — the same column order as advCols / teamCols in the
dashboard's <script>, so don't reorder one without the other.

Injury reports, live scores, and betting odds are NOT scraped here — the
dashboard fetches those directly from ESPN's public API client-side, on
every page load, so they don't depend on this script or GitHub Actions
running at all.

This script is deliberately defensive: if a page's structure doesn't
match what we expect (site redesign, empty response, etc.), it skips
updating that dataset and logs a warning rather than writing bad data.
Uses only the Python standard library + BeautifulSoup4.
"""
import json
import re
import sys
import time
import urllib.request

from bs4 import BeautifulSoup

BASE = "https://hoopsjunkie.io"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; wnba-dashboard-refresh/1.0; +https://github.com/lutzcody182/wnba-dashboard)"}

TEAM_NAMES = {
    "ATL": "Atlanta Dream", "CHI": "Chicago Sky", "CON": "Connecticut Sun",
    "DAL": "Dallas Wings", "GSV": "Golden State Valkyries", "IND": "Indiana Fever",
    "LVA": "Las Vegas Aces", "LAS": "Los Angeles Sparks", "MIN": "Minnesota Lynx",
    "NYL": "New York Liberty", "PHX": "Phoenix Mercury", "PDX": "Portland Fire",
    "SEA": "Seattle Storm", "TOR": "Toronto Tempo", "WAS": "Washington Mystics",
}
TEAM_CODES = set(TEAM_NAMES.keys())


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_all_pages(url, max_pages=6):
    """Follow pagination (?page=2, ?page=3, ...) until a page repeats/empties out."""
    htmls = []
    seen_slugs_per_page = []
    page_num = 1
    while page_num <= max_pages:
        sep = "&" if "?" in url else "?"
        page_url = url if page_num == 1 else f"{url}{sep}page={page_num}"
        html = fetch(page_url)
        slugs = set(re.findall(r'/wnba/player/([a-z0-9-]+)', html))
        if not slugs or slugs in seen_slugs_per_page:
            break
        htmls.append(html)
        seen_slugs_per_page.append(slugs)
        page_num += 1
        time.sleep(0.5)
    return htmls


def parse_player_leaderboard(html_pages):
    """
    Structural parse: find every <a href="/wnba/player/{slug}"> in the ranked
    table, walk up to its row, and pull (team-code, value) out of the row's
    remaining cell text. Robust to exact class names / markup changes.
    Returns list of (slug, display_name, team, value) in page order (already ranked).
    """
    out = []
    seen = set()
    for html in html_pages:
        soup = BeautifulSoup(html, "html.parser")
        for row in soup.find_all("tr"):
            player_links = row.find_all("a", href=re.compile(r"^/wnba/player/[a-z0-9-]+$"))
            if not player_links:
                continue
            # A row often has two links to the same player (a headshot image link with no
            # text, and a text link) — use the one with actual text for name/team.
            slug = player_links[0]["href"].rsplit("/", 1)[-1]
            link_text = ""
            for a in player_links:
                t = a.get_text(" ", strip=True)
                if t:
                    link_text = t
                    slug = a["href"].rsplit("/", 1)[-1]
                    break
            if slug in seen:
                continue
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if not cells:
                continue
            # The link text itself is usually "Full Name TEAM" (possibly "TEAM1/TEAM2" for
            # players who changed teams mid-season) — split off the trailing team code(s).
            m = re.match(r"^(.*?)\s+([A-Z]{2,3}(?:/[A-Z]{2,3})?)$", link_text)
            if m:
                name, team = m.group(1).strip(), m.group(2)
            else:
                name, team = link_text, ""
            # Value is the last non-empty cell that isn't the rank number or the name/team cell.
            value = None
            for c in reversed(cells):
                if c == link_text or c.rstrip("0123456789") == "" or c == team:
                    continue
                value = c
                break
            if value is None:
                continue
            seen.add(slug)
            out.append((slug, name, team, value))
    return out


# Column order must exactly match advCols in wnba-dashboard.html's <script>.
ADV_PLAYER_COLS = [
    ("ORtg", "advanced/offensive-rating"),
    ("DRtg", "advanced/defensive-rating"),
    ("NetRtg", "advanced/net-rating"),
    ("GmSc", "advanced/game-score"),
    ("PER", "advanced/player-efficiency-rating"),
    ("eFG", "advanced/effective-field-goal-percent"),
    ("TS", "advanced/true-shooting-percent"),
    ("FTr", "advanced/free-throw-rate"),
    ("3PAr", "advanced/three-point-rate"),
    ("OREB", "advanced/offensive-rebound-percent"),
    ("DREB", "advanced/defensive-rebound-percent"),
    ("REB", "advanced/rebound-percent"),
    ("AST", "advanced/assist-percent"),
    ("ASTRatio", "advanced/assist-ratio"),
    ("ASTTOV", "advanced/assist-to-turnover-ratio"),
    ("STL", "advanced/steal-percent"),
    ("BLK", "advanced/block-percent"),
    ("USG", "advanced/usage-percent"),
    ("TOVRatio", "advanced/turnover-ratio"),
]

# Column order must exactly match teamCols in wnba-dashboard.html's <script>.
TEAM_COLS = [
    ("ORtg", "advanced/offensive-rating"),
    ("DRtg", "advanced/defensive-rating"),
    ("NetRtg", "advanced/net-rating"),
    ("Pace", "advanced/pace"),
    ("PossDur", "advanced/avg-possession-duration"),
    ("eFG", "advanced/effective-field-goal-percent"),
    ("TS", "advanced/true-shooting-percent"),
    ("FTr", "advanced/free-throw-rate"),
    ("3PAr", "advanced/three-point-rate"),
    ("OREB", "advanced/offensive-rebound-percent"),
    ("DREB", "advanced/defensive-rebound-percent"),
    ("AST", "advanced/assist-percent"),
    ("ASTRatio", "advanced/assist-ratio"),
    ("ASTTOV", "advanced/assist-to-turnover-ratio"),
    ("STL", "advanced/steal-percent"),
    ("BLK", "advanced/block-percent"),
    ("TOVRatio", "advanced/turnover-ratio"),
    ("RA_FG", "scoring/restricted-area-fg-percent"),
    ("PaintFG", "scoring/paint-non-ra-fg-percent"),
    ("MidRangeFG", "scoring/mid-range-fg-percent"),
    ("AB3FG", "scoring/above-break-3-fg-percent"),
    ("LC3FG", "scoring/left-corner-3-fg-percent"),
    ("RC3FG", "scoring/right-corner-3-fg-percent"),
    ("FG0_4", "scoring/0-4-ft-fg-percent"),
    ("FG5_9", "scoring/5-9-ft-fg-percent"),
    ("FG10_14", "scoring/10-14-ft-fg-percent"),
    ("FG15_19", "scoring/15-19-ft-fg-percent"),
    ("FG20_24", "scoring/20-24-ft-fg-percent"),
    ("FG25plus", "scoring/25-plus-ft-fg-percent"),
    ("FBPts", "scoring/fast-break-points"),
    ("FBAst", "scoring/fast-break-assists"),
    ("FBFG", "scoring/fast-break-fg-percent"),
    ("FB3Pts", "scoring/fast-break-3pt-points"),
    ("FB3FG", "scoring/fast-break-3pt-percent"),
    ("SCPts", "scoring/second-chance-points"),
    ("SCAst", "scoring/second-chance-assists"),
    ("SCFG", "scoring/second-chance-fg-percent"),
    ("SC3Pts", "scoring/second-chance-3pt-points"),
    ("SC3FG", "scoring/second-chance-3pt-percent"),
    ("POT", "scoring/points-off-turnovers"),
    ("OTAst", "scoring/off-turnover-assists"),
    ("OTFG", "scoring/off-turnover-fg-percent"),
    ("OT3Pts", "scoring/off-turnover-3pt-points"),
    ("OT3FG", "scoring/off-turnover-3pt-percent"),
]


def parse_team_leaderboard(html):
    """
    Structural parse of a single-page team leaderboard: finds every
    <a href="/wnba/team/{code}..."> (hoopsjunkie appends "?season=2026" to these,
    hence the prefix match), walks up to its row, and takes the last cell that
    looks like a number/percentage as the value. Returns {code: value}.
    """
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for row in soup.find_all("tr"):
        team_links = row.find_all("a", href=re.compile(r"^/wnba/team/[a-z]+"))
        if not team_links:
            continue
        m = re.match(r"^/wnba/team/([a-z]+)", team_links[0]["href"])
        if not m:
            continue
        code = m.group(1).upper()
        if code not in TEAM_CODES or code in out:
            continue
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        if not cells:
            continue
        value = cells[-1]
        if not re.match(r"^-?\+?\d", value):  # last cell should look numeric; skip if not
            continue
        out[code] = value
    return out


def scrape_player_advanced():
    """Returns (advNamesRaw, advStatsRaw) strings in the dashboard's exact raw format,
    or (None, None) if the merged result looks too small to trust."""
    names, teams, stats = {}, {}, {}
    for col_key, slug_path in ADV_PLAYER_COLS:
        pages = fetch_all_pages(f"{BASE}/wnba/stats/2026/players/{slug_path}")
        for slug, name, team, value in parse_player_leaderboard(pages):
            names.setdefault(slug, name)
            if team:
                teams[slug] = team
            stats.setdefault(slug, {})[col_key] = value
        time.sleep(0.3)
    if len(names) < 80:  # sanity floor — the real dataset has 100+ qualified players
        return None, None
    names_lines = [f"{slug}={names[slug]}" for slug in names]
    stats_lines = []
    for slug in names:
        vals = [stats.get(slug, {}).get(col_key, "-") for col_key, _ in ADV_PLAYER_COLS]
        stats_lines.append(f"{slug}|{teams.get(slug, '')}|{'|'.join(vals)}")
    return "\n".join(names_lines), "\n".join(stats_lines)


def scrape_team_advanced():
    """Returns teamStatsRaw string in the dashboard's exact raw format, or None if
    fewer than all 15 teams came back for enough columns to trust."""
    stats = {}
    for col_key, slug_path in TEAM_COLS:
        html = fetch(f"{BASE}/wnba/stats/2026/teams/{slug_path}")
        for code, value in parse_team_leaderboard(html).items():
            stats.setdefault(code, {})[col_key] = value
        time.sleep(0.3)
    if len(stats) != 15:
        return None
    lines = []
    for code in sorted(stats.keys()):
        vals = [stats[code].get(col_key, "-") for col_key, _ in TEAM_COLS]
        lines.append(f"{code}|{'|'.join(vals)}")
    return "\n".join(lines)


def scrape_standings():
    html = fetch(f"{BASE}/wnba/standings")
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for a in soup.find_all("a", href=re.compile(r"^/wnba/team/[a-z]+$")):
        row = a.find_parent("tr")
        if row is None:
            continue
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        if len(cells) < 4:
            continue
        # Row shape: [rank, "CODE(x)" or "CODE", W, L, PCT]. The (x)/(o) suffix is a
        # sibling text node in the same <td> as the team link, not inside the <a> itself.
        team_td = a.find_parent("td")
        team_td_text = team_td.get_text(" ", strip=True) if team_td else a.get_text(" ", strip=True)
        code_cell = team_td_text.replace(" ", "")
        m = re.match(r"^([A-Z]{2,3})(\(x\)|\(o\))?$", code_cell)
        if not m:
            continue
        code, flagraw = m.group(1), m.group(2)
        if code not in TEAM_CODES:
            continue
        flag = "x" if flagraw == "(x)" else ("o" if flagraw == "(o)" else "")
        rank_m = re.match(r"^(\d+)$", cells[0])
        if not rank_m:
            continue
        rank = int(rank_m.group(1))
        w_l_pct = [c for c in cells if c != cells[0] and c != team_td_text]
        w_l_pct = [c for c in w_l_pct if re.match(r"^\d+$", c) or re.match(r"^\.\d+$", c)]
        if len(w_l_pct) < 3:
            continue
        w, l, pct = w_l_pct[0], w_l_pct[1], w_l_pct[2]
        rows.append({
            "rank": rank, "code": code, "name": TEAM_NAMES.get(code, code),
            "w": int(w), "l": int(l), "pct": pct, "flag": flag,
        })
    rows.sort(key=lambda r: r["rank"])
    return rows


def main():
    out = {}

    print("Scraping standings...", file=sys.stderr)
    try:
        standings = scrape_standings()
    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        standings = []
    if len(standings) == 15:
        out["standings"] = standings
    else:
        print(f"  WARNING: expected 15 teams, got {len(standings)} — skipping standings update", file=sys.stderr)

    print("Scraping jump balls won...", file=sys.stderr)
    try:
        jbw_pages = fetch_all_pages(f"{BASE}/wnba/stats/2026/players/hustle/jump-balls-won")
        jbw = parse_player_leaderboard(jbw_pages)
    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        jbw = []

    print("Scraping jump ball win %...", file=sys.stderr)
    try:
        jbwpct_pages = fetch_all_pages(f"{BASE}/wnba/stats/2026/players/hustle/jump-ball-win-percent")
        jbwpct = {slug: value for slug, name, team, value in parse_player_leaderboard(jbwpct_pages)}
    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        jbwpct = {}

    if len(jbw) >= 50:  # sanity floor — the real dataset has 100+ rows
        lines = [f"{slug}|{name}|{team}|{value}|{jbwpct.get(slug, '-')}" for slug, name, team, value in jbw]
        out["jumpBallsRaw"] = "\n".join(lines)
    else:
        print(f"  WARNING: only parsed {len(jbw)} jump-ball rows — skipping update", file=sys.stderr)

    print("Scraping early offense frequency...", file=sys.stderr)
    try:
        eof_pages = fetch_all_pages(f"{BASE}/wnba/stats/2026/players/play-contexts/early-offense-frequency")
        eof = parse_player_leaderboard(eof_pages)
    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        eof = []
    if len(eof) >= 50:
        out["earlyOffenseFreqRaw"] = "\n".join(f"{slug}|{value}" for slug, name, team, value in eof)
    else:
        print(f"  WARNING: only parsed {len(eof)} early-offense rows — skipping update", file=sys.stderr)

    print("Scraping player advanced stats (19 categories, paginated)...", file=sys.stderr)
    try:
        adv_names, adv_stats = scrape_player_advanced()
    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        adv_names, adv_stats = None, None
    if adv_names and adv_stats:
        out["advNamesRaw"] = adv_names
        out["advStatsRaw"] = adv_stats
    else:
        print("  WARNING: player advanced scrape looked incomplete — skipping update", file=sys.stderr)

    print("Scraping team advanced + scoring stats (44 categories)...", file=sys.stderr)
    try:
        team_stats = scrape_team_advanced()
    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        team_stats = None
    if team_stats:
        out["teamStatsRaw"] = team_stats
    else:
        print("  WARNING: team advanced scrape looked incomplete — skipping update", file=sys.stderr)

    if not out:
        print("Nothing scraped successfully — leaving existing data.js untouched.", file=sys.stderr)
        sys.exit(1)

    out["_updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    with open("data.js", "w") as f:
        f.write("// Auto-generated by scraper/scrape.py — do not edit by hand.\n")
        f.write("window.LIVE_DATA = " + json.dumps(out, indent=2, ensure_ascii=False) + ";\n")

    print(f"Wrote data.js with keys: {list(out.keys())}", file=sys.stderr)


if __name__ == "__main__":
    main()
