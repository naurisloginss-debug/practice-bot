"""Practice betting bot.

Runs every hour (GitHub Actions). Each run does only what is due:
  1. Scan   - once a day: find value bets and place them with practice money.
  2. Close  - shortly before kick-off: record the final fair price (to measure if we beat it).
  3. Settle - after games end: mark bets won/lost and update the practice fund.
Then it writes docs/data.json, which the dashboard (docs/index.html) reads.

No real money is involved. Needs the ODDS_API_KEY environment variable.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())
BETS_FILE = ROOT / "data" / "bets.json"
STATE_FILE = ROOT / "data" / "state.json"
DASH_FILE = ROOT / "docs" / "data.json"
NOTIFY_FILE = ROOT / "notify.md"
API = "https://api.the-odds-api.com/v4"
SHARP = "pinnacle"


def now_utc():
    # BOT_NOW lets tests pretend it is a different time.
    if os.environ.get("BOT_NOW"):
        return datetime.fromisoformat(os.environ["BOT_NOW"].replace("Z", "+00:00"))
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def load(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


class Api:
    def __init__(self, key, state):
        self.key = key
        self.state = state

    def get(self, path, **params):
        params["apiKey"] = self.key
        url = f"{API}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "practice-bot"})
        with urllib.request.urlopen(req, timeout=30) as res:
            left = res.headers.get("x-requests-remaining")
            if left is not None:
                self.state["credits_left"] = int(float(left))
            return json.loads(res.read())

    def low_on_credits(self):
        left = self.state.get("credits_left")
        if left is None:
            return False
        day = now_utc().date().isoformat()
        if self.state.get("budget_day") != day:
            self.state["budget_day"], self.state["credits_day_start"] = day, left
        used_today = self.state["credits_day_start"] - left
        return left < CONFIG["keep_credits"] or used_today >= CONFIG["daily_credit_budget"]


# ---------- competitions ----------
LEAGUE_NAMES = dict(CONFIG["leagues"])
SPORTS = {"soccer": "Football", "basketball": "Basketball", "tennis": "Tennis", "icehockey": "Ice hockey",
          "americanfootball": "American football", "mma": "MMA", "boxing": "Boxing", "baseball": "Baseball",
          "rugbyleague": "Rugby", "rugbyunion": "Rugby", "aussierules": "Aussie rules", "cricket": "Cricket"}


def sport_of(key):
    return SPORTS.get(key.split("_")[0], key.split("_")[0].title())


def league_name(key):
    return LEAGUE_NAMES.get(key, key)


def competitions(api, state):
    """The fixed list from config first, then every other active competition in the chosen sport groups.
    Listing sports costs no credits."""
    keys = list(CONFIG["leagues"])
    try:
        for x in api.get("/sports"):
            if x["group"] in CONFIG["sport_groups"] and x.get("active") and not x.get("has_outrights") and x["key"] not in keys \
                    and x["key"] not in CONFIG["skip_competitions"]:
                keys.append(x["key"])
                LEAGUE_NAMES[x["key"]] = x["title"]
    except Exception:
        pass
    state["league_names"] = {k: league_name(k) for k in keys}
    return keys


# ---------- prices ----------
MARKET_NAMES = {"h2h": "Winner", "spreads": "Spread", "totals": "Total"}


def market_rows(event):
    """Each bookmaker's prices, keyed by (market, outcome name, line)."""
    rows = []
    for b in event.get("bookmakers", []):
        prices = {}
        for m in b.get("markets", []):
            if m["key"] in MARKET_NAMES:
                for o in m["outcomes"]:
                    prices[(m["key"], o["name"], o.get("point"))] = o["price"]
        rows.append({"key": b["key"], "title": b["title"], "prices": prices})
    return rows


def fair_probs(rows):
    """Pinnacle's prices with its margin removed, per market. Empty if Pinnacle has none."""
    sharp = next((r for r in rows if r["key"] == SHARP), None)
    fair = {}
    if not sharp:
        return fair
    for market in MARKET_NAMES:
        outs = {k: p for k, p in sharp["prices"].items() if k[0] == market}
        if len(outs) < 2:
            continue
        total = sum(1 / p for p in outs.values())
        for k, p in outs.items():
            fair[k] = (1 / p) / total
    return fair


def effective_odds(odds, book_key):
    """Odds after exchange commission on winnings."""
    c = CONFIG["commission"].get(book_key, 0)
    return 1 + (odds - 1) * (1 - c)


def kelly_stake(odds, prob, fund):
    b = odds - 1
    f = (b * prob - (1 - prob)) / b
    if f <= 0:
        return 0.0
    share = min(f * CONFIG["kelly_fraction"], CONFIG["max_stake_pct"] / 100)
    return round(share * fund, 2)


def pick_label(market, name, point):
    if point is None:
        return name
    return f"{name} {'+' if market == 'spreads' and point > 0 else ''}{point:g}"


# ---------- strategies ----------

def fund_of(bets, strategy):
    settled = [b for b in bets if b["strategy"] == strategy and b["status"] in ("won", "lost", "void")]
    return CONFIG["start_fund"] + sum(b["profit"] for b in settled)


def books_for(strategy, rows):
    s = CONFIG["strategies"][strategy]
    if s["books"] == "all":
        return [r for r in rows if r["key"] != SHARP and r["key"] not in CONFIG["exclude_books"]]
    return [r for r in rows if r["key"] in s["books"]]


def best_value(rows, fair, strategy, market):
    """The single best value bet in one market of a game for a strategy."""
    best = None
    for r in books_for(strategy, rows):
        for k, odds in r["prices"].items():
            if k[0] != market or k not in fair:
                continue
            eff = effective_odds(odds, r["key"])
            edge = eff * fair[k] - 1
            if CONFIG["min_edge"] <= edge <= CONFIG["max_edge"] and (best is None or edge > best["edge"]):
                best = {"market": market, "name": k[1], "point": k[2], "book": r["title"], "book_key": r["key"],
                        "odds": odds, "eff_odds": round(eff, 4), "fair_odds": round(1 / fair[k], 3),
                        "prob": fair[k], "edge": round(edge, 4)}
    return best


# ---------- jobs ----------

def odds_params(**extra):
    return dict(regions="eu", markets=",".join(CONFIG["markets"]), oddsFormat="decimal", dateFormat="iso", **extra)


def due_scan(state, now):
    """The scan slot that is due now (e.g. '2026-11-04@9'), or None."""
    hours = [h for h in CONFIG["scan_hours_utc"] if h <= now.hour]
    if not hours:
        return None
    slot = f"{now.date().isoformat()}@{max(hours)}"
    return None if state.get("last_scan") == slot else slot


def scan(api, bets, state, now, notes):
    slot = due_scan(state, now)
    if not slot:
        return
    today = now.date().isoformat()
    state["last_scan"] = slot
    if api.low_on_credits():
        notes.append("Skipped a scan: odds credits are running low.")
        return
    placed = []
    start_from = now + timedelta(hours=1)
    start_to = now + timedelta(days=CONFIG["days_ahead"])
    for league_key in competitions(api, state):
        league = league_name(league_key)
        if api.low_on_credits():
            notes.append(f"Stopped the scan at {league}: daily odds budget used up.")
            break
        try:
            events = api.get(f"/sports/{league_key}/odds", **odds_params(commenceTimeFrom=iso(start_from), commenceTimeTo=iso(start_to)))
        except Exception as e:  # one league failing should not stop the others
            notes.append(f"Could not load {league}: {e}")
            continue
        for ev in events:
            rows = market_rows(ev)
            fair = fair_probs(rows)
            if not fair:
                continue
            for strategy in CONFIG["strategies"]:
                for market in CONFIG["markets"]:
                    if any(b["event_id"] == ev["id"] and b["strategy"] == strategy and b["market"] == market for b in bets):
                        continue
                    day_bets = [b for b in bets if b["strategy"] == strategy and b["placed_at"][:10] == today]
                    room = CONFIG["max_stake_per_day"] - sum(b["stake"] for b in day_bets)
                    if len(day_bets) >= CONFIG["max_bets_per_day"] or room < CONFIG["min_stake"]:
                        continue
                    v = best_value(rows, fair, strategy, market)
                    if not v:
                        continue
                    fund = fund_of(bets, strategy)
                    if fund < CONFIG["stop_below"]:
                        continue
                    stake = min(kelly_stake(v["eff_odds"], v["prob"], fund), round(room, 2))
                    if stake < CONFIG["min_stake"]:
                        continue
                    bet = {
                        "id": f"{ev['id']}-{strategy}-{market}", "strategy": strategy, "placed_at": iso(now),
                        "league_key": league_key, "league": league, "sport": sport_of(league_key), "event_id": ev["id"],
                        "game": f"{ev['home_team']} vs {ev['away_team']}", "home": ev["home_team"], "away": ev["away_team"],
                        "start": ev["commence_time"], "market": market, "market_name": MARKET_NAMES[market],
                        "name": v["name"], "point": v["point"], "pick": pick_label(market, v["name"], v["point"]),
                        "book": v["book"], "book_key": v["book_key"],
                        "odds": v["odds"], "eff_odds": v["eff_odds"], "fair_odds": v["fair_odds"], "edge": v["edge"],
                        "stake": stake, "status": "open", "closing_fair_odds": None, "clv": None, "profit": 0.0,
                        "score": None, "settled_at": None,
                    }
                    bets.append(bet)
                    placed.append(bet)
    if placed:
        lines = [f"- {CONFIG['strategies'][b['strategy']]['name']}: **{b['pick']}** ({b['market_name']}) in {b['game']} "
                 f"({b['league']}) at {b['odds']} on {b['book']}, stake {b['stake']:.2f}, edge +{b['edge']*100:.1f}%"
                 for b in placed]
        notes.append(f"Placed {len(placed)} practice bet{'s' if len(placed) != 1 else ''}:\n" + "\n".join(lines))


def record_closing(api, bets, now, notes):
    """Fetch the final fair price for bets starting within the next window."""
    window = timedelta(minutes=CONFIG["closing_window_minutes"])
    due = [b for b in bets if b["closing_fair_odds"] is None and b["status"] == "open"
           and now <= parse(b["start"]) <= now + window]
    by_league = {}
    for b in due:
        by_league.setdefault(b["league_key"], []).append(b)
    for league_key, group in by_league.items():
        if api.low_on_credits():
            return
        try:
            events = api.get(f"/sports/{league_key}/odds", **odds_params(eventIds=",".join(sorted({b["event_id"] for b in group}))))
        except Exception as e:
            notes.append(f"Could not load closing prices for {league_name(league_key)}: {e}")
            continue
        for ev in events:
            fair = fair_probs(market_rows(ev))
            for b in group:
                k = (b["market"], b["name"], b["point"])
                if b["event_id"] == ev["id"] and k in fair:
                    b["closing_fair_odds"] = round(1 / fair[k], 3)
                    # Positive = we got a better price than the final fair price.
                    b["clv"] = round(b["eff_odds"] * fair[k] - 1, 4)
                # If the main line moved (e.g. 2.5 -> 3.0) there is no like-for-like final price; it stays empty.


def payout_multiplier(margin, eff_odds):
    """What 1 unit staked returns for a line bet. margin > 0 wins, 0 is a push.
    Quarter lines (e.g. -0.25) are half on each neighbouring line."""
    def half(m):
        return eff_odds if m > 0 else (1.0 if m == 0 else 0.0)
    if abs(margin * 4 - round(margin * 4)) < 1e-9 and round(margin * 4) % 2 != 0:
        return (half(margin - 0.25) + half(margin + 0.25)) / 2
    return half(margin)


def result_of(b, home, away):
    """Payout multiplier for a finished bet."""
    if b["market"] == "h2h":
        winner = "Draw" if home == away else (b["home"] if home > away else b["away"])
        if winner == "Draw" and b["league_key"].startswith("basketball"):
            return 1.0  # should not happen (overtime decides), treat as void
        return b["eff_odds"] if b["name"] == winner else 0.0
    if b["market"] == "totals":
        total = home + away
        margin = (total - b["point"]) if b["name"] == "Over" else (b["point"] - total)
        return payout_multiplier(margin, b["eff_odds"])
    mine, theirs = (home, away) if b["name"] == b["home"] else (away, home)
    return payout_multiplier(mine + b["point"] - theirs, b["eff_odds"])


def settle(api, bets, state, now, notes):
    waiting = [b for b in bets if b["status"] == "open" and parse(b["start"]) + timedelta(hours=CONFIG["settle_after_hours"]) <= now]
    by_league = {}
    for b in waiting:
        by_league.setdefault(b["league_key"], []).append(b)
    checked = state.setdefault("last_settle_check", {})
    results = []
    for league_key, group in by_league.items():
        last = checked.get(league_key)
        if last and parse(last) + timedelta(hours=CONFIG["settle_every_hours"]) > now:
            continue
        if league_key in state.get("no_scores", []):
            # No results feed for this sport: keep the bet for its price check, close it after 4 days.
            for b in group:
                if parse(b["start"]) + timedelta(days=4) <= now:
                    b.update(status="void", profit=0.0, settled_at=iso(now), score="no result feed")
            continue
        if api.low_on_credits():
            return
        try:
            scores = api.get(f"/sports/{league_key}/scores", daysFrom=3, dateFormat="iso",
                             eventIds=",".join(sorted({b["event_id"] for b in group})))
        except Exception as e:
            if getattr(e, "code", None) in (404, 422):
                state.setdefault("no_scores", []).append(league_key)
            else:
                notes.append(f"Could not load results for {league_name(league_key)}: {e}")
            continue
        checked[league_key] = iso(now)
        found = {s["id"]: s for s in scores}
        for b in group:
            s = found.get(b["event_id"])
            if s is None:
                # Game vanished from the results feed long after kick-off: call it void.
                if parse(b["start"]) + timedelta(days=4) <= now:
                    b.update(status="void", profit=0.0, settled_at=iso(now), score="no result found")
                continue
            if not s.get("completed") or not s.get("scores"):
                continue
            pts = {x["name"]: float(x["score"]) for x in s["scores"]}
            home, away = pts.get(b["home"]), pts.get(b["away"])
            if home is None or away is None:
                continue
            mult = result_of(b, home, away)
            profit = round(b["stake"] * (mult - 1), 2)
            b.update(status="won" if profit > 0 else "lost" if profit < 0 else "void", profit=profit,
                     score=f"{b['home']} {home:g} - {away:g} {b['away']}", settled_at=iso(now))
            results.append(b)
    if results:
        lines = [f"- {b['status'].title()} {b['profit']:+.2f}: {b['pick']} ({b['market_name']}) in {b['game']} ({b['score']})"
                 for b in results]
        notes.append("Results:\n" + "\n".join(lines))


# ---------- dashboard data ----------

def summary(bets, strategy):
    mine = [b for b in bets if b["strategy"] == strategy]
    settled = sorted([b for b in mine if b["status"] in ("won", "lost")], key=lambda b: b["settled_at"])  # void bets change nothing
    staked = sum(b["stake"] for b in settled)
    profit = sum(b["profit"] for b in settled)
    clv = [b["clv"] for b in mine if b["clv"] is not None]
    fund = CONFIG["start_fund"]
    history = [{"t": mine[0]["placed_at"] if mine else None, "fund": fund}] if mine else []
    for b in settled:
        fund += b["profit"]
        history.append({"t": b["settled_at"], "fund": round(fund, 2)})
    return {
        "name": CONFIG["strategies"][strategy]["name"],
        "about": CONFIG["strategies"][strategy]["about"],
        "fund": round(CONFIG["start_fund"] + profit, 2),
        "bets": len(mine),
        "open": sum(1 for b in mine if b["status"] == "open"),
        "won": sum(1 for b in settled if b["status"] == "won"),
        "lost": sum(1 for b in settled if b["status"] == "lost"),
        "staked": round(staked, 2),
        "profit": round(profit, 2),
        "roi": round(profit / staked, 4) if staked else None,
        "avg_edge": round(sum(b["edge"] for b in mine) / len(mine), 4) if mine else None,
        "avg_clv": round(sum(clv) / len(clv), 4) if clv else None,
        "beat_close": round(sum(1 for c in clv if c > 0) / len(clv), 4) if clv else None,
        "clv_count": len(clv),
        "history": history,
        "by_league": breakdown(mine, "league"),
        "by_market": breakdown(mine, "market_name"),
        "by_sport": breakdown(mine, "sport"),
    }


def breakdown(bets, field):
    """Per league / per bet type: how many bets, profit, and how often we beat the final price."""
    out = []
    for value in sorted({b.get(field) or "?" for b in bets}):
        group = [b for b in bets if (b.get(field) or "?") == value]
        settled = [b for b in group if b["status"] in ("won", "lost")]
        staked = sum(b["stake"] for b in settled)
        profit = sum(b["profit"] for b in settled)
        clv = [b["clv"] for b in group if b["clv"] is not None]
        out.append({"name": value, "bets": len(group), "profit": round(profit, 2),
                    "roi": round(profit / staked, 4) if staked else None,
                    "avg_clv": round(sum(clv) / len(clv), 4) if clv else None,
                    "beat_close": round(sum(1 for c in clv if c > 0) / len(clv), 4) if clv else None,
                    "clv_count": len(clv)})
    return sorted(out, key=lambda r: (r["avg_clv"] is None, -(r["avg_clv"] or 0)))


def write_dashboard(bets, state, now, notes):
    data = {
        "updated": iso(now),
        "start_fund": CONFIG["start_fund"],
        "credits_left": state.get("credits_left"),
        "last_scan": state.get("last_scan"),
        "strategies": {s: summary(bets, s) for s in CONFIG["strategies"]},
        "bets": sorted(bets, key=lambda b: b["start"], reverse=True)[:200],
        "log": (state.get("log", []) + [{"t": iso(now), "text": n} for n in notes])[-30:],
    }
    state["log"] = data["log"]
    save(DASH_FILE, data)


def main():
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key:
        sys.exit("ODDS_API_KEY is not set.")
    now = now_utc()
    bets = load(BETS_FILE, [])
    state = load(STATE_FILE, {})
    api = Api(key, state)
    LEAGUE_NAMES.update(state.get("league_names", {}))
    notes = []
    scan(api, bets, state, now, notes)
    record_closing(api, bets, now, notes)
    settle(api, bets, state, now, notes)
    save(BETS_FILE, bets)
    write_dashboard(bets, state, now, notes)
    save(STATE_FILE, state)
    if notes:
        NOTIFY_FILE.write_text("\n\n".join(notes) + "\n")
    print("\n\n".join(notes) or "Nothing due this hour.")


if __name__ == "__main__":
    main()
