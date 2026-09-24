#!/usr/bin/env python3
"""
Dodgers Hitter Prop Recap
-------------------------
Two commands:

  python dodgers_props.py pregame [--auto]
      --auto (used by the GitHub schedule) only pulls games starting within ~75 min.
      Pulls today's Dodgers hitter prop lines from The Odds API and saves them
      to data/props_YYYY-MM-DD.json. Run it 30-60 min before first pitch
      (re-running overwrites with fresher lines, so the last run = closest to close).

  python dodgers_props.py recap [--date YYYY-MM-DD] [--historical]
      Pulls the Dodgers box score from the free MLB Stats API, grades every saved
      prop (cashed / missed), and writes:
        output/recap_YYYY-MM-DD.md    table + per-hitter lines for your video
        output/recap_YYYY-MM-DD.csv   same data, spreadsheet-friendly
        output/script_prompt_YYYY-MM-DD.txt   paste into an AI to draft the voiceover
      Default date is yesterday. --historical fetches the pregame lines from The
      Odds API's historical endpoint instead (paid plans only) if you forgot to
      run `pregame`.

Setup: set your Odds API key once:
  Mac/Linux:  export ODDS_API_KEY=your_key
  Windows:    setx ODDS_API_KEY your_key
No third-party packages needed (Python 3.9+).
"""

import argparse
import csv
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

# ---------------------------------------------------------------- settings
TEAM_NAME = "Los Angeles Dodgers"
MLB_TEAM_ID = 119  # Dodgers in the MLB Stats API

# Books tried in this order; the first one that has a player's line is used.
PREFERRED_BOOKS = ["fanduel", "draftkings", "betmgm", "caesars", "espnbet"]

# Odds API market key -> (label for video, function that gets the stat from a box line)
MARKETS = {
    "batter_hits":           ("Hits",          lambda s: s["H"]),
    "batter_singles":        ("Singles",       lambda s: s["1B"]),
    "batter_total_bases":    ("Total Bases",   lambda s: s["TB"]),
    "batter_home_runs":      ("Home Runs",     lambda s: s["HR"]),
    "batter_rbis":           ("RBIs",          lambda s: s["RBI"]),
    "batter_runs_scored":    ("Runs",          lambda s: s["R"]),
    "batter_hits_runs_rbis": ("H+R+RBI",       lambda s: s["H"] + s["R"] + s["RBI"]),
}
# Each market costs 1 API credit per call (1 region). Trim this list if you
# want to stretch the free tier's monthly quota.

ODDS_BASE = "https://api.the-odds-api.com/v4"
MLB_BASE = "https://statsapi.mlb.com/api/v1"
DATA_DIR = "data"
OUT_DIR = "output"


# ---------------------------------------------------------------- helpers
def get_json(url, params=None):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "dodgers-props/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            remaining = r.headers.get("x-requests-remaining")
            if remaining is not None:
                print(f"  (Odds API credits remaining: {remaining})")
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")[:300]
        raise RuntimeError(f"HTTP {e.code} for {url.split('?')[0]}: {body}") from None
    except urllib.error.URLError as e:
        sys.exit(f"Couldn't reach {url.split('?')[0]} ({e.reason}). Check your internet connection.")


def api_key():
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        sys.exit("ODDS_API_KEY is not set. Get a free key at the-odds-api.com, then set it (see top of file).")
    return key


def norm_name(name):
    """'Teoscar Hernández Jr.' -> 'teoscar hernandez' so book names match MLB names."""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[.\-']", " ", name.lower())
    name = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", name)
    return " ".join(name.split())


def implied_prob(american):
    a = float(american)
    return (-a) / (-a + 100) if a < 0 else 100 / (a + 100)


def fmt_odds(american):
    a = int(round(float(american)))
    return f"+{a}" if a > 0 else str(a)


def utc_iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- odds
def fetch_event_odds(event_id, key, snapshot_date=None):
    """Fetch props for one event. Tries all markets at once, falls back to one-by-one
    if a market isn't recognised. snapshot_date -> historical endpoint (paid)."""
    path = (f"/historical/sports/baseball_mlb/events/{event_id}/odds" if snapshot_date
            else f"/sports/baseball_mlb/events/{event_id}/odds")
    base = {"apiKey": key, "regions": "us", "oddsFormat": "american"}
    if snapshot_date:
        base["date"] = snapshot_date

    def call(markets):
        data = get_json(ODDS_BASE + path, {**base, "markets": ",".join(markets)})
        return data.get("data", data)  # historical responses are wrapped in "data"

    try:
        return call(list(MARKETS))
    except RuntimeError as e:
        print(f"  Combined request failed ({e}); retrying market by market...")
    merged = None
    for m in MARKETS:
        try:
            ev = call([m])
        except RuntimeError as e:
            print(f"  Skipping {m}: {e}")
            continue
        if merged is None:
            merged = ev
        else:
            for bk in ev.get("bookmakers", []):
                tgt = next((b for b in merged["bookmakers"] if b["key"] == bk["key"]), None)
                if tgt:
                    tgt["markets"].extend(bk["markets"])
                else:
                    merged["bookmakers"].append(bk)
    return merged or {"bookmakers": []}


def flatten_props(event):
    """-> {player_norm: {market: {book, name, side, point, price}}} using PREFERRED_BOOKS order."""
    by_book = {b["key"]: b for b in event.get("bookmakers", [])}
    book_order = [b for b in PREFERRED_BOOKS if b in by_book] + \
                 [b for b in by_book if b not in PREFERRED_BOOKS]
    props = {}
    for book in book_order:
        for mkt in by_book[book].get("markets", []):
            if mkt["key"] not in MARKETS:
                continue
            for o in mkt.get("outcomes", []):
                player = o.get("description")
                if not player or o.get("name") != "Over":
                    continue  # keep the Over side; Under is the mirror image
                slot = props.setdefault(norm_name(player), {"display": player, "lines": {}})
                if mkt["key"] not in slot["lines"]:  # first (preferred) book wins
                    slot["lines"][mkt["key"]] = {
                        "book": book, "point": o.get("point", 0.5), "price": o["price"],
                    }
    return props


def find_dodgers_events(events):
    return [e for e in events if TEAM_NAME in (e.get("home_team"), e.get("away_team"))]


def cmd_pregame(args):
    """Pull lines for today's Dodgers game(s).
    --auto: only pull for games starting within the next WINDOW minutes that
    haven't been saved yet (for scheduled runs; the events call costs no credits)."""
    key = api_key()
    today = date.today()
    start = datetime.combine(today, datetime.min.time()).astimezone()
    params = {"apiKey": key, "commenceTimeFrom": utc_iso(start),
              "commenceTimeTo": utc_iso(start + timedelta(days=1))}
    events = find_dodgers_events(get_json(ODDS_BASE + "/sports/baseball_mlb/events", params))
    if not events:
        print(f"No Dodgers game on The Odds API for {today}. Nothing to do.")
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"props_{today}.json")
    saved = load_saved_props(today) or []
    saved_ids = {g["event_id"] for g in saved}
    now = datetime.now(timezone.utc)
    changed = False

    for ev in sorted(events, key=lambda e: e["commence_time"]):
        first_pitch = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        mins = (first_pitch - now).total_seconds() / 60
        label = f"{ev['away_team']} @ {ev['home_team']} (first pitch in {mins:.0f} min)"
        if args.auto:
            if ev["id"] in saved_ids:
                print(f"Already saved: {label}")
                continue
            if not (0 <= mins <= args.window):
                print(f"Not in window yet: {label}")
                continue
        print(f"Pulling props: {label}")
        props = flatten_props(fetch_event_odds(ev["id"], key))
        print(f"  Found lines for {len(props)} players.")
        entry = {"event_id": ev["id"], "commence_time": ev["commence_time"],
                 "home_team": ev["home_team"], "away_team": ev["away_team"],
                 "pulled_at": utc_iso(now), "props": props}
        saved = [g for g in saved if g["event_id"] != ev["id"]] + [entry]
        changed = True

    if changed:
        saved.sort(key=lambda g: g["commence_time"])
        with open(path, "w") as f:
            json.dump(saved, f, indent=2)
        print(f"Saved -> {path}")


# ---------------------------------------------------------------- box score
def dodgers_games(game_date):
    sched = get_json(f"{MLB_BASE}/schedule",
                     {"sportId": 1, "teamId": MLB_TEAM_ID, "date": game_date.isoformat()})
    games = [g for d in sched.get("dates", []) for g in d.get("games", [])]
    return [g for g in games if g["status"]["abstractGameState"] == "Final"]


def dodgers_batting(game_pk):
    box = get_json(f"{MLB_BASE}/game/{game_pk}/boxscore")
    side = "home" if box["teams"]["home"]["team"]["id"] == MLB_TEAM_ID else "away"
    opp = box["teams"]["away" if side == "home" else "home"]["team"]["name"]
    team = box["teams"][side]
    lines = []
    for pid in team.get("batters", []):
        p = team["players"].get(f"ID{pid}")
        if not p:
            continue
        b = p.get("stats", {}).get("batting", {})
        if not b or b.get("plateAppearances", b.get("atBats", 0) + b.get("baseOnBalls", 0)) == 0:
            continue
        h, d2, d3, hr = b.get("hits", 0), b.get("doubles", 0), b.get("triples", 0), b.get("homeRuns", 0)
        singles = h - d2 - d3 - hr
        lines.append({
            "name": p["person"]["fullName"],
            "order": p.get("battingOrder", "999"),
            "AB": b.get("atBats", 0), "H": h, "1B": singles, "2B": d2, "3B": d3, "HR": hr,
            "R": b.get("runs", 0), "RBI": b.get("rbi", 0), "BB": b.get("baseOnBalls", 0),
            "SO": b.get("strikeOuts", 0), "SB": b.get("stolenBases", 0),
            "TB": singles + 2 * d2 + 3 * d3 + 4 * hr,
        })
    lines.sort(key=lambda x: int(x["order"]))
    return opp, "vs" if side == "home" else "@", lines


def stat_line(s):
    extras = []
    for k, label in [("2B", "2B"), ("3B", "3B"), ("HR", "HR"), ("RBI", "RBI"),
                     ("R", "R"), ("BB", "BB"), ("SB", "SB")]:
        if s[k]:
            extras.append(f"{s[k]} {label}" if s[k] > 1 else label)
    return f"{s['H']}-for-{s['AB']}" + (f", {', '.join(extras)}" if extras else "")


# ---------------------------------------------------------------- grading
def grade(batting, props):
    rows = []
    for s in batting:
        entry = props.get(norm_name(s["name"]))
        if not entry:
            rows.append({**s, "props": []})
            continue
        graded = []
        for mkey, line in entry["lines"].items():
            label, getter = MARKETS[mkey]
            actual = getter(s)
            graded.append({
                "market": label, "point": line["point"], "price": line["price"],
                "book": line["book"], "implied": implied_prob(line["price"]),
                "actual": actual, "cashed": actual > float(line["point"]),
            })
        graded.sort(key=lambda g: list(m[0] for m in MARKETS.values()).index(g["market"]))
        rows.append({**s, "props": graded})
    return rows


def prop_label(g):
    need = int(float(g["point"])) + 1  # Over 0.5 -> 1+, Over 1.5 -> 2+
    return f"{need}+ {g['market']}"


def write_outputs(game_date, games):
    os.makedirs(OUT_DIR, exist_ok=True)
    md, csv_rows, prompt_facts = [], [], []
    md.append(f"# Dodgers Hitter Prop Recap — {game_date:%A, %B} {game_date.day}, {game_date.year}")
    for g in games:
        md.append(f"\n## Dodgers {g['venue_word']} {g['opp']}\n")
        cashed = sum(p["cashed"] for r in g["rows"] for p in r["props"])
        total = sum(len(r["props"]) for r in g["rows"])
        if total:
            md.append(f"**Props graded:** {total} · **Cashed (Over):** {cashed} · **Missed:** {total - cashed}\n")
        for r in g["rows"]:
            md.append(f"### {r['name']} — {stat_line(r)}")
            fact = f"{r['name']}: {stat_line(r)}."
            if r["props"]:
                md.append("\n| Prop | Line | Implied | Result |\n|---|---|---|---|")
                bits = []
                for p in r["props"]:
                    res = "✅ Cashed" if p["cashed"] else "❌ Missed"
                    md.append(f"| {prop_label(p)} | {fmt_odds(p['price'])} ({p['book']}) | "
                              f"{p['implied']:.0%} | {res} (actual {p['actual']}) |")
                    bits.append(f"{prop_label(p)} at {fmt_odds(p['price'])} "
                                f"({p['implied']:.0%} implied) {'CASHED' if p['cashed'] else 'missed'}")
                    csv_rows.append({"date": game_date.isoformat(), "opponent": g["opp"],
                                     "player": r["name"], "stat_line": stat_line(r),
                                     "prop": prop_label(p), "odds": fmt_odds(p["price"]),
                                     "book": p["book"], "implied_prob": round(p["implied"], 3),
                                     "actual": p["actual"], "result": "cashed" if p["cashed"] else "missed"})
                fact += " Props: " + "; ".join(bits) + "."
            else:
                md.append("\n_No prop lines saved for this player._")
            md.append("")
            prompt_facts.append(fact)

    base = os.path.join(OUT_DIR, f"recap_{game_date}")
    with open(base + ".md", "w") as f:
        f.write("\n".join(md) + "\n")
    if csv_rows:
        with open(base + ".csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(csv_rows[0]))
            w.writeheader()
            w.writerows(csv_rows)

    opp_text = ", ".join(f"{g['venue_word']} {g['opp']}" for g in games)
    prompt = f"""Write a 60-90 second voiceover script for a short-form video recapping the
Los Angeles Dodgers hitters from {game_date:%B} {game_date.day} ({opp_text}).

Rules:
- Use ONLY the facts below. Do not invent stats, injuries, or context.
- Lead with the biggest story (best performance or most surprising prop result).
- For each prop mentioned, say the odds and what the implied probability means in plain words.
- Plus-money props that cashed and heavy favorites that missed are the most interesting — prioritize them.
- Frame as a recap of what happened, not betting advice or tomorrow's picks.
- Conversational, punchy, no filler. End with a one-line hook to follow for tomorrow's recap.

Facts:
""" + "\n".join(f"- {x}" for x in prompt_facts)
    with open(os.path.join(OUT_DIR, f"script_prompt_{game_date}.txt"), "w") as f:
        f.write(prompt + "\n")
    print(f"Wrote {base}.md, {base}.csv and script_prompt_{game_date}.txt in {OUT_DIR}/")


def load_saved_props(game_date):
    path = os.path.join(DATA_DIR, f"props_{game_date}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def historical_props(game):
    """Paid plans only: snapshot 30 min before first pitch."""
    key = api_key()
    first_pitch = datetime.fromisoformat(game["gameDate"].replace("Z", "+00:00"))
    snap = utc_iso(first_pitch - timedelta(minutes=30))
    evs = get_json(ODDS_BASE + "/historical/sports/baseball_mlb/events", {"apiKey": key, "date": snap})
    evs = find_dodgers_events(evs.get("data", []))
    if not evs:
        return {}
    ev = min(evs, key=lambda e: abs(datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")) - first_pitch))
    return flatten_props(fetch_event_odds(ev["id"], key, snapshot_date=snap))


def cmd_recap(args):
    game_date = date.fromisoformat(args.date) if args.date else date.today() - timedelta(days=1)
    games = dodgers_games(game_date)
    if not games:
        print(f"No completed Dodgers game on {game_date}. Nothing to recap.")
        return
    saved = load_saved_props(game_date)
    if saved is None and not args.historical:
        print(f"No saved props for {game_date} (run `pregame` before games, or use --historical). "
              "Writing stat-only recap.")

    out = []
    for i, game in enumerate(games):
        opp, venue_word, batting = dodgers_batting(game["gamePk"])
        if args.historical:
            props = historical_props(game)
        elif saved:
            # doubleheaders: match saved events by start time order
            props = saved[min(i, len(saved) - 1)]["props"]
        else:
            props = {}
        rows = grade(batting, props)
        unmatched = [p["display"] for k, p in props.items()
                     if k not in {norm_name(b["name"]) for b in batting}]
        if unmatched:
            print(f"  Note: lines saved but player didn't bat / name mismatch: {', '.join(unmatched)}")
        out.append({"opp": opp, "venue_word": venue_word, "rows": rows})
    write_outputs(game_date, out)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Dodgers hitter prop recap")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pg = sub.add_parser("pregame", help="save today's Dodgers hitter prop lines")
    pg.add_argument("--auto", action="store_true",
                    help="only pull games starting soon that aren't saved yet (for schedules)")
    pg.add_argument("--window", type=int, default=75,
                    help="minutes before first pitch that --auto will pull (default 75)")
    r = sub.add_parser("recap", help="grade props against the box score")
    r.add_argument("--date", help="YYYY-MM-DD (default: yesterday)")
    r.add_argument("--historical", action="store_true",
                   help="pull pregame lines from the historical endpoint (paid plans)")
    args = ap.parse_args()
    {"pregame": cmd_pregame, "recap": cmd_recap}[args.cmd](args)


if __name__ == "__main__":
    main()
