"""Past-seasons test: replays the value-bet rules on real historical odds and results.

Data: football-data.co.uk (free). For each match it has the prices of several bookmakers
taken a day or two before kick-off, Pinnacle's final price, and the result.

For every match we:
  1. work out the fair price from Pinnacle's early price (margin removed),
  2. "bet" 1 unit wherever a bookmaker beat it by at least the minimum edge,
  3. check the result, and compare our price with Pinnacle's final fair price.

Writes docs/backtest.json, which the dashboard shows. Uses no odds credits.
Run: python backtest.py            (downloads the data)
     python backtest.py --dir X    (reads CSV files from folder X instead, for testing)
"""
import csv
import io
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import bot

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "docs" / "backtest.json"
SEASONS = ["2324", "2425", "2526", "2627"]
LEAGUES = {  # football-data code: (name, The Odds API key)
    "E0": ("Premier League", "soccer_epl"),
    "E1": ("Championship (England 2)", "soccer_efl_champ"),
    "E2": ("League One (England 3)", "soccer_england_league1"),
    "E3": ("League Two (England 4)", "soccer_england_league2"),
    "SC0": ("Scottish Premiership", "soccer_spl"),
    "D1": ("Bundesliga", "soccer_germany_bundesliga"),
    "D2": ("2. Bundesliga", "soccer_germany_bundesliga2"),
    "I1": ("Serie A", "soccer_italy_serie_a"),
    "I2": ("Serie B", "soccer_italy_serie_b"),
    "SP1": ("La Liga", "soccer_spain_la_liga"),
    "SP2": ("La Liga 2", "soccer_spain_segunda_division"),
    "F1": ("Ligue 1", "soccer_france_ligue_one"),
    "F2": ("Ligue 2", "soccer_france_ligue_two"),
    "N1": ("Eredivisie", "soccer_netherlands_eredivisie"),
    "B1": ("Belgian Pro League", "soccer_belgium_first_div"),
    "P1": ("Primeira Liga", "soccer_portugal_primeira_liga"),
    "T1": ("Süper Lig", "soccer_turkey_super_league"),
    "G1": ("Greek Super League", "soccer_greece_super_league"),
}
# Bookmakers for the "All bookmakers" strategy (column prefixes). 1xBet is left out, as in the live bot.
BOOKS = {"B365": "bet365", "BW": "bwin", "IW": "Interwetten", "WH": "William Hill", "VC": "BetVictor", "LB": "Ladbrokes", "BF": "Betfair Sportsbook"}
ERRORS = []
EXCHANGE = "BFE"  # Betfair Exchange
COMMISSION = bot.CONFIG["commission"].get("betfair_ex_eu", 0.05)
MIN_EDGE, MAX_EDGE = bot.CONFIG["min_edge"], bot.CONFIG["max_edge"]
MIN_BETS_FOR_VERDICT = 60


def fetch(season, code, folder=None):
    if folder:
        p = Path(folder) / f"{season}_{code}.csv"
        return p.read_bytes().decode("utf-8-sig", errors="replace") if p.exists() else None
    url = f"https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "practice-bot"}), timeout=60) as r:
                raw = r.read()
            try:
                return raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                return raw.decode("latin-1")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(2 ** attempt)
        except Exception:
            time.sleep(2 ** attempt)
    return None


def num(row, col):
    try:
        v = float(row.get(col) or "")
        return v if v > 1.0 else None
    except ValueError:
        return None


def fair(prices):
    """Remove the margin from a full set of prices (dict side -> price)."""
    if not prices or any(p is None for p in prices.values()):
        return None
    total = sum(1 / p for p in prices.values())
    return {k: (1 / p) / total for k, p in prices.items()}


# ---------- tennis (tennis-data.co.uk) ----------
TENNIS_YEARS = ["2024", "2025", "2026"]
TENNIS_TOURS = {"": "ATP tennis (men)", "w": "WTA tennis (women)"}
TENNIS_BOOKS = {"B365": "bet365", "EX": "Expekt", "LB": "Ladbrokes", "SJ": "Stan James", "UB": "Unibet", "CB": "Centrebet", "GB": "Gamebookers", "IW": "Interwetten", "SB": "Sportingbet"}


def fetch_tennis(year, tour, folder=None):
    """Rows as dicts. The files are Excel workbooks."""
    import openpyxl
    if folder:
        p = Path(folder) / f"tennis_{year}{tour}.xlsx"
        if not p.exists():
            return None
        raw = p.read_bytes()
    else:
        url = f"http://www.tennis-data.co.uk/{year}{tour}/{year}.xlsx"
        raw = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "practice-bot"}), timeout=90) as r:
                    raw = r.read()
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    ERRORS.append(f"{url}: not found")
                    return None
                ERRORS.append(f"{url}: HTTP {e.code}")
                time.sleep(2 ** attempt)
            except Exception as e:
                ERRORS.append(f"{url}: {e}")
                time.sleep(2 ** attempt)
        if raw is None:
            return None
    try:
        ws = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True).active
    except Exception as e:
        ERRORS.append(f"tennis {year}{tour}: could not read the file ({e})")
        return None
    it = ws.iter_rows(values_only=True)
    head = [str(h).strip() if h is not None else "" for h in next(it)]
    return [dict(zip(head, (str(v) if v is not None else "" for v in row))) for row in it]


def run_tennis(by, folder, loaded, found_books):
    """Winner bets on tennis. The data has no final prices, so only results count here."""
    matches = 0
    for year in TENNIS_YEARS:
        for tour, name in TENNIS_TOURS.items():
            rows = fetch_tennis(year, tour, folder)
            if not rows:
                continue
            loaded.append(f"tennis{tour}-{year}")
            books = [p for p in TENNIS_BOOKS if f"{p}W" in rows[0]]
            found_books.update(TENNIS_BOOKS[p] for p in books)
            for row in rows:
                if (row.get("Comment") or "Completed").strip() != "Completed":
                    continue  # retirements and walkovers settle differently at each bookmaker
                f = fair({"W": num(row, "PSW"), "L": num(row, "PSL")})
                if not f:
                    continue
                matches += 1
                best = None
                for p in books:
                    for side in ("W", "L"):
                        price = num(row, f"{p}{side}")
                        if price is None:
                            continue
                        edge = price * f[side] - 1
                        if MIN_EDGE <= edge <= MAX_EDGE and (best is None or edge > best[2]):
                            best = (side, price, edge)
                if not best:
                    continue
                side, price, edge = best
                mult = price if side == "W" else 0.0  # the "W" player is the one who won
                for bucket, key in (("total", None), ("sport", "Tennis"), ("league", name), ("market", "Winner (tennis)"), ("season", year)):
                    target = by["all"][bucket] if key is None else by["all"][bucket].setdefault(key, new_stats())
                    add(target, mult, edge, None)
    return matches


# Each market: sides, then how to find a bookmaker's price for a side (early and final).
def market_cols(market, prefix, side, closing):
    c = "C" if closing else ""
    if market == "h2h":
        return f"{prefix}{c}{side}"
    if market == "totals":
        # Pinnacle's over/under columns are "P>2.5" / "PC>2.5"; others use their own prefix.
        p = "P" if prefix == "PS" else prefix
        return f"{p}{c}{'>' if side == 'Over' else '<'}2.5"
    # Asian handicap: e.g. B365AHH / B365CAHH; Pinnacle uses PAHH / PCAHH.
    p = "P" if prefix == "PS" else prefix
    return f"{p}{c}AH{side}"


SIDES = {"h2h": ["H", "D", "A"], "totals": ["Over", "Under"], "spreads": ["H", "A"]}
MARKET_NAMES = {"h2h": "Winner", "totals": "Total (2.5 goals)", "spreads": "Asian handicap"}


def settle(market, side, row, eff_odds):
    hg, ag = float(row["FTHG"]), float(row["FTAG"])
    if market == "h2h":
        result = "H" if hg > ag else "A" if ag > hg else "D"
        return eff_odds if side == result else 0.0
    if market == "totals":
        margin = (hg + ag - 2.5) if side == "Over" else (2.5 - hg - ag)
        return bot.payout_multiplier(margin, eff_odds)
    line = float(row.get("AHh") or "nan")  # handicap given to the home team
    margin = (hg + line - ag) if side == "H" else (ag - line - hg)
    return bot.payout_multiplier(margin, eff_odds)


def new_stats():
    return {"bets": 0, "staked": 0.0, "profit": 0.0, "clv_sum": 0.0, "clv_n": 0, "beat": 0, "edge_sum": 0.0}


def add(stats, mult, edge, clv):
    stats["bets"] += 1
    stats["staked"] += 1
    stats["profit"] += mult - 1
    stats["edge_sum"] += edge
    if clv is not None:
        stats["clv_n"] += 1
        stats["clv_sum"] += clv
        stats["beat"] += clv > 0


def finish(stats):
    n = stats["bets"]
    return {
        "bets": n,
        "roi": round(stats["profit"] / stats["staked"], 4) if stats["staked"] else None,
        "profit_units": round(stats["profit"], 2),
        "avg_edge": round(stats["edge_sum"] / n, 4) if n else None,
        "avg_clv": round(stats["clv_sum"] / stats["clv_n"], 4) if stats["clv_n"] else None,
        "beat_close": round(stats["beat"] / stats["clv_n"], 4) if stats["clv_n"] else None,
        "verdict": verdict(stats),
    }


def verdict(stats):
    if stats["bets"] < MIN_BETS_FOR_VERDICT:
        return "too few bets"
    if not stats["clv_n"]:
        # No final prices (tennis): judge on return, which needs many more bets to mean anything.
        if stats["bets"] < 500:
            return "too few bets"
        roi = stats["profit"] / stats["staked"]
        return "promising" if roi > 0.02 else "no edge" if roi < -0.01 else "unclear"
    clv = stats["clv_sum"] / stats["clv_n"]
    beat = stats["beat"] / stats["clv_n"]
    if clv > 0.01 and beat >= 0.55:
        return "promising"
    if clv < 0 or beat < 0.45:
        return "no edge"
    return "unclear"


def run(folder=None):
    strategies = {"betfair": "Betfair bot", "all": "All bookmakers"}
    by = {s: {"total": new_stats(), "sport": {}, "league": {}, "market": {}, "season": {}} for s in strategies}
    found_books, loaded, matches = set(), [], 0
    for code, (league, odds_key) in LEAGUES.items():
        for season in SEASONS:
            text = fetch(season, code, folder)
            if not text:
                continue
            rows = [r for r in csv.DictReader(io.StringIO(text)) if r.get("FTHG") not in (None, "") and r.get("HomeTeam")]
            if not rows:
                continue
            loaded.append(f"{code}-{season}")
            cols = set(rows[0].keys())
            books = [p for p in BOOKS if f"{p}H" in cols]
            found_books.update(BOOKS[p] for p in books)
            for row in rows:
                matches += 1
                for market, sides in SIDES.items():
                    if market == "spreads" and not row.get("AHh"):
                        continue
                    f_early = fair({s: num(row, market_cols(market, "PS", s, False)) for s in sides})
                    if not f_early:
                        continue
                    f_close = fair({s: num(row, market_cols(market, "PS", s, True)) for s in sides})
                    for strategy in strategies:
                        best = None
                        candidates = [(EXCHANGE, COMMISSION)] if strategy == "betfair" else [(p, 0) for p in books]
                        for prefix, commission in candidates:
                            for side in sides:
                                price = num(row, market_cols(market, prefix, side, False))
                                if price is None:
                                    continue
                                eff = 1 + (price - 1) * (1 - commission)
                                edge = eff * f_early[side] - 1
                                if MIN_EDGE <= edge <= MAX_EDGE and (best is None or edge > best[2]):
                                    best = (side, eff, edge)
                        if not best:
                            continue
                        side, eff, edge = best
                        mult = settle(market, side, row, eff)
                        clv = eff * f_close[side] - 1 if f_close else None
                        for bucket, key in (("total", None), ("sport", "Football"), ("league", league), ("market", MARKET_NAMES[market]), ("season", season)):
                            target = by[strategy][bucket] if key is None else by[strategy][bucket].setdefault(key, new_stats())
                            add(target, mult, edge, clv)
    matches += run_tennis(by, folder, loaded, found_books)
    out = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "matches": matches,
        "files": loaded,
        "seasons": SEASONS,
        "min_edge": MIN_EDGE,
        "bookmakers": sorted(found_books),
        "download_errors": ERRORS[-20:],
        "odds_keys": {name: key for name, key in LEAGUES.values()},
        "strategies": {},
    }
    for s, name in strategies.items():
        def table(d):
            rows = [{"name": k, **finish(v)} for k, v in d.items()]
            return sorted(rows, key=lambda r: (r["avg_clv"] is None, -(r["avg_clv"] or 0)))
        out["strategies"][s] = {"name": name, "total": finish(by[s]["total"]), "by_league": table(by[s]["league"]),
                                "by_sport": table(by[s]["sport"]), "by_market": table(by[s]["market"]), "by_season": sorted(table(by[s]["season"]), key=lambda r: r["name"])}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    t = out["strategies"]["all"]["total"]
    print(f"{matches} matches from {len(loaded)} files. All bookmakers: {t['bets']} bets, return {t['roi']}, vs final price {t['avg_clv']}")
    b = out["strategies"]["betfair"]["total"]
    for e in ERRORS:
        print("Download problem:", e)
    print(f"Betfair bot: {b['bets']} bets, return {b['roi']}, vs final price {b['avg_clv']}")


if __name__ == "__main__":
    folder = sys.argv[sys.argv.index("--dir") + 1] if "--dir" in sys.argv else None
    run(folder)
