"""Practice betting bot.

Runs every hour (GitHub Actions). Each run does only what is due:
  1. Scan   - once a day: find value bets and place them with practice money.
  2. Close  - shortly before kick-off: record the final fair price (to measure if we beat it).
  3. Settle - after games end: mark bets won/lost and update the practice fund.
Then it writes docs/data.json, which the dashboard (docs/index.html) reads.

No real money is involved. Needs the ODDS_API_KEY environment variable.
"""
import json
import math
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
OBS_FILE = ROOT / "data" / "observations.json"
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


def thoughts_for(strategy, v, league_key, fund, stake, favoured, level, nudges=()):
    """The character's reasoning for a bet, step by step, in plain words."""
    c = CONFIG["strategies"][strategy]
    pick = pick_label(v["market"], v["name"], v["point"])
    lines = [
        f"Pinnacle, the sharpest bookmaker, rates {pick} at {v['prob']*100:.1f}%. That makes the fair price {v['fair_odds']:.2f}."
        if not nudges else
        f"Pinnacle's price, adjusted with what I know about {league_name(league_key)} ({', '.join(nudges)}), gives {pick} {v['prob']*100:.1f}%: fair price {v['fair_odds']:.2f}.",
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


# ---------- knowledge ----------
# Each character studies every game it scans (not only its bets): the fair chances the market gave
# and, later, what actually happened. Over time it finds where the market is biased in its sport.
# A finding it is very sure about becomes a rule that nudges its fair price by half the gap.

def observe(obs, knowledge, ev, league_key, rows, fair, now):
    if ev["id"] not in obs:
        h2h = {}
        for (m, name, point), p in fair.items():
            if m == "h2h":
                role = "home" if name == ev["home_team"] else "away" if name == ev["away_team"] else "draw"
                h2h[role] = round(p, 4)
        over = [(point, p) for (m, name, point), p in fair.items() if m == "totals" and name == "Over"]
        obs[ev["id"]] = {"league": league_key, "start": ev["commence_time"], "home": ev["home_team"], "away": ev["away_team"],
                         "h2h": h2h or None, "tot": {"point": over[0][0], "over": round(over[0][1], 4)} if over else None, "result": None}
    books = knowledge.setdefault("books", {}).setdefault(league_key, {})
    for r in rows:
        if r["key"] == SHARP:
            continue
        n_soft = [0, 0]
        for k, price in r["prices"].items():
            if k in fair:
                n_soft[0] += 1
                n_soft[1] += effective_odds(price, r["key"]) * fair[k] - 1 >= 0.02
        if n_soft[0]:
            have = books.setdefault(r["title"], [0, 0])
            have[0] += n_soft[0]
            have[1] += n_soft[1]


ROLE_NAMES = {"home": "home wins", "draw": "draws", "away": "away wins", "over": "overs"}


def bias_stats(obs, league_key):
    """Per outcome type: how often it happened vs how often the market priced it."""
    done = [o for o in obs.values() if o["league"] == league_key and o.get("result")]
    out = {}
    for role in ("home", "draw", "away"):
        g = [o for o in done if o["h2h"] and role in o["h2h"]]
        if g:
            hit = [(1.0 if (role == "home" and o["result"][0] > o["result"][1]) or (role == "away" and o["result"][1] > o["result"][0])
                    or (role == "draw" and o["result"][0] == o["result"][1]) else 0.0) for o in g]
            out[role] = (len(g), sum(o["h2h"][role] for o in g) / len(g), sum(hit) / len(g))
    g = [o for o in done if o.get("tot") and sum(o["result"]) != o["tot"]["point"]]
    if g:
        out["over"] = (len(g), sum(o["tot"]["over"] for o in g) / len(g), sum(1.0 for o in g if sum(o["result"]) > o["tot"]["point"]) / len(g))
    return out


def knowledge_of(obs, knowledge, bets, strategy):
    """Facts the character has discovered about its sport, the rules it now uses, and its Sport IQ."""
    K = CONFIG["knowledge"]
    leagues = CONFIG["strategies"][strategy]["leagues"]
    facts, rules = [], {}
    for lk in leagues:
        for role, (n, priced, actual) in bias_stats(obs, lk).items():
            if n < K["min_games"]:
                continue
            se = math.sqrt(max(priced * (1 - priced), 1e-6) / n)
            z = (actual - priced) / se
            gap = actual - priced
            if n >= K["rule_games"] and abs(z) >= K["rule_z"]:
                conf = "using"
                rules[f"{lk}|{role}"] = round(gap / 2, 4)
            elif n >= K["likely_games"] and abs(z) >= 2:
                conf = "likely"
            elif abs(gap) >= 0.02 and abs(z) >= 1.5:
                conf = "watching"
            elif n >= K["likely_games"] and abs(z) < 1:
                facts.append({"conf": "solid", "n": n, "z": 0, "text": f"The market prices {ROLE_NAMES[role]} in {league_name(lk)} accurately.",
                              "detail": f"{actual*100:.0f}% happened, {priced*100:.0f}% priced, {n} games"})
                continue
            else:
                continue
            word = "more" if gap > 0 else "less"
            facts.append({"conf": conf, "n": n, "z": abs(z),
                          "text": f"{ROLE_NAMES[role].capitalize()} in {league_name(lk)} happen {word} often than priced.",
                          "detail": f"{actual*100:.0f}% happened vs {priced*100:.0f}% priced, {n} games"})
        books = knowledge.get("books", {}).get(lk, {})
        rated = {b: v for b, v in books.items() if v[0] >= K["min_prices"]}
        if len(rated) >= 2:
            avg = sum(v[1] for v in rated.values()) / sum(v[0] for v in rated.values())
            best, v = max(rated.items(), key=lambda kv: kv[1][1] / kv[1][0])
            if v[1] / v[0] > avg * 1.3:
                facts.append({"conf": "solid", "n": v[0], "z": 0, "text": f"{best} is the softest bookmaker in {league_name(lk)}.",
                              "detail": f"beats fair by 2%+ on {v[1]/v[0]*100:.0f}% of its prices vs {avg*100:.0f}% across all bookmakers"})
    mine = [b for b in bets if b["strategy"] == strategy and b.get("clv") is not None]
    if len(mine) >= 10:
        moved = sum(1 for b in mine if b["clv"] > 0) / len(mine)
        facts.append({"conf": "solid" if len(mine) >= 30 else "watching", "n": len(mine), "z": 0,
                      "text": f"When I find value, the market moves my way before kick-off {moved*100:.0f}% of the time.",
                      "detail": f"{len(mine)} bets measured"})
    order = {"using": 0, "likely": 1, "solid": 2, "watching": 3}
    facts.sort(key=lambda f: (order[f["conf"]], -f["z"], -f["n"]))
    studied = [o for o in obs.values() if o["league"] in leagues]
    resolved = [o for o in studied if o.get("result")]
    lessons = learn(bets, strategy)[1]
    # Sport IQ: grows with games studied, bets measured and knowledge proven; good or bad pricing moves it too.
    iq = 80 + 6 * math.log2(1 + len(resolved) / 50) + 3 * math.log2(1 + len(studied) / 200) + 5 * math.log2(1 + len(mine) / 20) \
        + 4 * sum(1 for f in facts if f["conf"] == "using") + min(10, sum(1 for f in facts if f["conf"] in ("likely", "solid"))) + 1.5 * len(lessons)
    if len(mine) >= 20:
        iq += max(-8, min(8, sum(b["clv"] for b in mine) / len(mine) * 300))
    return {"facts": facts[:12], "rules": rules, "iq": round(min(iq, 180)), "studied": len(studied), "resolved": len(resolved)}


def prune_obs(obs, now):
    """Keep finished games for a year and unfinished ones for a week after kick-off."""
    keep = {}
    for k, o in obs.items():
        age = now - parse(o["start"])
        if age < timedelta(days=365 if o.get("result") else 7):
            keep[k] = o
    return keep


def adjust_fair(fair, ev, league_key, rules):
    """Nudge the market's fair chances using rules the character is sure about. Returns (fair, notes)."""
    if not rules:
        return fair, []
    out, notes = dict(fair), []
    role_of = lambda name: "home" if name == ev["home_team"] else "away" if name == ev["away_team"] else "draw"
    h2h = [k for k in fair if k[0] == "h2h"]
    for k in h2h:
        d = rules.get(f"{league_key}|{role_of(k[1])}")
        if d:
            out[k] = min(0.97, max(0.02, fair[k] + d))
            notes.append(f"{ROLE_NAMES[role_of(k[1])]} {d*100:+.1f}%")
    if h2h and notes:
        total = sum(out[k] for k in h2h)
        for k in h2h:
            out[k] /= total
    d = rules.get(f"{league_key}|over")
    if d:
        for k in fair:
            if k[0] == "totals":
                out[k] = min(0.97, max(0.03, fair[k] + (d if k[1] == "Over" else -d)))
        notes.append(f"overs {d*100:+.1f}%")
    return out, notes


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


# ---------- competition ----------
# The characters compete on skill (Sport IQ, beating the market, discoveries), never on profit or
# how much they bet, so wanting to win never pushes them into reckless bets.

ACHIEVEMENTS = [
    ("first_blood", "🩸", "First Blood", "Placed its first bet."),
    ("beat_it", "🎯", "Beat the Market", "Got a better price than the final one."),
    ("ten_measured", "📏", "Measured Ten", "10 bets checked against the final price."),
    ("on_fire", "🔥", "On Fire", "Beat the market 5 bets in a row."),
    ("scholar", "📚", "Scholar", "Studied 100 games."),
    ("pattern", "🔍", "Pattern Hunter", "Found its first 'Likely' pattern in its sport."),
    ("rule", "⚙️", "Rule Maker", "Turned a finding into a betting rule."),
    ("lesson", "🧪", "Self-Critic", "Cut or backed a kind of bet based on its own record."),
    ("iq100", "🧠", "IQ 100", "Reached Sport IQ 100."),
    ("iq120", "💡", "IQ 120", "Reached Sport IQ 120."),
    ("sharp", "⚡", "Sharp Mind", "Reached level Sharp."),
    ("master", "👑", "Mastermind", "Reached the top level."),
]


def streaks(bets, strategy):
    measured = sorted((b for b in bets if b["strategy"] == strategy and b.get("clv") is not None), key=lambda b: b["start"])
    best = cur = 0
    for b in measured:
        cur = cur + 1 if b["clv"] > 0 else 0
        best = max(best, cur)
    return cur, best


def week_stats(obs, bets, strategy, week_start, week_end):
    leagues = CONFIG["strategies"][strategy]["leagues"]
    studied = sum(1 for o in obs.values() if o["league"] in leagues and week_start <= parse(o["start"]) < week_end)
    measured = [b for b in bets if b["strategy"] == strategy and b.get("clv") is not None and week_start <= parse(b["start"]) < week_end]
    beat = sum(1 for b in measured if b["clv"] > 0) / len(measured) if measured else None
    return {"studied": studied, "measured": len(measured), "beat": beat}


def competition(bets, state, now, notes):
    """Achievements, streaks, weekly self-set goals and the arena ranking."""
    obs = load(OBS_FILE, {})
    week_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) - timedelta(days=now.weekday())
    unlocked = state.setdefault("achievements", {})
    out = {}
    for s, c in CONFIG["strategies"].items():
        mind = knowledge_of(obs, state.get("knowledge", {}), bets, s)
        lv = level_of(bets, s)
        cur, best = streaks(bets, s)
        mine = [b for b in bets if b["strategy"] == s]
        measured = [b for b in mine if b.get("clv") is not None]
        have = {
            "first_blood": bool(mine), "beat_it": any(b["clv"] > 0 for b in measured), "ten_measured": len(measured) >= 10,
            "on_fire": best >= 5, "scholar": mind["studied"] >= 100,
            "pattern": any(f["conf"] in ("likely", "using") for f in mind["facts"]), "rule": bool(mind["rules"]),
            "lesson": bool(learn(bets, s)[1]), "iq100": mind["iq"] >= 100, "iq120": mind["iq"] >= 120,
            "sharp": lv["n"] >= 3 and not lv["cold"], "master": lv["n"] >= 4 and not lv["cold"],
        }
        mine_unlocked = unlocked.setdefault(s, {})
        for key, emoji, title, desc in ACHIEVEMENTS:
            if have[key] and key not in mine_unlocked:
                mine_unlocked[key] = iso(now)
                notes.append(f"🏅 {c['emoji']} {c['name']} unlocked {emoji} {title}: {desc}")
        # Weekly goals: beat last week by 10%, with a floor so a quiet week still has a target.
        last = week_stats(obs, bets, s, week_start - timedelta(days=7), week_start)
        this = week_stats(obs, bets, s, week_start, week_start + timedelta(days=7))
        goals = [
            {"text": "Games studied", "target": max(20, math.ceil(last["studied"] * 1.1)), "now": this["studied"]},
            {"text": "Bets checked against the market", "target": max(3, math.ceil(last["measured"] * 1.1)), "now": this["measured"]},
            {"text": "Beat the market on", "target": round(min(0.95, max(0.55, (last["beat"] or 0) + 0.02)) * 100), "now": round((this["beat"] or 0) * 100), "unit": "%",
             "pending": this["measured"] < 3},
        ]
        hist = state.get("iq_history", {}).get(s, [])
        start_iq = next((h["iq"] for h in hist if h["d"] >= week_start.date().isoformat()), mind["iq"])
        out[s] = {"iq": mind["iq"], "iq_gain": mind["iq"] - start_iq, "streak": cur, "best_streak": best,
                  "achievements": [{"key": k, "emoji": e, "title": t, "desc": d, "at": mine_unlocked.get(k)} for k, e, t, d in ACHIEVEMENTS],
                  "goals": goals, "score": mind["iq"] + 5 * len(mine_unlocked)}
    award_trophies(out, bets, obs, state, now, week_start, notes)
    trophies = state.get("trophies", [])
    for s in out:
        out[s]["trophies"] = [t for t in trophies if t["who"] == s][::-1]
    ranking = sorted(out, key=lambda s: -out[s]["score"])
    grower = max(out, key=lambda s: out[s]["iq_gain"])
    leader = ranking[0]
    if state.get("leader") and state["leader"] != leader:
        c = CONFIG["strategies"][leader]
        notes.append(f"🏟️ {c['emoji']} {c['name']} takes the lead in the Arena.")
    state["leader"] = leader
    talk = []
    for i, s in enumerate(ranking):
        v = CONFIG["strategies"][s].get("voice", {})
        rival = CONFIG["strategies"][ranking[1 if i == 0 else 0]]["name"]
        line = (v.get("lead") if i == 0 else v.get("chase", "")).format(rival=rival)
        talk.append({"who": s, "text": line})
    return {"ranking": ranking, "grower": grower if out[grower]["iq_gain"] > 0 else None, "talk": talk, "chars": out,
            "week_start": week_start.date().isoformat()}


TROPHIES = {
    "champion": ("🏆", "Weekly Champion", "Gained the most Arena points in the week: Sport IQ growth plus new badges."),
    "grower": ("📈", "Grower of the Week", "Biggest Sport IQ jump of the week."),
    "sharpshooter": ("🎯", "Sharpshooter of the Week", "Best 'beat the market' rate of the week (5+ bets)."),
    "mastermind": ("🧠", "Betting Mastermind of the Month", "Highest Sport IQ when the month ended."),
}


def iq_on(state, s, day):
    """The character's Sport IQ on a given day (the last value recorded on or before it)."""
    hist = state.get("iq_history", {}).get(s, [])
    vals = [h["iq"] for h in hist if h["d"] <= day]
    return vals[-1] if vals else (hist[0]["iq"] if hist else None)


def award_trophies(chars, bets, obs, state, now, week_start, notes):
    """Hands out last week's trophies on the first run of a new week, and the monthly one on a new month."""
    trophies = state.setdefault("trophies", [])

    def give(kind, who, period, detail):
        e, title, _ = TROPHIES[kind]
        trophies.append({"kind": kind, "emoji": e, "title": title, "who": who, "period": period, "detail": detail, "at": iso(now)})
        c = CONFIG["strategies"][who]
        notes.append(f"{e} {c['emoji']} {c['name']} wins {title} ({period}). {detail}")

    this_week = week_start.date().isoformat()
    prev = state.get("trophy_week")
    if prev and prev != this_week:
        p_start = parse(prev + "T00:00:00Z")
        period = f"week of {p_start.strftime('%d %b')}"
        gains = {s: (iq_on(state, s, this_week) or 0) - (iq_on(state, s, prev) or 0) for s in chars}
        badges = {s: sum(1 for t in state.get("achievements", {}).get(s, {}).values() if prev <= t[:10] < this_week) for s in chars}
        points = {s: gains[s] + 5 * badges[s] for s in chars}
        # Ties go to the underdog (lower Sport IQ), so the leader has to keep growing to win.
        champ = max(chars, key=lambda s: (points[s], -chars[s]["iq"]))
        if points[champ] > 0:
            give("champion", champ, period, f"+{points[champ]} Arena points (+{gains[champ]} IQ, {badges[champ]} new badge{'s' if badges[champ] != 1 else ''}).")
        g = max(chars, key=lambda s: (gains[s], -chars[s]["iq"]))
        if gains[g] > 0:
            give("grower", g, period, f"+{gains[g]} Sport IQ.")
        rates = {}
        for s in chars:
            st = week_stats(obs, bets, s, p_start, week_start)
            if st["measured"] >= 5:
                rates[s] = (st["beat"], st["measured"])
        if rates:
            best = max(rates, key=rates.get)
            give("sharpshooter", best, period, f"Beat the market on {rates[best][0]*100:.0f}% of {rates[best][1]} bets.")
    state["trophy_week"] = this_week
    this_month = now.strftime("%Y-%m")
    prev_m = state.get("trophy_month")
    if prev_m and prev_m != this_month:
        best = max(chars, key=lambda s: chars[s]["iq"])
        give("mastermind", best, datetime.strptime(prev_m, "%Y-%m").strftime("%B %Y"), f"Sport IQ {chars[best]['iq']}.")
    state["trophy_month"] = this_month


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
    obs = load(OBS_FILE, {})
    know = state.setdefault("knowledge", {})
    rules = {s: knowledge_of(obs, know, bets, s)["rules"] for s in CONFIG["strategies"]}
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
            fair_market = fair_probs(rows)
            if not fair_market:
                continue
            observe(obs, know, ev, league_key, rows, fair_market, now)
            for strategy in chars:
                fair, nudges = adjust_fair(fair_market, ev, league_key, rules[strategy])
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
                        "thoughts": thoughts_for(strategy, v, league_key, fund, stake, favoured, levels[strategy], nudges),
                    }
                    bets.append(bet)
                    placed.append(bet)
    save(OBS_FILE, prune_obs(obs, now))
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
            # No event filter: same cost, and the results of every game help the character learn its sport.
            scores = api.get(f"/sports/{league_key}/scores", daysFrom=3, dateFormat="iso")
        except Exception as e:
            if getattr(e, "code", None) in (404, 422):
                state.setdefault("no_scores", []).append(league_key)
            else:
                notes.append(f"Could not load results for {league_name(league_key)}: {e}")
            continue
        checked[league_key] = iso(now)
        found = {s["id"]: s for s in scores}
        obs = load(OBS_FILE, {})
        for sid, sc in found.items():
            o = obs.get(sid)
            if o and not o.get("result") and sc.get("completed") and sc.get("scores"):
                pts = {x["name"]: float(x["score"]) for x in sc["scores"]}
                if o["home"] in pts and o["away"] in pts:
                    o["result"] = [pts[o["home"]], pts[o["away"]]]
        save(OBS_FILE, obs)
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


def mind_of(strategy, bets, state, now):
    """Knowledge book + Sport IQ, with a daily IQ history so growth is visible."""
    k = knowledge_of(load(OBS_FILE, {}), state.get("knowledge", {}), bets, strategy)
    hist = state.setdefault("iq_history", {}).setdefault(strategy, [])
    today = now.date().isoformat()
    if hist and hist[-1]["d"] == today:
        hist[-1]["iq"] = k["iq"]
    else:
        hist.append({"d": today, "iq": k["iq"]})
    del hist[:-120]
    week_ago = (now - timedelta(days=7)).date().isoformat()
    base = next((h["iq"] for h in reversed(hist) if h["d"] <= week_ago), hist[0]["iq"])
    return {"iq": k["iq"], "iq_week": k["iq"] - base, "iq_history": hist[-60:], "facts": k["facts"],
            "rules": len(k["rules"]), "studied": k["studied"], "resolved": k["resolved"]}


def write_dashboard(bets, state, now, notes, arena=None):
    data = {
        "updated": iso(now),
        "start_fund": CONFIG["start_fund"],
        "order": list(CONFIG["strategies"]),
        "arena": arena,
        "credits_left": state.get("credits_left"),
        "last_scan": state.get("last_scan"),
        "strategies": {s: summary(bets, s) | {"diary": state.get("diary", {}).get(s), "level": level_of(bets, s),
                                              "mind": mind_of(s, bets, state, now)} for s in CONFIG["strategies"]},
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
    arena = competition(bets, state, now, notes)
    save(BETS_FILE, bets)
    write_dashboard(bets, state, now, notes, arena)
    save(STATE_FILE, state)
    if notes:
        NOTIFY_FILE.write_text("\n\n".join(notes) + "\n")
    print("\n\n".join(notes) or "Nothing due this hour.")


if __name__ == "__main__":
    main()
