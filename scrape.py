#!/usr/bin/env python3
"""
Scrapes a handful of hoopsjunkie.io WNBA stat pages and writes data.js,
which the dashboard loads to override its embedded snapshot data with
fresh numbers. Run daily by .github/workflows/refresh-data.yml (and
runnable manually from the Actions tab any time).

Currently covers: standings, jump balls won, jump ball win %, early
offense frequency (the datasets behind the Standings tab and the Jump
Balls tab), and current injury reports (via ESPN's public injuries API,
matched to schedule games by team name). Advanced Stats / Team Advanced
Stats are NOT covered yet — those tables pull from ~60 additional
paginated pages each and are left on manual refresh for now.

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


ESPN_API = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba"


def normalize_injury_status(raw):
    """Map ESPN's free-text status to the single-word vocabulary the dashboard styles
    (out / doubtful / questionable / probable / available) — anything else just renders
    unstyled rather than breaking."""
    s = (raw or "").strip().lower()
    if "day-to-day" in s or "day to day" in s:
        return "Questionable"
    if "doubtful" in s:
        return "Doubtful"
    if "questionable" in s:
        return "Questionable"
    if "probable" in s:
        return "Probable"
    if "available" in s or "active" in s:
        return "Available"
    if "out" in s:
        return "Out"
    cleaned = re.sub(r"[^A-Za-z]", "", raw or "")
    return cleaned or "Out"


def scrape_injuries():
    """
    Returns { "Atlanta Dream": "Jordin Canada - Out (Illness); ...", ... } for every
    team with at least one listed injury. Teams with none are simply absent from the
    dict — the dashboard falls back to "No injury report available" for those.
    """
    data = json.loads(fetch(f"{ESPN_API}/injuries"))
    by_team = {}
    for team_entry in data.get("injuries", []):
        team_name = team_entry.get("displayName")
        if not team_name:
            continue
        entries = []
        for inj in team_entry.get("injuries", []):
            athlete = inj.get("athlete") or {}
            name = athlete.get("displayName")
            if not name:
                continue
            status = normalize_injury_status(inj.get("status"))
            reason = ((inj.get("details") or {}).get("type") or "").strip()
            entries.append(f"{name} - {status}" + (f" ({reason})" if reason else ""))
        if entries:
            by_team[team_name] = "; ".join(entries)
    return by_team


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

    print("Scraping injury reports (ESPN)...", file=sys.stderr)
    try:
        injuries_by_team = scrape_injuries()
    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        injuries_by_team = {}
    if injuries_by_team:
        out["injuriesByTeam"] = injuries_by_team
    else:
        print("  WARNING: no injuries parsed — skipping update (could be a genuinely healthy day)", file=sys.stderr)

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
