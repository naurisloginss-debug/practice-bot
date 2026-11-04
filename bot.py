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


def P(strategy, key):
    """A character's own setting, or the shared default."""
    return CONFIG["strategies"][strategy].get(key, CONFIG.get(key))


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
    keys = []
    for s in CONFIG["strategies"].values():
        keys += [k for k in s["leagues"] if k not in keys]
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


def kelly_stake(odds, prob, fund, strategy, boost=1.0):
    """Kelly stake scaled by the character's risk level (and what it has learned), capped per bet."""
    b = odds - 1
    f = (b * prob - (1 - prob)) / b
    if f <= 0:
        return 0.0
    share = min(f * P(strategy, "kelly_fraction") * boost, P(strategy, "max_stake_pct") / 100)
    return round(share * fund, 2)


def pick_label(market, name, point):
    if point is None:
        return name
    return f"{name} {'+' if market == 'spreads' and point > 0 else ''}{point:g}"


# ---------- strategies ----------

def fund_of(bets, strategy):
    settled = [b for b in bets if b["strategy"] == strategy and b["status"] in ("won", "lost", "void")]
    return P(strategy, "start_fund") + sum(b["profit"] for b in settled)


def books_for(strategy, rows):
    s = CONFIG["strategies"][strategy]
    if s["books"] == "all":
        return [r for r in rows if r["key"] != SHARP and r["key"] not in CONFIG["exclude_books"]]
    return [r for r in rows if r["key"] in s["books"]]


def best_value(rows, fair, strategy, market, league_key, playbook):
    """The best value bet in one market of a game for a character, within its style and lessons.
    Returns (bet or None, near_miss or None). A near miss is the most tempting price it turned down, with the reason."""
    best, miss = None, None
    lo, hi = P(strategy, "min_odds"), P(strategy, "max_odds")
    min_edge, max_edge = P(strategy, "min_edge"), P(strategy, "max_edge")
    for r in books_for(strategy, rows):
        for k, odds in r["prices"].items():
            if k[0] != market or k not in fair:
                continue
            eff = effective_odds(odds, r["key"])
            edge = eff * fair[k] - 1
            option = {"market": market, "name": k[1], "point": k[2], "book": r["title"], "book_key": r["key"],
                      "odds": odds, "eff_odds": round(eff, 4), "fair_odds": round(1 / fair[k], 3),
                      "prob": fair[k], "edge": round(edge, 4)}
            reason = None
            if edge > max_edge:
                reason = f"+{edge*100:.1f}% looks too good to be true. Probably a price the bookmaker forgot to update."
            elif not lo <= odds <= hi:
                reason = f"Odds {odds:.2f} are outside my game ({lo}–{hi})."
            elif verdict_for(playbook, league_key, market, odds) == "avoid":
                reason = f"I've learned to stay away from {league_name(league_key)} {MARKET_NAMES[market].lower()} at these odds."
            elif edge < min_edge:
                if edge <= 0:
                    continue
                reason = f"Only +{edge*100:.1f}% edge. I need +{min_edge*100:.1f}% before I move."
            if reason:
                if miss is None or edge > miss["edge"]:
                    miss = {**option, "reason": reason}
            elif best is None or edge > best["edge"]:
                best = option
    return best, miss


def thoughts_for(strategy, v, league_key, fund, stake, favoured, level):
    """The character's reasoning for a bet, step by step, in plain words."""
    c = CONFIG["strategies"][strategy]
    pick = pick_label(v["market"], v["name"], v["point"])
    lines = [
        f"Pinnacle, the sharpest bookmaker, rates {pick} at {v['prob']*100:.1f}%. That makes the fair price {v['fair_odds']:.2f}.",
        f"{v['book']} pays {v['odds']:.2f}" + (f" ({v['eff_odds']:.2f} after the exchange fee)" if v["eff_odds"] != v["odds"] else "") +
        f". That is +{v['edge']*100:.1f}% above fair. My bar is +{P(strategy, 'min_edge')*100:.1f}%.",
    ]
    if favoured:
        lines.append(f"My record on {league_name(league_key)} {MARKET_NAMES[v['market']].lower()} bets beats the market, so I'm pressing a little harder here.")
    if level["stake"] != 1.0:
        lines.append(f"I'm {level['badge']} {level['title']}: stakes ×{level['stake']:g}.")
    b = v["eff_odds"] - 1
    full = max(0.0, (b * v["prob"] - (1 - v["prob"])) / b)
    lines.append(f"Full Kelly says {full*100:.1f}% of my fund. I play {P(strategy, 'kelly_fraction'):g}× Kelly, capped at {P(strategy, 'max_stake_pct'):g}%: "
                 f"{stake:.2f} of my {fund:.2f}.")
    lines.append(c.get("voice", {}).get("strike", "Decision: bet."))
    return lines


# ---------- learning ----------
# Each character studies its own bets. It learns from the price check (did we beat the market's
# final price?), not from wins and losses, which are mostly luck over a few dozen bets.

def odds_band(odds):
    return "favourites" if odds < 2.0 else "middle odds" if odds < 3.5 else "long shots"


def segment(league_key, market, odds=None):
    """Learning groups: league + bet type + odds range, and the broader league + bet type."""
    return f"{league_key}|{market}|{odds_band(odds) if odds is not None else '*'}"


def verdict_for(playbook, league_key, market, odds):
    """'avoid' wins over 'favour'; the narrow group is checked before the broad one."""
    found = [playbook.get(segment(league_key, market, odds)), playbook.get(segment(league_key, market))]
    return "avoid" if "avoid" in found else "favour" if "favour" in found else None


def learn(bets, strategy):
    """Returns (playbook, lessons). playbook: group -> 'avoid' | 'favour'."""
    L = CONFIG["learning"]
    groups = {}
    for b in bets:
        if b["strategy"] == strategy and b.get("clv") is not None:
            for key in (segment(b["league_key"], b["market"], b["odds"]), segment(b["league_key"], b["market"])):
                groups.setdefault(key, []).append(b)
    playbook, lessons = {}, []
    for seg, group in sorted(groups.items()):
        if len(group) < L["min_bets"]:
            continue
        clv = sum(b["clv"] for b in group) / len(group)
        beat = sum(1 for b in group if b["clv"] > 0) / len(group)
        league_key, market, band = seg.split("|")
        label = f"{league_name(league_key)} · {MARKET_NAMES[market].lower()}" + ("" if band == "*" else f" · {band}")
        if clv <= L["avoid_below"]:
            playbook[seg] = "avoid"
            lessons.append({"seg": seg, "kind": "avoid", "text": f"Stopped: {label}", "detail": f"{clv*100:+.1f}% vs the market over {len(group)} bets"})
        elif clv >= L["favour_above"] and beat >= L["favour_beat"]:
            playbook[seg] = "favour"
            lessons.append({"seg": seg, "kind": "favour", "text": f"Backing harder: {label}", "detail": f"{clv*100:+.1f}% vs the market, beat it on {beat*100:.0f}% of {len(group)} bets"})
    # A narrow lesson that says the same as its broad group adds nothing on screen.
    lessons = [l for l in lessons if l["seg"].endswith("|*") or playbook.get(l["seg"].rsplit("|", 1)[0] + "|*") != playbook[l["seg"]]]
    for l in lessons:
        del l["seg"]
    return playbook, sorted(lessons, key=lambda x: x["kind"])


# ---------- levels ----------
# A character earns its level by beating the market's final price, measured over many bets.
# Higher levels unlock slightly bolder stakes (still under the hard per-bet cap). A cold streak cuts them.
LEVELS = [
    {"n": 1, "title": "Rookie", "badge": "🐣", "bets": 0, "clv": None, "beat": None, "stake": 1.0, "perk": "Normal stakes while it proves itself."},
    {"n": 2, "title": "Apprentice", "badge": "🎓", "bets": 15, "clv": 0.0, "beat": 0.5, "stake": 1.0, "perk": "Has a track record. Starts learning which bets work."},
    {"n": 3, "title": "Sharp", "badge": "⚡", "bets": 40, "clv": 0.01, "beat": 0.55, "stake": 1.15, "perk": "Stakes +15%. Consistently beats the market."},
    {"n": 4, "title": "Mastermind", "badge": "👑", "bets": 100, "clv": 0.015, "beat": 0.57, "stake": 1.3, "perk": "Stakes +30%. Proven edge over 100+ bets."},
]
COLD = {"title": "Cold streak", "badge": "🥶", "stake": 0.6, "perk": "Stakes cut to 60% until its prices beat the market again."}


def level_of(bets, strategy):
    measured = [b["clv"] for b in bets if b["strategy"] == strategy and b.get("clv") is not None]
    n = len(measured)
    clv = sum(measured) / n if n else 0.0
    beat = sum(1 for c in measured if c > 0) / n if n else 0.0
    reached = LEVELS[0]
    for lv in LEVELS[1:]:
        if n >= lv["bets"] and clv >= lv["clv"] and beat >= lv["beat"]:
            reached = lv
    out = {k: reached[k] for k in ("n", "title", "badge", "stake", "perk")}
    out.update(measured=n, clv=round(clv, 4), beat=round(beat, 4), cold=False)
    if n >= 30 and clv < 0:
        out.update(cold=True, title=COLD["title"], badge=COLD["badge"], stake=COLD["stake"], perk=COLD["perk"])
    nxt = next((lv for lv in LEVELS if lv["n"] == reached["n"] + 1), None)
    if nxt:
        needs = [{"text": f"{nxt['bets']} measured bets", "done": n >= nxt["bets"], "now": f"{n}"},
                 {"text": f"beat the market on {nxt['beat']*100:.0f}%+", "done": n > 0 and beat >= nxt["beat"], "now": f"{beat*100:.0f}%" if n else "–"},
                 {"text": f"average price {nxt['clv']*100:+.1f}%+ vs market", "done": n > 0 and clv >= nxt["clv"], "now": f"{clv*100:+.1f}%" if n else "–"}]
        out["next"] = {"title": nxt["title"], "badge": nxt["badge"], "perk": nxt["perk"], "needs": needs,
                       "progress": round(min(1.0, n / nxt["bets"]) if nxt["bets"] else 1.0, 3)}
    return out


# ---------- jobs ----------

def odds_params(markets, **extra):
    return dict(regions="eu", markets=",".join(sorted(markets)), oddsFormat="decimal", dateFormat="iso", **extra)


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
    playbooks = {s: learn(bets, s)[0] for s in CONFIG["strategies"]}
    levels = {s: level_of(bets, s) for s in CONFIG["strategies"]}
    looked = {s: set() for s in CONFIG["strategies"]}
    misses = {s: [] for s in CONFIG["strategies"]}
    limited, stopped = {}, {}
    for league_key in competitions(api, state):
        league = league_name(league_key)
        chars = [s for s, c in CONFIG["strategies"].items() if league_key in c["leagues"]]
        markets = {m for s in chars for m in P(s, "markets")}
        start_to = now + timedelta(days=max(P(s, "days_ahead") for s in chars))
        if api.low_on_credits():
            notes.append(f"Stopped the scan at {league}: daily odds budget used up.")
            break
        try:
            events = api.get(f"/sports/{league_key}/odds", **odds_params(markets, commenceTimeFrom=iso(start_from), commenceTimeTo=iso(start_to)))
        except Exception as e:  # one league failing should not stop the others
            notes.append(f"Could not load {league}: {e}")
            continue
        for ev in events:
            rows = market_rows(ev)
            fair = fair_probs(rows)
            if not fair:
                continue
            for strategy in chars:
                if parse(ev["commence_time"]) > now + timedelta(days=P(strategy, "days_ahead")):
                    continue
                for market in P(strategy, "markets"):
                    if any(b["event_id"] == ev["id"] and b["strategy"] == strategy and b["market"] == market for b in bets):
                        continue
                    day_bets = [b for b in bets if b["strategy"] == strategy and b["placed_at"][:10] == today]
                    room = P(strategy, "max_stake_per_day") - sum(b["stake"] for b in day_bets)
                    if len(day_bets) >= P(strategy, "max_bets_per_day") or room < CONFIG["min_stake"]:
                        limited[strategy] = True
                        continue
                    v, miss = best_value(rows, fair, strategy, market, league_key, playbooks[strategy])
                    looked[strategy].add(ev["id"])
                    if miss:
                        misses[strategy].append({**miss, "game": f"{ev['home_team']} vs {ev['away_team']}", "league": league,
                                                 "pick": pick_label(market, miss["name"], miss["point"])})
                    if not v:
                        continue
                    fund = fund_of(bets, strategy)
                    if fund < P(strategy, "stop_below"):
                        stopped[strategy] = True
                        continue
                    favoured = verdict_for(playbooks[strategy], league_key, market, v["odds"]) == "favour"
                    boost = (CONFIG["learning"]["favour_boost"] if favoured else 1.0) * levels[strategy]["stake"]
                    stake = min(kelly_stake(v["eff_odds"], v["prob"], fund, strategy, boost), round(room, 2))
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
                        "thoughts": thoughts_for(strategy, v, league_key, fund, stake, favoured, levels[strategy]),
                    }
                    bets.append(bet)
                    placed.append(bet)
    # Each character's diary entry for this scan: what it looked at, what it did, what it passed on.
    diary = state.setdefault("diary", {})
    for strategy, c in CONFIG["strategies"].items():
        mine = [b for b in placed if b["strategy"] == strategy]
        voice = c.get("voice", {})
        if stopped.get(strategy):
            mood = voice.get("stopped", "I've hit my loss limit. Standing down until you reset me.")
        elif mine:
            mood = voice.get("strike", "Found value. Striking.")
        elif limited.get(strategy):
            mood = voice.get("limit", "Daily limit reached. Discipline first.")
        else:
            mood = voice.get("wait", "Nothing worth the risk today.")
        top = sorted(misses[strategy], key=lambda m: -m["edge"])[:4]
        diary[strategy] = {"t": iso(now), "mood": mood, "looked": len(looked[strategy]), "placed": len(mine),
                           "passed": [{k: m[k] for k in ("game", "league", "pick", "odds", "book", "edge", "reason")} for m in top]}
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
            events = api.get(f"/sports/{league_key}/odds", **odds_params({b["market"] for b in group}, eventIds=",".join(sorted({b["event_id"] for b in group}))))
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
    start = P(strategy, "start_fund")
    fund = start
    history = [{"t": mine[0]["placed_at"] if mine else None, "fund": fund}] if mine else []
    for b in settled:
        fund += b["profit"]
        history.append({"t": b["settled_at"], "fund": round(fund, 2)})
    return {
        **profile(strategy),
        "start_fund": start,
        "fund": round(start + profit, 2),
        "lessons": learn(bets, strategy)[1],
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


def profile(strategy):
    c = CONFIG["strategies"][strategy]
    return {k: c.get(k) for k in ("name", "emoji", "sport", "inspired_by", "story", "style", "motto", "backtest")} | {
        "risk": {"kelly": P(strategy, "kelly_fraction"), "max_stake_pct": P(strategy, "max_stake_pct"),
                 "min_edge": P(strategy, "min_edge"), "odds": [P(strategy, "min_odds"), P(strategy, "max_odds")],
                 "stop_below": P(strategy, "stop_below")},
        "leagues": [league_name(k) for k in c["leagues"]],
        "markets": [MARKET_NAMES[m] for m in P(strategy, "markets")],
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
        "order": list(CONFIG["strategies"]),
        "credits_left": state.get("credits_left"),
        "last_scan": state.get("last_scan"),
        "strategies": {s: summary(bets, s) | {"diary": state.get("diary", {}).get(s), "level": level_of(bets, s)} for s in CONFIG["strategies"]},
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
    for b in bets:  # bets made under an older name keep their history
        b["strategy"] = CONFIG.get("renamed", {}).get(b["strategy"], b["strategy"])
    bets[:] = [b for b in bets if b["strategy"] in CONFIG["strategies"]]
    notes = []
    scan(api, bets, state, now, notes)
    record_closing(api, bets, now, notes)
    settle(api, bets, state, now, notes)
    for s, c in CONFIG["strategies"].items():  # announce level changes
        lv = level_of(bets, s)
        before = state.setdefault("levels", {}).get(s)
        now_title = lv["title"]
        if before and before != now_title:
            up = lv["n"] > next((x["n"] for x in LEVELS if x["title"] == before), 0) and not lv["cold"]
            notes.append(f"{c['emoji']} {c['name']} {'levelled up' if up else 'changed level'}: {before} → {lv['badge']} {now_title}. {lv['perk']}")
        state["levels"][s] = now_title
    save(BETS_FILE, bets)
    write_dashboard(bets, state, now, notes)
    save(STATE_FILE, state)
    if notes:
        NOTIFY_FILE.write_text("\n\n".join(notes) + "\n")
    print("\n\n".join(notes) or "Nothing due this hour.")


if __name__ == "__main__":
    main()
