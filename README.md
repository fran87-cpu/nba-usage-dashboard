# NBA Box Score + USG%

A first Streamlit prototype that combines NBA traditional and advanced player box scores.

## Run locally

1. Install Python 3.10+.
2. In this folder run `python -m pip install -r requirements.txt`.
3. Run `streamlit run app.py`.
4. Enter an NBA game ID (default: `0022600001`).

The app merges the official NBA `BoxScoreTraditionalV3` and `BoxScoreAdvancedV3` responses by game, team, and player ID. `usagePercentage` is multiplied by 100 for display.

**Important:** NBA Stats endpoints are unofficially accessed through `nba_api` and may time out or block some IPs. The default game is not guaranteed to be retrievable. Missing USG% is displayed as missing, never zero. Live in-game updating is not implemented.
