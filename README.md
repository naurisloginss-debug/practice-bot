# Practice bot

Finds value bets across many sports (football, basketball, tennis, ice hockey, American football, MMA and more) and places them with **pretend money**.
No real bets are placed.

- Runs every hour on GitHub Actions (`.github/workflows/bot.yml`).
- Twice a day it scans for value bets (winner, spread and total): a price above the fair price, which comes from Pinnacle with its margin removed.
- Before kick-off it records the final fair price, to measure whether the bot beat it.
- After the game it settles the bet and updates the practice fund.
- Dashboard: `docs/index.html`, published with GitHub Pages.
- Settings (fund, stake sizing, limits, leagues): `config.json`.

Two strategies run side by side:
- **Betfair bot**: Betfair Exchange only, after commission. This is what a real bot could do.
- **All bookmakers**: best price anywhere. Shows how much value exists if you bet by hand.

Needs one repository secret: `ODDS_API_KEY`.

## Past seasons test

`backtest.py` replays the same rules on real past odds and results from football-data.co.uk (18 football leagues) and tennis-data.co.uk (ATP and WTA). It uses no odds credits. Run it from the Actions tab ("Past seasons test"); it also runs every Monday.

## Free plan vs 20K plan

`config.json` is the active setup. It is currently the 20K plan setup (many leagues, three bet types,
two scans a day, results every 6 hours). `config.free.json` keeps the smaller setup for the free
Odds API plan (500 credits a month), and `config.20k.json` the 20K one. To switch, copy one over `config.json`.
