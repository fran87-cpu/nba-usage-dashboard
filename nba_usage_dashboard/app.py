"""Free ESPN NBA box scores with estimated usage rate. No API key needed."""
from datetime import date
import re

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="NBA Box Score + Usage", page_icon="🏀", layout="wide")
BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
CDN = "https://cdn.espn.com/core/nba/boxscore"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36", "Accept": "application/json"}


def get_json(url, params):
    response = requests.get(url, params=params, headers=HEADERS, timeout=18)
    response.raise_for_status()
    return response.json()


@st.cache_data(ttl=600, show_spinner=False)
def games_on(day):
    payload = get_json(f"{BASE}/scoreboard", {"dates": day.strftime("%Y%m%d"), "limit": 100})
    return payload.get("events", [])


def locate_boxscore(payload):
    """Find ESPN's boxscore across site API and CDN response wrappers."""
    if not isinstance(payload, dict):
        return None
    box = payload.get("boxscore")
    if isinstance(box, dict) and isinstance(box.get("players"), list):
        return box
    for key in ("gamepackageJSON", "content", "page", "data"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            found = locate_boxscore(nested)
            if found:
                return found
    return None


@st.cache_data(ttl=600, show_spinner=False)
def boxscore(event_id):
    errors = []
    for url, params, source in [
        (f"{BASE}/summary", {"event": event_id}, "ESPN game summary"),
        (CDN, {"xhr": "1", "gameId": event_id}, "ESPN CDN"),
    ]:
        try:
            result = locate_boxscore(get_json(url, params))
            if result and result.get("players"):
                return result, source
            errors.append(f"{source}: no player box-score data in response")
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{source}: {exc}")
    raise RuntimeError(" | ".join(errors))


def number(value):
    if value in (None, "", "--", "-", "DNP", "N/A"):
        return 0.0
    try:
        return float(str(value).replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


def minutes(value):
    if value is None or str(value).strip() in ("", "--", "DNP"):
        return 0.0
    value = str(value).strip()
    if ":" in value:
        try:
            m, s = value.split(":", 1)
            return float(m) + float(s) / 60
        except ValueError:
            return 0.0
    return number(value)


def minutes_display(value):
    """Show the time ESPN provides as MM:SS; do not invent sub-minute precision."""
    if value is None or str(value).strip() in ("", "--", "DNP"):
        return "—"
    raw = str(value).strip()
    if ":" in raw:
        parts = raw.split(":")
        if len(parts) == 2:
            try:
                mins, secs = int(parts[0]), int(parts[1])
                if mins >= 0 and 0 <= secs < 60:
                    return f"{mins:02d}:{secs:02d}"
            except ValueError:
                pass
        return raw
    try:
        total_seconds = round(float(raw) * 60)
        if total_seconds < 0:
            return "—"
        mins, secs = divmod(total_seconds, 60)
        return f"{mins:02d}:{secs:02d}"
    except ValueError:
        return "—"


def split_shots(value):
    if isinstance(value, str) and "-" in value:
        a, b = value.split("-", 1)
        return int(number(a)), int(number(b))
    return 0, 0


def normalize_key(value):
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def player_position(athlete, entry):
    """Read ESPN's own position abbreviation without inferring a role."""
    for source in (athlete, entry):
        position = source.get("position")
        if isinstance(position, dict):
            abbreviation = position.get("abbreviation") or position.get("shortName")
        elif isinstance(position, str):
            abbreviation = position
        else:
            abbreviation = None
        if abbreviation:
            abbreviation = str(abbreviation).strip().upper()
            if abbreviation in {"PG", "SG", "SF", "PF", "C"}:
                return abbreviation
    return None


def athlete_rows(group):
    """ESPN statistics contain parallel labels/names and per-athlete stats arrays."""
    rows = []
    for section in group.get("statistics", []):
        keys = section.get("names") or section.get("labels") or []
        labels = section.get("labels") or []
        for entry in section.get("athletes", []):
            athlete = entry.get("athlete") or {}
            stats = entry.get("stats") or []
            values = {}
            for idx, value in enumerate(stats):
                if idx < len(keys):
                    values[normalize_key(keys[idx])] = value
                if idx < len(labels):
                    values[normalize_key(labels[idx])] = value
            def pick(*aliases):
                for alias in aliases:
                    k = normalize_key(alias)
                    if k in values:
                        return values[k]
                return None
            fgm, fga = split_shots(pick("FG", "fieldGoals"))
            tpm, tpa = split_shots(pick("3PT", "3P", "threePointFieldGoals"))
            ftm, fta = split_shots(pick("FT", "freeThrows"))
            raw_minutes = pick("MIN", "minutes")
            min_value = minutes(raw_minutes)
            # ESPN marks starting lineup members with starter=true on the athlete entry.
            # Do not guess from minutes or row order if ESPN omits this field.
            starter_value = entry.get("starter", entry.get("starting"))
            if starter_value is None:
                starter_value = athlete.get("starter", athlete.get("starting"))
            is_starter = starter_value is True or str(starter_value).lower() == "true"
            player_name = athlete.get("displayName") or athlete.get("fullName") or "Unknown"
            position = player_position(athlete, entry)
            if position:
                player_name = f"{player_name} ({position})"
            rows.append({
                "Starter": "★" if is_starter else "",
                "Player": player_name,
                "MIN": minutes_display(raw_minutes), "_minutes_numeric": min_value, "PTS": int(number(pick("PTS", "points"))),
                "FGM": fgm, "FGA": fga, "3PM": tpm, "3PA": tpa,
                "FTM": ftm, "FTA": fta,
                "OREB": int(number(pick("OREB", "offensiveRebounds"))),
                "DREB": int(number(pick("DREB", "defensiveRebounds"))),
                "REB": int(number(pick("REB", "totalRebounds"))),
                "AST": int(number(pick("AST", "assists"))),
                "STL": int(number(pick("STL", "steals"))),
                "BLK": int(number(pick("BLK", "blocks"))),
                "TOV": int(number(pick("TO", "TOV", "turnovers"))),
                "PF": int(number(pick("PF", "personalFouls", "fouls"))),
                "+/-": pick("+/-", "plusMinus"),
            })
    return rows


def shooting_percentage(made, attempts):
    """Return a percentage, or NA when no attempts were recorded."""
    return (100.0 * made / attempts.where(attempts.ne(0), float("nan"))).round(1)


def team_table(group):
    df = pd.DataFrame(athlete_rows(group))
    if df.empty:
        return df
    # Field-goal percentage only; undefined when the player attempted no shots.
    df["FG%"] = shooting_percentage(df["FGM"], df["FGA"])
    cols = list(df.columns)
    cols.remove("FG%")
    cols.insert(cols.index("FGA") + 1, "FG%")
    df = df[cols]
    # Sum player statistics to calculate team totals. This avoids mismatched
    # ESPN team-stat label formats and works for completed regulation/OT games.
    team_minutes = df["_minutes_numeric"].sum()
    team_actions = df["FGA"].sum() + 0.44 * df["FTA"].sum() + df["TOV"].sum()
    individual_actions = df["FGA"] + 0.44 * df["FTA"] + df["TOV"]
    if team_minutes > 0 and team_actions > 0:
        usage = 100 * individual_actions * (team_minutes / 5) / (df["_minutes_numeric"].replace(0, float("nan")) * team_actions)
        df["USG% (Est.)"] = usage.round(1)
    else:
        df["USG% (Est.)"] = float("nan")
    return df.drop(columns="_minutes_numeric")


def game_label(event):
    competition = (event.get("competitions") or [{}])[0]
    teams = competition.get("competitors") or []
    away = next((t for t in teams if t.get("homeAway") == "away"), {})
    home = next((t for t in teams if t.get("homeAway") == "home"), {})
    def name(team):
        return (team.get("team") or {}).get("displayName", "Unknown")
    status = (event.get("status") or {}).get("type") or {}
    return f"{name(away)} @ {name(home)} — {status.get('shortDetail') or status.get('description') or 'Unknown'}"


st.title("🏀 NBA Box Score + Player Usage")
st.caption("Version 3.6 · Free ESPN data · ESPN player positions · Starters first · MM:SS minutes · Estimated USG% · No API key required")
selected_date = st.date_input("Select game date", value=date(2026, 10, 20), min_value=date(2000, 1, 1))
try:
    with st.spinner("Loading games from ESPN..."):
        events = games_on(selected_date)
except (requests.RequestException, ValueError) as exc:
    st.error(f"Could not retrieve ESPN's scoreboard: {exc}")
    st.info("The scoreboard may be temporarily unavailable from Streamlit Cloud. Try another date or reload later.")
    st.stop()

if not events:
    st.info("No NBA games were returned for this date. Try another date.")
    st.stop()

choices = {str(e.get("id")): game_label(e) for e in events if e.get("id")}
selected_id = st.selectbox("Choose a game", options=list(choices), format_func=lambda k: choices[k])
if not selected_id:
    st.stop()

try:
    with st.spinner("Loading player box scores..."):
        box, source = boxscore(selected_id)
except RuntimeError as exc:
    st.error("ESPN did not provide a usable player box score for this game.")
    st.code(str(exc))
    st.info("Try a completed game. If the game has finished and this persists, the ESPN endpoints may be blocked from Streamlit Cloud.")
    st.stop()

st.caption(f"ESPN event ID: {selected_id} · Player data source: {source}")
team_groups = box.get("players", [])
if not team_groups:
    st.warning("No player statistics were published for this game yet.")
    st.stop()

tabs = st.tabs([(g.get("team") or {}).get("displayName", f"Team {i+1}") for i, g in enumerate(team_groups)])
for i, (tab, group) in enumerate(zip(tabs, team_groups)):
    with tab:
        name = (group.get("team") or {}).get("displayName", f"Team {i+1}")
        df = team_table(group)
        if df.empty:
            st.info(f"No player stats available for {name}.")
            continue
        starters_found = int((df["Starter"] == "★").sum())
        if starters_found == 5:
            st.caption("★ = starting lineup · Starter rows are highlighted in light yellow and displayed first.")
        else:
            st.caption(f"★ = ESPN-confirmed starter. ESPN marked {starters_found} of 5 starters for this team; unmarked players are not inferred.")
        sort = st.selectbox("Sort by", ["MIN", "PTS", "USG% (Est.)", "REB", "AST", "Player"], key=f"sort_{i}")
        # Keep ESPN-confirmed starters at the top on initial load and when changing sort options.
        df = df.assign(_starter_first=df["Starter"].eq("★"))
        # MM:SS sorts lexicographically for varying minute widths; convert to seconds.
        # Keep the human-readable MIN column untouched in the display and CSV.
        if sort == "MIN":
            df["_sort_minutes"] = df["MIN"].map(
                lambda v: minutes(v) if v != "—" else float("nan")
            )
        sort_column = "_sort_minutes" if sort == "MIN" else sort
        df = df.sort_values(
            ["_starter_first", sort_column],
            ascending=[False, sort == "Player"],
            na_position="last",
            kind="stable",
        ).drop(columns=["_starter_first", "_sort_minutes"], errors="ignore")
        def highlight_starters(row):
            shade = "background-color: #fff1b8; color: #262626;" if row["Starter"] == "★" else ""
            return [shade] * len(row)

        styled_df = df.style.apply(highlight_starters, axis=1)
        st.dataframe(styled_df, use_container_width=True, hide_index=True, column_config={
            "USG% (Est.)": st.column_config.NumberColumn("USG% (Est.)", format="%.1f%%"),
            "FG%": st.column_config.NumberColumn("FG%", format="%.1f%%")
        })
        st.download_button("Download CSV", data=df.to_csv(index=False).encode("utf-8"),
                           file_name=f"espn_{selected_id}_team{i+1}.csv", mime="text/csv", key=f"download_{i}")

st.caption("Player positions (PG, SG, SF, PF, C) are shown beside names when provided by ESPN; missing positions are not inferred.")
st.caption("**FG%** = 100 × FGM / FGA. A blank percentage means the player attempted no field goals.")
st.info("**USG% (Est.)** = 100 × (player FGA + 0.44 × FTA + TOV) × (team player-minutes / 5) ÷ [player minutes × (team FGA + 0.44 × FTA + TOV)]. This is an estimate, **not** official NBA on-court usage. Players with no minutes have no estimate. Team totals are computed from the player rows supplied by ESPN.")
st.caption("ESPN endpoints are unofficial for third-party use and can change or restrict access. This project is not affiliated with ESPN or the NBA.")

