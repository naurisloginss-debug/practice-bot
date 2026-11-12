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
import random
import os
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())
BETS_FILE = ROOT / "data" / "bets.json"
STATE_FILE = ROOT / "data" / "state.json"
DASH_FILE = ROOT / "docs" / "data.json"
NOTIFY_FILE = ROOT / "notify.md"
OBS_FILE = ROOT / "data" / "observations.json"
NEWS_FILE = ROOT / "data" / "news.json"
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


TODAY_MODS = {}  # per character: today's challenge rules (set each run)
TRAIT_MODS = {}  # per character: permanent effects of traits it learned from challenges (set each run)


def P(strategy, key):
    """Today's challenge rule, else the character's traits applied to its own setting (or the shared default)."""
    day, trait = TODAY_MODS.get(strategy, {}), TRAIT_MODS.get(strategy, {})
    if key in day:
        return day[key]
    val = CONFIG["strategies"][strategy].get(key, CONFIG.get(key))
    if key in trait:
        val = trait[key](val) if callable(trait[key]) else trait[key]
    if key == "min_edge":
        val += day.get("edge_bonus", 0)
    return val


# ---------- competitions ----------
LEAGUE_NAMES = dict(CONFIG["leagues"])
SPORTS = {"soccer": "Football", "basketball": "Basketball", "tennis": "Tennis", "icehockey": "Ice hockey",
          "americanfootball": "American football", "mma": "MMA", "boxing": "Boxing", "baseball": "Baseball",
          "rugbyleague": "Rugby", "rugbyunion": "Rugby", "aussierules": "Aussie rules", "cricket": "Cricket"}


def sport_of(key):
    return SPORTS.get(key.split("_")[0], key.split("_")[0].title())


def league_name(key):
    return LEAGUE_NAMES.get(key, key)


EXTRA_LEAGUES = {}  # per character: competitions it adopted or is exploring this week (set from state each run)


def leagues_of(strategy):
    base = CONFIG["strategies"][strategy]["leagues"]
    return base + [k for k in EXTRA_LEAGUES.get(strategy, []) if k not in base]


def competitions(api, state):
    """The fixed list from config first, then every other active competition in the chosen sport groups.
    Listing sports costs no credits."""
    keys = []
    for s in CONFIG["strategies"]:
        keys += [k for k in leagues_of(s) if k not in keys]
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
                reason = f"Only +{edge*100:.2f}% edge. I need +{min_edge*100:.1f}% before I move."
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
    """'avoid' wins over 'favour'. Checks this league at these odds, this league overall,
    and these odds across all leagues."""
    found = [playbook.get(segment(league_key, market, odds)), playbook.get(segment(league_key, market)),
             playbook.get(segment("*", market, odds))]
    return "avoid" if "avoid" in found else "favour" if "favour" in found else None


def learn(bets, strategy):
    """Returns (playbook, lessons). playbook: group -> 'avoid' | 'favour'."""
    L = CONFIG["learning"]
    groups = {}
    for b in bets:
        if b["strategy"] == strategy and b.get("clv") is not None and not b.get("gut") and not b.get("wild"):
            for key in (segment(b["league_key"], b["market"], b["odds"]), segment(b["league_key"], b["market"]),
                        segment("*", b["market"], b["odds"])):
                groups.setdefault(key, []).append(b)
    playbook, lessons = {}, []
    for seg, group in sorted(groups.items()):
        if len(group) < L["min_bets"]:
            continue
        clv = sum(b["clv"] for b in group) / len(group)
        beat = sum(1 for b in group if b["clv"] > 0) / len(group)
        league_key, market, band = seg.split("|")
        where = "all leagues" if league_key == "*" else league_name(league_key)
        label = f"{where} · {MARKET_NAMES[market].lower()}" + ("" if band == "*" else f" · {band}")
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
    leagues = leagues_of(strategy)
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
    mine = [b for b in bets if b["strategy"] == strategy and b.get("clv") is not None and not b.get("gut") and not b.get("wild")]
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
    sport_set = {sport_of(k) for k in leagues}
    read = sum(1 for n in load(NEWS_FILE, []) if n["sport"] in sport_set)
    iq += min(6, 2 * math.log2(1 + read / 50))  # a little from reading the news
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
    measured = [b["clv"] for b in bets if b["strategy"] == strategy and b.get("clv") is not None
                and not b.get("gut") and not b.get("wild") and not b.get("scout")]
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
    ("challenge", "🎯", "Challenge Accepted", "Completed its first daily challenge."),
    ("iron_will", "🔥", "Iron Will", "Completed 5 daily challenges in a row."),
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
    leagues = leagues_of(strategy)
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
            "challenge": challenge_stats(state, s)["done"] >= 1, "iron_will": challenge_stats(state, s)["streak"] >= 5,
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
                  "goals": goals, "score": mind["iq"] + 5 * len(mine_unlocked) + 3 * challenge_stats(state, s)["done"]}
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
    race = selection_race(bets, state, now, notes)
    for t in talk:  # they all know only one of them goes live
        v = CONFIG["strategies"][t["who"]].get("voice", {})
        pos = race["ranking"].index(t["who"]) if t["who"] in race["ranking"] else 2
        line = v.get("race_lead" if pos == 0 else "race_chase")
        if line:
            t["text"] += " " + line.format(days=race["days_left"])
    return {"ranking": ranking, "grower": grower if out[grower]["iq_gain"] > 0 else None, "talk": talk, "chars": out,
            "week_start": week_start.date().isoformat(), "race": race}


def selection_race(bets, state, now, notes):
    """Only one character continues with real money after the test. Ranked on proven skill:
    a cautious estimate of how reliably it beats the market's final price (few bets or wild swings
    count against it, so a lucky streak can't win), plus a little for challenges and traits."""
    R = CONFIG["selection"]
    sel_day = parse(R["date"] + "T00:00:00Z")
    days_left = max(0, (sel_day - now).days + (1 if (sel_day - now).seconds else 0))
    rows = []
    for s, c in CONFIG["strategies"].items():
        m = [b["clv"] for b in bets if b["strategy"] == s and b.get("clv") is not None
             and not b.get("gut") and not b.get("wild") and not b.get("scout")]
        n = len(m)
        mean = sum(m) / n if n else 0.0
        sd = math.sqrt(sum((x - mean) ** 2 for x in m) / (n - 1)) if n > 1 else 0.05
        cautious = mean - sd / math.sqrt(n) if n else -0.05  # lower edge of what its record proves
        beat = sum(1 for x in m if x > 0) / n if n else 0.0
        ch = challenge_stats(state, s)
        ch_rate = ch["done"] / ch["judged"] if ch["judged"] else 0.0
        traits = len(state.get("traits", {}).get(s, {}))
        score = 1000 * cautious + 20 * (beat - 0.5) + 5 * ch_rate + 2 * traits
        rows.append({"who": s, "eligible": n >= R["min_bets"], "measured": n, "need": max(0, R["min_bets"] - n),
                     "clv": round(mean, 4), "cautious": round(cautious, 4), "beat": round(beat, 4),
                     "challenges": round(ch_rate, 3), "traits": traits, "score": round(score, 2)})
    rows.sort(key=lambda r: (not r["eligible"], -r["score"]))
    leader = rows[0]["who"]
    if state.get("race_leader") and state["race_leader"] != leader:
        c = CONFIG["strategies"][leader]
        notes.append(f"🏁 {c['emoji']} {c['name']} now leads the race to go live ({days_left} days to selection).")
    state["race_leader"] = leader
    return {"date": R["date"], "days_left": days_left, "min_bets": R["min_bets"], "rows": rows,
            "ranking": [r["who"] for r in rows], "leader": leader}


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


# ---------- explorer mindset ----------
# Real experts keep trying new things in small, smart amounts (explore vs. exploit).
# 1) Scout bets: a promising price in a kind of bet the character has barely tried gets a small
#    "scout" bet even below its usual edge bar, to find out whether that kind of bet works.
# 2) A new competition each week: it scans one competition in its sport that it has never covered.
#    If its bets there beat the market, it adopts the competition; if not, it moves on.

def experience(bets, strategy, league_key, market, odds):
    seg = segment(league_key, market, odds)
    return sum(1 for b in bets if b["strategy"] == strategy and not b.get("wild") and not b.get("gut")
               and segment(b["league_key"], b["market"], b["odds"]) == seg)


def explore_update(sports_list, state, bets, now, notes):
    """Weekly: judge last week's exploration (adopt or move on) and pick a new competition to explore."""
    E = CONFIG["explore"]
    week = (now - timedelta(days=now.weekday())).date().isoformat()
    ex_all = state.setdefault("exploring", {})
    adopted_all = state.setdefault("adopted", {})
    tried_all = state.setdefault("tried", {})
    for s, c in CONFIG["strategies"].items():
        group = c.get("explore_group")
        if not group:
            continue
        adopted = adopted_all.setdefault(s, [])
        tried = tried_all.setdefault(s, {})
        ex = ex_all.get(s)
        if ex and ex["week"] != week:
            measured = [b["clv"] for b in bets if b["strategy"] == s and b["league_key"] == ex["key"] and b.get("clv") is not None and not b.get("wild")]
            avg = sum(measured) / len(measured) if measured else None
            if len(measured) >= E["adopt_bets"] and avg > 0:
                adopted.append(ex["key"])
                notes.append(f"🗺️ {c['emoji']} {c['name']} adopted {ex['name']}: it beat the market there ({avg*100:+.1f}% over {len(measured)} bets).")
            else:
                tried[ex["key"]] = week
                why = "not enough bets to judge" if len(measured) < E["adopt_bets"] else f"{avg*100:+.1f}% vs the market"
                notes.append(f"🧭 {c['emoji']} {c['name']} moved on from {ex['name']} ({why}).")
            ex = None
        if not ex and sports_list:
            known = set(leagues_of(s)) | set(adopted)
            fresh = [x for x in sports_list if x.get("group") == group and x.get("active") and not x.get("has_outrights")
                     and x["key"] not in known and x["key"] not in CONFIG.get("skip_competitions", [])
                     and not (x["key"] in tried and parse(tried[x["key"]] + "T00:00:00Z") > now - timedelta(days=E["retry_days"]))]
            if fresh:
                pick = random.choice(fresh)
                ex = {"key": pick["key"], "name": pick["title"], "week": week, "since": iso(now)}
                LEAGUE_NAMES[pick["key"]] = pick["title"]
                notes.append(f"🧭 {c['emoji']} {c['name']} is exploring {pick['title']} this week.")
        ex_all[s] = ex
        EXTRA_LEAGUES[s] = adopted + ([ex["key"]] if ex else [])
        for k in EXTRA_LEAGUES[s]:
            state.setdefault("league_names", {})[k] = league_name(k)


def explorer_of(strategy, bets, state):
    sc = [b for b in bets if b["strategy"] == strategy and b.get("scout")]
    m = [b["clv"] for b in sc if b.get("clv") is not None]
    ex = state.get("exploring", {}).get(strategy)
    return {"exploring": ex, "adopted": [league_name(k) for k in state.get("adopted", {}).get(strategy, [])],
            "tried": [league_name(k) for k in state.get("tried", {}).get(strategy, {})],
            "curiosity": P(strategy, "curiosity"),
            "scouts": len(sc), "scout_clv": round(sum(m) / len(m), 4) if m else None, "scout_measured": len(m)}


def place_scout_bets(bets, scout_pool, placed, now, today):
    E = CONFIG["explore"]
    for strategy, pool in scout_pool.items():
        if not pool:
            continue
        curiosity = P(strategy, "curiosity")
        done = sum(1 for b in bets if b["strategy"] == strategy and b.get("scout") and b["placed_at"][:10] == today)
        fund = fund_of(bets, strategy)
        if fund < P(strategy, "stop_below"):
            continue
        # Least-tried kinds of bets first: that's where there is most to learn.
        focus = TODAY_MODS.get(strategy, {}).get("focus_market")
        cap = E["per_day"] + (2 if focus else 0) + TRAIT_MODS.get(strategy, {}).get("extra_scouts", 0)
        for c in sorted(pool, key=lambda c: (c["market"] != focus, c["experience"], -c["edge"])):
            if done >= cap:
                break
            if any(b["event_id"] == c["ev"]["id"] and b["strategy"] == strategy for b in bets):
                continue
            if random.random() >= curiosity:
                continue
            ev, market = c["ev"], c["market"]
            stake = round(max(CONFIG["min_stake"], fund * E["stake_pct"] / 100), 2)
            pick = pick_label(market, c["name"], c["point"])
            label = f"{c['league']} {MARKET_NAMES[market].lower()} bets at {odds_band(c['odds'])}"
            bet = {
                "id": f"{ev['id']}-{strategy}-{market}-scout", "strategy": strategy, "placed_at": iso(now), "scout": True,
                "league_key": c["league_key"], "league": c["league"], "sport": sport_of(c["league_key"]), "event_id": ev["id"],
                "game": f"{ev['home_team']} vs {ev['away_team']}", "home": ev["home_team"], "away": ev["away_team"],
                "start": ev["commence_time"], "market": market, "market_name": MARKET_NAMES[market],
                "name": c["name"], "point": c["point"], "pick": pick, "book": c["book"], "book_key": c["book_key"],
                "odds": c["odds"], "eff_odds": c["eff_odds"], "fair_odds": c["fair_odds"], "edge": c["edge"],
                "stake": stake, "status": "open", "closing_fair_odds": None, "clv": None, "profit": 0.0, "score": None, "settled_at": None,
                "thoughts": [f"🧭 Exploring. I've only made {c['experience']} bet{'s' if c['experience'] != 1 else ''} like this ({label}).",
                             f"{pick} at {c['odds']:.2f} is +{c['edge']*100:.2f}% above fair: below my usual bar, but the only way to learn if this kind of bet works for me is to try it.",
                             f"Scout stake: {stake:.2f} ({E['stake_pct']:g}% of my fund). The result feeds my learning."],
            }
            bets.append(bet)
            placed.append(bet)
            done += 1


# ---------- reading the news ----------
# Twice a day each character skims its sport's news headlines (public RSS feeds), like a person
# reading the sports pages. It spots which teams are in the news and whether the news is bad
# (injuries, suspensions, a sacked coach) or good (a key player back). It uses this in its reasoning,
# as a possible hunch, and for a little Sport IQ. Headlines are data only; nothing in them is acted on
# as an instruction.

BAD_NEWS = ("injur", "ruled out", "sidelined", "out for", "doubt", "suspend", "banned", "sacked", "crisis", "illness",
            "hamstring", "torn", "surgery", "setback", "fracture")
GOOD_NEWS = ("return", "back in training", "fit again", "boost", "unbeaten", "winning run", "winning streak", "cleared to play")
GENERIC = {"united", "city", "real", "club", "sporting", "athletic", "atletico", "town", "county", "rovers", "wanderers",
           "albion", "the", "fc", "cf", "ac", "as", "sc", "de", "la", "north", "south", "west", "east", "new", "state", "saint", "st"}


def fetch_news(state, now, notes, force=False):
    """Read each sport's feeds (at most every few hours). Keeps 7 days of headlines.
    Records each feed's status so a broken feed is easy to spot."""
    news = load(NEWS_FILE, [])
    last = state.get("news_at")
    if not force and last and now - parse(last) < timedelta(hours=CONFIG.get("news_every_hours", 3)):
        return news
    state["news_at"] = iso(now)
    status = state.setdefault("feed_status", {})
    seen = {n["link"] for n in news}
    sports = {sport_of(k) for st_ in CONFIG["strategies"] for k in leagues_of(st_)}
    added = 0
    for sport in sports:
        for url in CONFIG.get("news_feeds", {}).get(sport, []):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "practice-bot (RSS reader)"})
                with urllib.request.urlopen(req, timeout=20) as res:
                    root = ET.fromstring(res.read())
                status[url] = f"ok {iso(now)}"
            except Exception as e:
                status[url] = f"error {iso(now)}: {str(e)[:80]}"
                continue
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                if not title or not link or link in seen:
                    continue
                if any(w in title.lower() for w in ("promo", "bonus code", "betting apps", "sportsbook offer")):
                    continue  # adverts, not news
                try:
                    t = parsedate_to_datetime(item.findtext("pubDate")).astimezone(timezone.utc)
                except Exception:
                    t = now
                if now - t > timedelta(days=7):
                    continue
                desc = (item.findtext("description") or "")[:300]
                low = (title + " " + desc).lower()
                tone = -1 if any(w in low for w in BAD_NEWS) else 1 if any(w in low for w in GOOD_NEWS) else 0
                news.append({"t": iso(t), "sport": sport, "title": title[:200], "link": link, "source": url.split("/")[2], "tone": tone,
                             "text": low[:500]})
                seen.add(link)
                added += 1
    news = [n for n in news if now - parse(n["t"]) <= timedelta(days=7)][-600:]
    save(NEWS_FILE, news)
    return news


def team_aliases(team):
    words = [w for w in team.replace(".", "").split() if w.lower() not in GENERIC and len(w) >= 4]
    return {team.lower()} | {w.lower() for w in words}


def team_buzz(news, team, sport, now, hours=48):
    al = team_aliases(team)
    out = []
    for n in news:
        if n["sport"] != sport or now - parse(n["t"]) > timedelta(hours=hours):
            continue
        if any(f" {a} " in f" {n['text']} " or n["text"].startswith(a) for a in al):
            out.append(n)
    return out


def news_thought(news, b, now):
    lines = []
    for team in (b["home"], b["away"]):
        items = team_buzz(news, team, b["sport"], now)
        if items:
            mood = sum(i["tone"] for i in items)
            feel = "bad news for them" if mood < 0 else "good news for them" if mood > 0 else "nothing that changes much"
            lines.append(f"I read {len(items)} article{'s' if len(items) != 1 else ''} about {team} lately ({feel}): “{items[-1]['title']}”")
    return lines


# ---------- gut feeling ----------
# Like people, the characters sometimes act on a hunch: a half-formed signal their rules don't accept.
# Hunches are small (a fixed slice of the fund), rare (a daily cap), tracked separately, and graded
# against the market. A gut that keeps beating the market earns more trust; one that doesn't, less.

def gut_record(bets, strategy):
    g = [b for b in bets if b["strategy"] == strategy and b.get("gut")]
    m = [b["clv"] for b in g if b.get("clv") is not None]
    base = P(strategy, "gut_trust")
    G = CONFIG["gut"]
    trust = base
    if len(m) >= G["min_measured"]:
        trust = base + (sum(m) / len(m)) * G["trust_per_clv"]
    trust = max(G["trust_min"], min(G["trust_max"], trust))
    return {"bets": len(g), "measured": len(m), "avg_clv": round(sum(m) / len(m), 4) if m else None,
            "beat": round(sum(1 for c in m if c > 0) / len(m), 4) if m else None, "trust": round(trust, 3),
            "base": base, "won": sum(1 for b in g if b["status"] == "won"), "lost": sum(1 for b in g if b["status"] == "lost"),
            "profit": round(sum(b["profit"] for b in g if b["status"] in ("won", "lost")), 2)}


def gut_candidates(strategy, ev, league_key, rows, fair_market, first_seen, misses_here, playbook, news=(), now=None):
    """Hunches for one game: 'almost an edge' near misses, 'smart money moving' price drift,
    and 'I read bad news about their opponent'."""
    G = CONFIG["gut"]
    out = []
    if news and "h2h" in P(strategy, "markets"):
        sport = sport_of(league_key)
        for team, other in ((ev["home_team"], ev["away_team"]), (ev["away_team"], ev["home_team"])):
            bad = [n for n in team_buzz(news, team, sport, now) if n["tone"] < 0]
            k = ("h2h", other, None)
            if not bad or k not in fair_market:
                continue
            best = None
            for r in books_for(strategy, rows):
                odds = r["prices"].get(k)
                if not odds or not P(strategy, "min_odds") <= odds <= P(strategy, "max_odds"):
                    continue
                if verdict_for(playbook, league_key, "h2h", odds) == "avoid":
                    continue
                eff = effective_odds(odds, r["key"])
                edge = eff * fair_market[k] - 1
                if edge >= -0.005 and (best is None or edge > best["edge"]):
                    best = {"market": "h2h", "name": other, "point": None, "book": r["title"], "book_key": r["key"], "odds": odds,
                            "eff_odds": round(eff, 4), "fair_odds": round(1 / fair_market[k], 3), "prob": fair_market[k], "edge": round(edge, 4)}
            if best:
                out.append({**best, "score": 1.1 + 0.1 * len(bad), "signal": "news",
                            "why": f"I read that {team} have trouble: “{bad[-1]['title']}”. The price on {other} may not have caught up yet."})
    lo, hi = P(strategy, "min_odds"), P(strategy, "max_odds")
    for m in misses_here:
        if m["reason"].startswith("Only +") and m["edge"] >= G["min_edge"]:
            out.append({**m, "score": m["edge"] / P(strategy, "min_edge"), "signal": "almost",
                        "why": f"Only +{m['edge']*100:.2f}% edge, just under my +{P(strategy, 'min_edge')*100:.1f}% bar. Can't prove it, but I like it."})
    if first_seen and first_seen.get("h2h") and "h2h" in P(strategy, "markets"):
        role_name = {"home": ev["home_team"], "away": ev["away_team"], "draw": "Draw"}
        for role, p0 in first_seen["h2h"].items():
            k = ("h2h", role_name[role], None)
            if k not in fair_market:
                continue
            drift = fair_market[k] - p0
            if drift < G["steam"]:
                continue
            best = None
            for r in books_for(strategy, rows):
                odds = r["prices"].get(k)
                if not odds or not lo <= odds <= hi * 1.2:
                    continue
                if verdict_for(playbook, league_key, "h2h", odds) == "avoid":
                    continue
                eff = effective_odds(odds, r["key"])
                edge = eff * fair_market[k] - 1
                if edge >= -0.01 and (best is None or edge > best["edge"]):
                    best = {"market": "h2h", "name": k[1], "point": None, "book": r["title"], "book_key": r["key"], "odds": odds,
                            "eff_odds": round(eff, 4), "fair_odds": round(1 / fair_market[k], 3), "prob": fair_market[k], "edge": round(edge, 4)}
            if best:
                out.append({**best, "score": 1 + drift * 20, "signal": "steam",
                            "why": f"The sharp money is moving to {k[1]}: its chance went from {p0*100:.0f}% to {fair_market[k]*100:.0f}% since I first looked."})
    return out


# ---------- daily challenges ----------
# Every morning each character gets a challenge aimed at its weak spot. Challenges reward skill
# (beating the market, studying, adapting), never volume or risk. Some change how it bets that day.

CHALLENGES = {
    "precision": ("🎯", "Precision day", "Only bets with an edge 1% above my usual bar, and at least 2 of them must beat the market."),
    "discipline": ("🧘", "Discipline day", "No gut calls, no wild bets, nothing above odds 3.0, and still beat the market."),
    "new_ground": ("🗺️", "New ground", "Make a bet in the bet type I use least: {market}."),
    "sharp_eye": ("🦅", "Sharp eye", "Find a +3% edge that holds up against the market."),
    "study": ("📚", "Study day", "Study at least {target} games today."),
    "explorer": ("🧭", "Explorer day", "Make a bet in {competition}, the competition I'm exploring."),
    "news": ("📰", "Read and react", "Use something I read in a bet today."),
    "underdog": ("🐶", "Underdog hunter", "Back an underdog (odds 2.5+) that beats the market."),
}


def day_bets(bets, strategy, day):
    return [b for b in bets if b["strategy"] == strategy and b["placed_at"][:10] == day]


def pick_challenge(bets, state, strategy, day):
    mine = [b for b in bets if b["strategy"] == strategy and not b.get("wild")]
    measured = [b for b in mine if b.get("clv") is not None]
    beat = sum(1 for b in measured if b["clv"] > 0) / len(measured) if measured else None
    longs = [b["clv"] for b in measured if b["odds"] >= 3.5]
    markets = P(strategy, "markets")
    use = {m: sum(1 for b in mine if b["market"] == m) for m in markets}
    least = min(use, key=use.get)
    weights = {"precision": 1, "discipline": 1, "sharp_eye": 1, "study": 1, "news": 1}
    if beat is not None and beat < 0.55:
        weights["precision"] += 3
    if longs and sum(longs) / len(longs) < 0:
        weights["discipline"] += 3
    if len(markets) > 1 and use[least] < 0.15 * max(1, len(mine)):
        weights["new_ground"] = 3
    if state.get("exploring", {}).get(strategy):
        weights["explorer"] = 2
    if "h2h" in markets and sport_of(CONFIG["strategies"][strategy]["leagues"][0]) != "Football":
        weights["underdog"] = 2
    recent = [c["id"] for c in state.get("challenges", {}).get(strategy, [])[-2:]]
    for cid in recent:  # vary the challenges
        if cid in weights:
            weights[cid] = max(0.3, weights[cid] / 3)
    cid = random.choices(list(weights), weights=list(weights.values()))[0]
    emoji, title, text = CHALLENGES[cid]
    looked = [v for v in state.get("looked_by_day", {}).get(strategy, {}).values()]
    target = max(10, round(0.8 * (sum(looked) / len(looked)))) if looked else 20
    ex = state.get("exploring", {}).get(strategy) or {}
    text = text.format(market=MARKET_NAMES[least].lower(), target=target, competition=ex.get("name", "a new competition"))
    return {"day": day, "id": cid, "emoji": emoji, "title": title, "text": text, "status": "active",
            "market": least, "target": target, "league": ex.get("key")}


def challenge_mods(ch):
    if not ch or ch["status"] != "active":
        return {}
    if ch["id"] == "precision":
        return {"edge_bonus": 0.01, "no_gut": True, "no_wild": True}
    if ch["id"] == "discipline":
        return {"max_odds": 3.0, "no_gut": True, "no_wild": True}
    if ch["id"] == "new_ground":
        return {"focus_market": ch["market"]}
    return {}


def judge_challenge(ch, bets, state, strategy, now):
    """'done', 'failed', or None while it can't be judged yet (e.g. waiting for final prices)."""
    day_over = now.date().isoformat() > ch["day"]
    mine = day_bets(bets, strategy, ch["day"])
    serious = [b for b in mine if not b.get("gut") and not b.get("wild")]
    waiting = any(b.get("clv") is None and b["status"] == "open" for b in serious)
    cid = ch["id"]
    if cid in ("precision", "discipline", "sharp_eye", "underdog"):
        pool = serious
        if cid == "sharp_eye":
            pool = [b for b in serious if b["edge"] >= 0.03]
        if cid == "underdog":
            pool = [b for b in serious if b["market"] == "h2h" and b["odds"] >= 2.5]
        beat = [b for b in pool if b.get("clv") is not None and b["clv"] > 0]
        need = 2 if cid == "precision" else 1
        if len(beat) >= need:
            return "done", f"{len(beat)} bet{'s' if len(beat) != 1 else ''} beat the market."
        if day_over and not pool:
            return "none", "No chance today: nothing fit the challenge, and not betting was the right call."
        if day_over and not any(b.get("clv") is None for b in pool):
            return "failed", f"{len(pool)} bet{'s' if len(pool) != 1 else ''}, but not enough beat the market."
        return None, ""
    if cid == "new_ground":
        hit = [b for b in mine if b["market"] == ch["market"]]
        if hit:
            return "done", f"Bet {hit[0]['pick']} ({MARKET_NAMES[ch['market']].lower()})."
        return ("failed", "No bet of that type today.") if day_over else (None, "")
    if cid == "study":
        n = state.get("looked_by_day", {}).get(strategy, {}).get(ch["day"], 0)
        if n >= ch["target"]:
            return "done", f"Studied {n} games."
        return ("failed", f"Studied {n} of {ch['target']} games.") if day_over else (None, "")
    if cid == "explorer":
        hit = [b for b in mine if b["league_key"] == ch.get("league")]
        if hit:
            return "done", f"Bet {hit[0]['pick']} there."
        return ("failed", "No bet there today.") if day_over else (None, "")
    if cid == "news":
        hit = [b for b in mine if any("I read" in t for t in b.get("thoughts", []))]
        if hit:
            return "done", f"Used the news in {hit[0]['pick']}."
        return ("failed", "Nothing I read connected to a bet.") if day_over else (None, "")
    return None, ""


def run_challenges(bets, state, now, notes):
    """Judge open challenges, give each character today's challenge, and set today's rules."""
    day = now.date().isoformat()
    all_ch = state.setdefault("challenges", {})
    for s, c in CONFIG["strategies"].items():
        hist = all_ch.setdefault(s, [])
        for ch in hist:
            if ch["status"] in ("active", "judging"):
                verdict, detail = judge_challenge(ch, bets, state, s, now)
                if verdict:
                    ch["status"], ch["detail"] = verdict, detail
                    if verdict != "none":
                        notes.append(f"{'✅' if verdict == 'done' else '❌'} {c['emoji']} {c['name']} "
                                     f"{'completed' if verdict == 'done' else 'missed'} {ch['emoji']} {ch['title']}: {detail}")
                elif ch["day"] < day:
                    ch["status"] = "judging"  # the day is over, waiting for final prices
        if not hist or hist[-1]["day"] != day:
            ch = pick_challenge(bets, state, s, day)
            hist.append(ch)
            notes.append(f"🎯 {c['emoji']} {c['name']}'s challenge today: {ch['emoji']} {ch['title']}. {ch['text']}")
        del hist[:-60]
        TODAY_MODS[s] = challenge_mods(hist[-1])


TRAITS = {
    "precision": ("🎯", "Precise", "Always demands +0.5% more edge.", {"min_edge": lambda v: v + 0.005}),
    "discipline": ("🧘", "Disciplined", "Avoids odds above 3.5.", {"max_odds": lambda v: min(v, 3.5)}),
    "new_ground": ("🗺️", "Versatile", "Keeps trying every bet type (+1 scout bet a day).", {"extra_scouts": 1}),
    "sharp_eye": ("🦅", "Sharp-eyed", "Bets 15% more when the edge is +3% or bigger.", {"sharp_boost": 1.15}),
    "study": ("📚", "Studious", "Looks a day further ahead.", {"days_ahead": lambda v: v + 1}),
    "explorer": ("🧭", "Adventurous", "15% more curious.", {"curiosity": lambda v: min(0.95, v + 0.15)}),
    "news": ("📰", "Well-read", "Trusts its hunches more (+10% gut trust).", {"gut_trust": lambda v: min(0.9, v + 0.1)}),
    "underdog": ("🐶", "Underdog lover", "Allows underdogs at odds up to 1.0 higher.", {"max_odds": lambda v: v + 1.0}),
}


def challenge_effect(bets, state, strategy, cid):
    """Bets on days with this challenge vs bets on all other days, measured against the market's final price."""
    days = {c["day"] for c in state.get("challenges", {}).get(strategy, []) if c["id"] == cid and c["status"] != "active"}
    serious = [b for b in bets if b["strategy"] == strategy and b.get("clv") is not None and not b.get("gut") and not b.get("wild")]
    on = [b["clv"] for b in serious if b["placed_at"][:10] in days]
    off = [b["clv"] for b in serious if b["placed_at"][:10] not in days]
    stats = lambda xs: (sum(xs) / len(xs), sum(1 for x in xs if x > 0) / len(xs)) if xs else (None, None)
    return len(days), on, off, stats(on), stats(off)


def update_traits(bets, state, now, notes):
    """A challenge that clearly makes a character stronger becomes part of who it is; one that stops helping is dropped."""
    T = CONFIG["traits"]
    all_traits = state.setdefault("traits", {})
    for s, c in CONFIG["strategies"].items():
        mine = all_traits.setdefault(s, {})
        for cid in TRAITS:
            n_days, on, off, (clv_on, beat_on), (clv_off, beat_off) = challenge_effect(bets, state, s, cid)
            emoji, title, effect, _ = TRAITS[cid]
            ch_title = CHALLENGES[cid][1]
            if cid not in mine:
                if (n_days >= T["min_days"] and len(on) >= T["min_bets"] and len(off) >= T["min_base_bets"]
                        and clv_on >= T["min_gain"] and clv_on - clv_off >= T["min_gain"] and beat_on >= beat_off
                        and len(mine) < T["max_traits"]):
                    mine[cid] = {"since": iso(now), "on": round(clv_on, 4), "off": round(clv_off, 4), "bets": len(on)}
                    notes.append(f"🧬 {c['emoji']} {c['name']} made {ch_title} part of its character: {emoji} {title}. "
                                 f"On those days its bets beat the market by {clv_on*100:+.1f}%, vs {clv_off*100:+.1f}% normally. {effect}")
            elif on and off:
                mine[cid].update(on=round(clv_on, 4), off=round(clv_off, 4), bets=len(on))
                if len(on) >= T["drop_bets"] and clv_on - clv_off < 0:
                    del mine[cid]
                    notes.append(f"🧬 {c['emoji']} {c['name']} dropped the {emoji} {title} trait: it stopped helping "
                                 f"({clv_on*100:+.1f}% vs {clv_off*100:+.1f}% normally).")
        mods = {}
        for cid in mine:
            for k, v in TRAITS[cid][3].items():
                if callable(v) and callable(mods.get(k)):
                    prev = mods[k]
                    mods[k] = (lambda f, g: (lambda x: f(g(x))))(v, prev)
                else:
                    mods[k] = v
        TRAIT_MODS[s] = mods


def traits_of(state, strategy):
    out = []
    for cid, t in state.get("traits", {}).get(strategy, {}).items():
        emoji, title, effect, _ = TRAITS[cid]
        out.append({"id": cid, "emoji": emoji, "title": title, "effect": effect, "from": CHALLENGES[cid][1],
                    "since": t["since"], "on": t["on"], "off": t["off"], "bets": t["bets"]})
    return out


def challenge_stats(state, strategy):
    hist = state.get("challenges", {}).get(strategy, [])
    done = [c for c in hist if c["status"] == "done"]
    streak = 0
    for c in reversed([c for c in hist if c["status"] in ("done", "failed")]):
        if c["status"] != "done":
            break
        streak += 1
    return {"today": hist[-1] if hist else None, "recent": hist[-8:][::-1], "done": len(done),
            "judged": sum(1 for c in hist if c["status"] in ("done", "failed")), "streak": streak}


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


def place_gut_bets(bets, gut_pool, placed, now, today):
    G = CONFIG["gut"]
    for strategy, pool in gut_pool.items():
        if not pool or TODAY_MODS.get(strategy, {}).get("no_gut"):
            continue
        rec = gut_record(bets, strategy)
        cap = G["daily_cap_high"] if rec["trust"] >= G["high_trust"] else G["daily_cap"]
        done_today = sum(1 for b in bets if b["strategy"] == strategy and b.get("gut") and b["placed_at"][:10] == today)
        fund = fund_of(bets, strategy)
        if fund < P(strategy, "stop_below"):
            continue
        for c in sorted(pool, key=lambda c: -c["score"]):
            if done_today >= cap:
                break
            if any(b["event_id"] == c["ev"]["id"] and b["strategy"] == strategy for b in bets):
                continue
            # The human part: even a good hunch is followed only some of the time.
            if random.random() >= rec["trust"]:
                continue
            ev, market = c["ev"], c["market"]
            stake = round(max(CONFIG["min_stake"], fund * G["stake_pct"] / 100), 2)
            voice = CONFIG["strategies"][strategy].get("voice", {})
            pick = pick_label(market, c["name"], c["point"])
            bet = {
                "id": f"{ev['id']}-{strategy}-{market}-gut", "strategy": strategy, "placed_at": iso(now), "gut": True,
                "league_key": c["league_key"], "league": c["league"], "sport": sport_of(c["league_key"]), "event_id": ev["id"],
                "game": f"{ev['home_team']} vs {ev['away_team']}", "home": ev["home_team"], "away": ev["away_team"],
                "start": ev["commence_time"], "market": market, "market_name": MARKET_NAMES[market],
                "name": c["name"], "point": c["point"], "pick": pick, "book": c["book"], "book_key": c["book_key"],
                "odds": c["odds"], "eff_odds": c["eff_odds"], "fair_odds": c["fair_odds"], "edge": c["edge"],
                "stake": stake, "status": "open", "closing_fair_odds": None, "clv": None, "profit": 0.0, "score": None, "settled_at": None,
                "thoughts": [c["why"], "My rules say no. " + voice.get("gut", "But my gut says yes."),
                             f"A hunch gets a small stake: {stake:.2f} ({G['stake_pct']:g}% of my fund). My gut is trusted {rec['trust']*100:.0f}% right now, "
                             f"and every hunch is graded against the market."],
            }
            bets.append(bet)
            placed.append(bet)
            done_today += 1


def wild_record(bets, strategy):
    w = [b for b in bets if b["strategy"] == strategy and b.get("wild")]
    m = [b["clv"] for b in w if b.get("clv") is not None]
    return {"bets": len(w), "won": sum(1 for b in w if b["status"] == "won"), "lost": sum(1 for b in w if b["status"] == "lost"),
            "profit": round(sum(b["profit"] for b in w if b["status"] in ("won", "lost")), 2),
            "best": max((b["odds"] for b in w if b["status"] == "won"), default=None),
            "avg_clv": round(sum(m) / len(m), 4) if m else None, "appetite": CONFIG["strategies"][strategy].get("wild_appetite", 0.3)}


def wild_candidates(strategy, ev, league_key, rows, fair_market, news, now):
    """Bold long shots: odds in the wild range, not terrible value. News or a moving price makes one more tempting."""
    W = CONFIG["wild"]
    out = []
    for r in books_for(strategy, rows):
        for k, odds in r["prices"].items():
            wild_markets = P(strategy, "markets") + CONFIG["strategies"][strategy].get("wild_markets", [])
            if k not in fair_market or k[0] not in wild_markets or not W["min_odds"] <= odds <= W["max_odds"]:
                continue
            eff = effective_odds(odds, r["key"])
            edge = eff * fair_market[k] - 1
            if edge < W["min_edge"]:
                continue
            out.append({"market": k[0], "name": k[1], "point": k[2], "book": r["title"], "book_key": r["key"], "odds": odds,
                        "eff_odds": round(eff, 4), "fair_odds": round(1 / fair_market[k], 3), "prob": fair_market[k], "edge": round(edge, 4),
                        "score": edge + 0.02 * len(team_buzz(news, k[1], sport_of(league_key), now)) if news else edge})
    return out


def wild_mood(bets, strategy):
    """How the character feels about a wild bet today, and why. Returns (multiplier, reason)."""
    fund = fund_of(bets, strategy)
    start = P(strategy, "start_fund")
    cur, _ = streaks(bets, strategy)
    lv = level_of(bets, strategy)
    if fund < start * 0.95:
        return 0.0, f"I'm down ({fund:.2f} of {start:.0f}), and I don't chase losses."
    if lv["cold"]:
        return 0.3, "I'm on a cold streak. Staying careful."
    if cur >= 3:
        return 1.4, f"Feeling sharp after beating the market {cur} times in a row."
    return 1.0, ""


def place_wild_bets(bets, wild_pool, placed, now, today):
    """Wild bets are a choice, never a duty: the character decides each day, by appetite and mood,
    and says why it did or didn't. Returns {strategy: choice text}."""
    W = CONFIG["wild"]
    choices = {}
    for strategy, pool in wild_pool.items():
        c_cfg = CONFIG["strategies"][strategy]
        if TODAY_MODS.get(strategy, {}).get("no_wild"):
            choices[strategy] = "No wild bets today: today's challenge is about discipline."
            continue
        if not pool:
            choices[strategy] = "No long shot worth a look today."
            continue
        pool = sorted(pool, key=lambda c: -(c["score"] + random.uniform(0, 0.02)))
        top = pool[0]
        tempting = f"{pick_label(top['market'], top['name'], top['point'])} at {top['odds']:.2f}"
        mult, why = wild_mood(bets, strategy)
        appetite = min(0.9, P(strategy, "wild_appetite") * mult)
        done = sum(1 for b in bets if b["strategy"] == strategy and b.get("wild") and b["placed_at"][:10] == today)
        fund = fund_of(bets, strategy)
        if done >= W["per_day"]:
            continue
        if fund < P(strategy, "stop_below") or appetite == 0:
            choices[strategy] = f"Saw {tempting}. Tempting. Passed: {why or 'not today.'}"
            continue
        took = []
        for c in pool:
            if done >= W["per_day"]:
                break
            if any(b["event_id"] == c["ev"]["id"] and b["strategy"] == strategy for b in bets):
                continue
            if random.random() >= appetite * (0.6 if done else 1.0):
                break  # its choice: not (again) today
            ev, market = c["ev"], c["market"]
            stake = round(max(CONFIG["min_stake"], fund * W["stake_pct"] / 100), 2)
            pick = pick_label(market, c["name"], c["point"])
            bet = {
                "id": f"{ev['id']}-{strategy}-{market}-wild", "strategy": strategy, "placed_at": iso(now), "wild": True,
                "league_key": c["league_key"], "league": c["league"], "sport": sport_of(c["league_key"]), "event_id": ev["id"],
                "game": f"{ev['home_team']} vs {ev['away_team']}", "home": ev["home_team"], "away": ev["away_team"],
                "start": ev["commence_time"], "market": market, "market_name": MARKET_NAMES[market],
                "name": c["name"], "point": c["point"], "pick": pick, "book": c["book"], "book_key": c["book_key"],
                "odds": c["odds"], "eff_odds": c["eff_odds"], "fair_odds": c["fair_odds"], "edge": c["edge"],
                "stake": stake, "status": "open", "closing_fair_odds": None, "clv": None, "profit": 0.0, "score": None, "settled_at": None,
                "thoughts": [f"Wild one: {pick} at {c['odds']:.2f}. The market gives it {c['prob']*100:.0f}%."] + ([why] if why else []) +
                            [c_cfg.get("voice", {}).get("wild", "Probably won't land. But if it does..."),
                             f"My choice, tiny stake: {stake:.2f} ({W['stake_pct']:g}% of my fund). Kept out of my serious record."],
            }
            bets.append(bet)
            placed.append(bet)
            took.append(f"{pick} at {c['odds']:.2f}")
            done += 1
        if took:
            choices[strategy] = (why + " " if why else "") + "Took a shot: " + ", ".join(took) + "."
        else:
            choices[strategy] = f"Saw {tempting}. Tempting, but I passed. {why or 'Not feeling it today.'}".strip()
    return choices


def scan(api, bets, state, now, notes):
    slot = due_scan(state, now)
    if not slot:
        return
    today = now.date().isoformat()
    if api.low_on_credits():
        # Don't use up the scan: it runs on the next 15-minute check once credits are back. Say so once.
        if state.get("skip_note") != slot:
            state["skip_note"] = slot
            notes.append("Waiting to scan: odds credits are below the reserve. It will run as soon as credits are available.")
        return
    state["last_scan"] = slot
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
    gut_pool = {s: [] for s in CONFIG["strategies"]}
    scout_pool = {s: [] for s in CONFIG["strategies"]}
    wild_pool = {s: [] for s in CONFIG["strategies"]}
    news = load(NEWS_FILE, [])
    for league_key in competitions(api, state):
        league = league_name(league_key)
        chars = [s for s in CONFIG["strategies"] if league_key in leagues_of(s)]
        markets = {m for s in chars for m in P(s, "markets") + CONFIG["strategies"][s].get("wild_markets", [])}
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
            first_seen = dict(obs[ev["id"]]) if ev["id"] in obs else None
            observe(obs, know, ev, league_key, rows, fair_market, now)
            for strategy in chars:
                misses_here = []
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
                        misses_here.append(miss)
                        E = CONFIG["explore"]
                        if not v and miss["reason"].startswith("Only +") and miss["edge"] >= E["min_edge"]:
                            exp = experience(bets, strategy, league_key, market, miss["odds"])
                            if exp < E["until"]:
                                scout_pool[strategy].append({**miss, "experience": exp, "ev": ev, "league_key": league_key, "league": league})
                    if not v:
                        continue
                    fund = fund_of(bets, strategy)
                    if fund < P(strategy, "stop_below"):
                        stopped[strategy] = True
                        continue
                    favoured = verdict_for(playbooks[strategy], league_key, market, v["odds"]) == "favour"
                    boost = (CONFIG["learning"]["favour_boost"] if favoured else 1.0) * levels[strategy]["stake"]
                    if v["edge"] >= 0.03 and TRAIT_MODS.get(strategy, {}).get("sharp_boost"):
                        boost *= TRAIT_MODS[strategy]["sharp_boost"]
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
                if not any(b["event_id"] == ev["id"] and b["strategy"] == strategy for b in bets):
                    for c in gut_candidates(strategy, ev, league_key, rows, fair_market, first_seen, misses_here, playbooks[strategy], news, now):
                        gut_pool[strategy].append({**c, "ev": ev, "league_key": league_key, "league": league})
                    for c in wild_candidates(strategy, ev, league_key, rows, fair_market, news, now):
                        wild_pool[strategy].append({**c, "ev": ev, "league_key": league_key, "league": league})
    place_scout_bets(bets, scout_pool, placed, now, today)
    place_gut_bets(bets, gut_pool, placed, now, today)
    wild_choices = place_wild_bets(bets, wild_pool, placed, now, today)
    for b in placed:  # what the character read about these teams goes into its reasoning
        extra = news_thought(news, b, now)
        if extra:
            b["thoughts"] = b["thoughts"][:-1] + extra + b["thoughts"][-1:]
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
        lbd = state.setdefault("looked_by_day", {}).setdefault(strategy, {})
        lbd[today] = lbd.get(today, 0) + len(looked[strategy])
        for d_ in sorted(lbd)[:-14]:
            del lbd[d_]
        mindset = c.get("voice", {}).get("mindset")
        diary[strategy] = {"t": iso(now), "mood": mood, "mindset": mindset, "looked": len(looked[strategy]), "placed": len(mine),
                           "wild_choice": wild_choices.get(strategy),
                           "scouts": sum(1 for b in mine if b.get("scout")),
                           "passed": [{k: m[k] for k in ("game", "league", "pick", "odds", "book", "edge", "reason")} for m in top]}
    if placed:
        lines = [f"- {CONFIG['strategies'][b['strategy']]['name']}{' 💭 gut' if b.get('gut') else ''}{' 🎲 wild' if b.get('wild') else ''}{' 🧭 scout' if b.get('scout') else ''}: **{b['pick']}** ({b['market_name']}) in {b['game']} "
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
    # Leagues with studied games that have finished but have no result yet: fetch them too, so the
    # characters learn from every game, not only the ones they bet on.
    since = now - timedelta(days=3)
    for o in load(OBS_FILE, {}).values():
        start = parse(o["start"])
        if not o.get("result") and since <= start <= now - timedelta(hours=CONFIG["settle_after_hours"]):
            by_league.setdefault(o["league"], [])
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
        "gut": gut_record(bets, strategy),
        "wild": wild_record(bets, strategy),
        "reading": reading_of(strategy),
    }


def profile(strategy):
    c = CONFIG["strategies"][strategy]
    return {k: c.get(k) for k in ("name", "emoji", "sport", "inspired_by", "story", "style", "motto", "backtest")} | {
        "risk": {"kelly": P(strategy, "kelly_fraction"), "max_stake_pct": P(strategy, "max_stake_pct"),
                 "min_edge": P(strategy, "min_edge"), "odds": [P(strategy, "min_odds"), P(strategy, "max_odds")],
                 "stop_below": P(strategy, "stop_below")},
        "leagues": [league_name(k) for k in leagues_of(strategy)],
        "markets": [MARKET_NAMES[m] for m in P(strategy, "markets")],
    }


def reading_of(strategy):
    sports = {sport_of(k) for k in leagues_of(strategy)}
    news = [n for n in load(NEWS_FILE, []) if n["sport"] in sports]
    news.sort(key=lambda n: n["t"], reverse=True)
    flagged = [n for n in news if n["tone"] != 0][:6]
    return {"week": len(news), "latest": [{k: n[k] for k in ("t", "title", "link", "source", "tone")} for n in (flagged or news)[:6]]}


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
                                              "explorer": explorer_of(s, bets, state),
                                              "challenge": challenge_stats(state, s),
                                              "traits": traits_of(state, s),
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
    try:
        fetch_news(state, now, notes)
    except Exception as e:
        print("News reading failed:", e)
    sports_list = []
    try:
        sports_list = api.get("/sports")  # free call: refreshes the real credit balance (e.g. after a plan upgrade or new key)
    except Exception:
        pass
    for s_ in CONFIG["strategies"]:
        ex = state.get("exploring", {}).get(s_)
        EXTRA_LEAGUES[s_] = state.get("adopted", {}).get(s_, []) + ([ex["key"]] if ex else [])
    explore_update(sports_list, state, bets, now, notes)
    update_traits(bets, state, now, notes)
    run_challenges(bets, state, now, notes)
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
