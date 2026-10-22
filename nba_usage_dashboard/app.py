"""Free ESPN NBA box scores with estimated usage rate. No API key needed."""
from datetime import date, timedelta
import re
from html import escape

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


@st.cache_data(ttl=86400, show_spinner=False)
def roster_positions(team_id, season):
    """Look up detailed ESPN positions from the team's season-specific roster."""
    if not team_id:
        return {}
    try:
        payload = get_json(f"{BASE}/teams/{team_id}/roster", {"season": season})
    except (requests.RequestException, ValueError):
        return {}
    positions = {}
    groups = payload.get("athletes") or []
    for group in groups:
        entries = group.get("items", []) if isinstance(group, dict) else []
        if isinstance(group, dict) and group.get("id"):
            entries = [group]
        for athlete in entries:
            if not isinstance(athlete, dict):
                continue
            role = player_position(athlete, {})
            if role and athlete.get("id") is not None:
                positions[str(athlete["id"])] = role
    return positions


def athlete_rows(group, roster=None):
    roster = roster or {}
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
            position = player_position(athlete, entry) or roster.get(str(athlete.get("id", "")))
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


def team_table(group, roster=None):
    df = pd.DataFrame(athlete_rows(group, roster))
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
    # A player with no recorded playing time is DNP. Hide all numeric/stat
    # fields in that player's row, while retaining their name and position.
    # Apply this AFTER usage calculations so team totals remain numeric.
    dnp = df["_minutes_numeric"].eq(0)
    stat_columns = [c for c in df.columns if c not in ("Starter", "Player", "MIN", "_minutes_numeric")]
    df.loc[dnp, "MIN"] = "DNP"
    df.loc[dnp, stat_columns] = ""
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


def show_game_score(event):
    """Render final/current score and period breakdown from ESPN scoreboard."""
    competition = (event.get("competitions") or [{}])[0]
    competitors = competition.get("competitors") or []
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    if not away or not home:
        st.info("Score information is unavailable for this game.")
        return

    def team_name(c):
        t = c.get("team") or {}
        return t.get("abbreviation") or t.get("shortDisplayName") or t.get("displayName") or "Team"

    def periods(c):
        values = {}
        for index, item in enumerate(c.get("linescores") or [], start=1):
            if not isinstance(item, dict):
                continue
            period = item.get("period") or index
            try:
                period = int(period)
            except (ValueError, TypeError):
                continue
            val = item.get("displayValue", item.get("value"))
            if val is not None and str(val).strip() != "":
                try:
                    values[period] = str(int(float(val)))
                except (ValueError, TypeError):
                    values[period] = str(val)
        return values

    status = (event.get("status") or {}).get("type") or {}
    completed = status.get("completed") is True or status.get("name") == "STATUS_FINAL"
    state = str(status.get("state") or "").lower()
    label = "Final" if completed else (status.get("shortDetail") or status.get("description") or "Game status unavailable")
    away_periods, home_periods = periods(away), periods(home)
    highest = max([4] + list(away_periods) + list(home_periods))
    period_labels = [f"Q{i}" for i in range(1, 5)] + [f"OT{i-4}" for i in range(5, highest + 1)]
    columns = ["Team"] + period_labels + ["Total"]

    def row(c, data):
        score = c.get("score")
        if isinstance(score, dict):
            score = score.get("displayValue", score.get("value"))
        if score is None or str(score).strip() == "":
            score = "—"
        return [team_name(c)] + [data.get(i, "—") for i in range(1, highest + 1)] + [str(score)]

    st.markdown(f"**{'Final score' if completed else 'Game score'} · {escape(str(label))}**")
    score_df = pd.DataFrame([row(away, away_periods), row(home, home_periods)], columns=columns)
    # Static table: the period order and team order must remain fixed.
    st.table(score_df.set_index("Team"))
    if not away_periods and not home_periods:
        st.caption("ESPN has not supplied a quarter-by-quarter breakdown for this game.")
    elif not completed and state == "pre":
        st.caption("This game has not started; scores will appear when ESPN publishes them.")


st.title("🏀 NBA Box Score + Player Usage")
st.caption("Version 4.3 (based on 3.8) · Free ESPN data · ESPN player positions · Starters first · MM:SS minutes · Estimated USG% · No API key required")
selected_date = st.date_input("Select game date", value=date.today() - timedelta(days=1), min_value=date(2000, 1, 1))
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

selected_event = next((e for e in events if str(e.get("id")) == selected_id), None)
if selected_event:
    show_game_score(selected_event)

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
        team_id = (group.get("team") or {}).get("id")
        # NBA season uses the year of the spring playoffs (e.g. Oct 2025 -> 2026).
        season = selected_date.year + (1 if selected_date.month >= 10 else 0)
        roster = roster_positions(str(team_id), season) if team_id else {}
        df = team_table(group, roster)
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
                lambda v: float("nan") if v in ("—", "DNP") else minutes(v)
            )
        sort_column = "_sort_minutes" if sort == "MIN" else sort
        df = df.sort_values(
            ["_starter_first", sort_column],
            ascending=[False, sort == "Player"],
            na_position="last",
            kind="stable",
        ).drop(columns=["_starter_first", "_sort_minutes"], errors="ignore")
        # Streamlit's dataframe grid cannot apply a smaller font to only part of a
        # cell. Render a safe, scrollable HTML table so the ESPN position can be
        # visually secondary while retaining starter highlighting and Sort by.
        def display_cell(column, value):
            if value == "":
                return ""
            if pd.isna(value):
                return "—"
            if column == "Player":
                name, sep, role = str(value).rpartition(" (")
                if sep and role.endswith(")") and re.fullmatch(r"[A-Za-z]{1,4}", role[:-1]):
                    return f'{escape(name)} <span class="nba-role">({escape(role[:-1])})</span>'
            if column in ("FG%", "USG% (Est.)"):
                return f"{float(value):.1f}%"
            return escape(str(value))

        # Group related stats with subtle vertical dividers after selected columns.
        # Apply the same class to headers and body cells, regardless of column order.
        divider_after = {"MIN", "PTS", "FG%", "3PA", "FTA", "REB", "AST", "BLK", "PF"}
        headers = "".join(
            f'<th class="{"nba-group-end" if c in divider_after else ""}">{escape(str(c))}</th>'
            for c in df.columns
        )
        body_rows = []
        for _, player_row in df.iterrows():
            css_class = ' class="nba-starter"' if player_row["Starter"] == "★" else ""
            cells = "".join(
                f'<td class="{" ".join(x for x in ("nba-player" if col == "Player" else "", "nba-group-end" if col in divider_after else "") if x)}">{display_cell(col, player_row[col])}</td>'
                for col in df.columns
            )
            body_rows.append(f"<tr{css_class}>{cells}</tr>")
        table_html = f"""
        <style>
          .nba-scroll {{ overflow-x: auto; max-width: 100%; }}
          .nba-box {{ border-collapse: collapse; width: 100%; font-size: 0.88rem; white-space: nowrap; }}
          .nba-box th, .nba-box td {{ padding: 8px 10px; border-bottom: 1px solid #ddd; text-align: right; }}
          .nba-box th {{ background: #f1f3f5; color: #252525; font-weight: 650; }}
          .nba-box th:nth-child(2), .nba-box td:nth-child(2) {{ text-align: left; }}
          .nba-box td {{ color: inherit; }}
          .nba-box .nba-starter td {{ background: #fff1b8; color: #262626; }}
          .nba-box .nba-role {{ font-size: 0.78em; opacity: 0.75; font-weight: 400; }}
          .nba-box .nba-player {{ font-weight: 550; }}
          /* Slightly stronger than row rules, without crowding the compact box score. */
          .nba-box th.nba-group-end, .nba-box td.nba-group-end {{ border-right: 2px solid #aeb8c4; }}
        </style>
        <div class="nba-scroll"><table class="nba-box"><thead><tr>{headers}</tr></thead>
        <tbody>{''.join(body_rows)}</tbody></table></div>
        """
        st.html(table_html)
        st.download_button("Download CSV", data=df.to_csv(index=False).encode("utf-8"),
                           file_name=f"espn_{selected_id}_team{i+1}.csv", mime="text/csv", key=f"download_{i}")

st.caption("Positions use ESPN game data first, then ESPN’s season-specific team roster. ESPN may omit some detailed roles; these are left blank rather than guessed.")
st.caption("**FG%** = 100 × FGM / FGA. A blank percentage means the player attempted no field goals.")
st.info("**USG% (Est.)** = 100 × (player FGA + 0.44 × FTA + TOV) × (team player-minutes / 5) ÷ [player minutes × (team FGA + 0.44 × FTA + TOV)]. This is an estimate, **not** official NBA on-court usage. Players with no minutes have no estimate. Team totals are computed from the player rows supplied by ESPN.")
st.caption("ESPN endpoints are unofficial for third-party use and can change or restrict access. This project is not affiliated with ESPN or the NBA.")
