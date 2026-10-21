import re
from datetime import datetime

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="NBA Box Score + Player Usage", page_icon="🏀", layout="wide")

BASE_URL = "https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{game_id}.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NBABoxScoreDemo/1.0)", "Accept": "application/json"}


def minutes_as_float(value):
    """NBA live feed can use ISO-like PT34M12.00S or clock-style 34:12."""
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    match = re.fullmatch(r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?", s)
    if match:
        hours, mins, secs = (float(x or 0) for x in match.groups())
        return hours * 60 + mins + secs / 60
    if ":" in s:
        parts = s.split(":")
        try:
            return float(parts[0]) + float(parts[1]) / 60
        except (ValueError, IndexError):
            return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def num(d, key):
    value = d.get(key)
    return float(value) if value is not None else 0.0


@st.cache_data(ttl=600, show_spinner=False)
def fetch_game(game_id):
    response = requests.get(BASE_URL.format(game_id=game_id), headers=HEADERS, timeout=15)
    response.raise_for_status()
    payload = response.json()
    game = payload.get("game")
    if not isinstance(game, dict) or not game.get("homeTeam") or not game.get("awayTeam"):
        raise ValueError("The NBA returned an unexpected or incomplete game record.")
    return game


def team_table(team):
    players = team.get("players", [])
    team_stats = team.get("statistics") or {}
    # Team stats should come from the feed. If unavailable, sum player statistics.
    def total(key):
        if team_stats.get(key) is not None:
            return num(team_stats, key)
        return sum(num(p.get("statistics") or {}, key) for p in players)

    team_fga = total("fieldGoalsAttempted")
    team_fta = total("freeThrowsAttempted")
    team_tov = total("turnovers")
    team_actions = team_fga + 0.44 * team_fta + team_tov
    total_team_minutes = sum(minutes_as_float(p.get("statistics", {}).get("minutes")) for p in players)
    rows = []
    for p in players:
        s = p.get("statistics") or {}
        minutes = minutes_as_float(s.get("minutes"))
        # Basketball Reference-style estimate, not NBA's official on-court usage.
        actions = num(s, "fieldGoalsAttempted") + 0.44 * num(s, "freeThrowsAttempted") + num(s, "turnovers")
        usage = 100 * actions * (total_team_minutes / 5) / (minutes * team_actions) if minutes > 0 and team_actions > 0 and total_team_minutes > 0 else None
        name = p.get("name") or " ".join(filter(None, [p.get("firstName"), p.get("familyName")])) or "Unknown"
        rows.append({
            "Player": name, "MIN": round(minutes, 1), "PTS": int(num(s, "points")),
            "FGM": int(num(s, "fieldGoalsMade")), "FGA": int(num(s, "fieldGoalsAttempted")),
            "3PM": int(num(s, "threePointersMade")), "3PA": int(num(s, "threePointersAttempted")),
            "FTM": int(num(s, "freeThrowsMade")), "FTA": int(num(s, "freeThrowsAttempted")),
            "OREB": int(num(s, "reboundsOffensive")), "DREB": int(num(s, "reboundsDefensive")),
            "REB": int(num(s, "reboundsTotal")), "AST": int(num(s, "assists")),
            "STL": int(num(s, "steals")), "BLK": int(num(s, "blocks")),
            "TOV": int(num(s, "turnovers")), "PF": int(num(s, "foulsPersonal")),
            "+/-": s.get("plusMinusPoints"), "USG% (Est.)": round(usage, 1) if usage is not None else None,
            "Status": p.get("notPlayingReason") or p.get("notPlayingDescription") or "",
        })
    return pd.DataFrame(rows)


st.title("🏀 NBA Box Score + Player Usage")
st.caption("Traditional NBA box score with an estimated Player Usage Rate (USG%).")
game_id = st.text_input("NBA Game ID", value="0022600001", max_chars=10, help="The 10-digit ID in an NBA.com game URL.").strip()
if not re.fullmatch(r"\d{10}", game_id):
    st.warning("Enter a 10-digit NBA Game ID, for example 0022600001.")
    st.stop()

with st.spinner("Loading box score from NBA Live Data..."):
    try:
        game = fetch_game(game_id)
    except requests.exceptions.Timeout:
        st.error("The NBA Live Data service did not respond in time. Please try again later.")
        st.stop()
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        st.error(f"NBA Live Data returned HTTP {status}. Check that this game has a published box score.")
        st.stop()
    except (requests.exceptions.RequestException, ValueError) as exc:
        st.error(f"Could not load this game: {exc}")
        st.stop()

away, home = game["awayTeam"], game["homeTeam"]
name = lambda t: f"{t.get('teamCity', '')} {t.get('teamName', '')}".strip()
st.subheader(f"{name(away)} {away.get('score', '–')} — {home.get('score', '–')} {name(home)}")
st.caption(f"Game ID: {game_id} · Status: {game.get('gameStatusText') or game.get('gameStatus', 'Unknown')}")

away_tab, home_tab = st.tabs([name(away), name(home)])
for tab, team in ((away_tab, away), (home_tab, home)):
    with tab:
        df = team_table(team)
        if df.empty:
            st.info("No player box-score records are available yet for this team.")
            continue
        sort = st.selectbox("Sort players by", ["MIN", "PTS", "USG% (Est.)", "REB", "AST", "Player"], key=str(team.get("teamId")))
        df = df.sort_values(sort, ascending=(sort == "Player"), na_position="last")
        st.dataframe(df, use_container_width=True, hide_index=True, column_config={"USG% (Est.)": st.column_config.NumberColumn("USG% (Est.)", format="%.1f%%")})
        st.download_button("Download team box score (CSV)", df.to_csv(index=False).encode("utf-8"), file_name=f"nba_{game_id}_{team.get('teamTricode', 'team')}.csv", mime="text/csv", key=f"dl_{team.get('teamId')}")

st.info("**USG% (Est.)** is calculated from field-goal attempts, free-throw attempts, turnovers, player minutes and team totals. It is an estimate, not the NBA's official usage percentage. Players with no recorded minutes show a blank value.")
st.caption("Data source: NBA Live Data API (cdn.nba.com). This unofficial demo depends on NBA endpoint availability; it may be unavailable from some hosting networks.")
