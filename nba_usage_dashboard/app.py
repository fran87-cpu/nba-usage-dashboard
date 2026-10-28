"""Free ESPN NBA box scores with estimated usage rate. No API key needed."""
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
from html import escape
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="NBA Box Score + Usage", page_icon="🏀", layout="wide")
BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
CDN = "https://cdn.espn.com/core/nba/boxscore"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36", "Accept": "application/json"}


def current_nba_season_year(today=None):
    """ESPN identifies an NBA season by its ending calendar year."""
    today = today or date.today()
    return today.year + 1 if today.month >= 10 else today.year


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
                "_espn_player_id": str(athlete.get("id") or ""),
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
    # Custom fantasy scoring supplied by the user; applied consistently
    # to box scores, daily rankings, and previous-game averages.
    df["FPTS"] = df.apply(fantasy_points, axis=1).round(1)
    # Position fantasy points immediately before estimated usage.
    cols = list(df.columns)
    cols.remove("FPTS")
    cols.insert(cols.index("USG% (Est.)"), "FPTS")
    df = df[cols]
    # A player with no recorded playing time is DNP. Hide all numeric/stat
    # fields in that player's row, while retaining their name and position.
    # Apply this AFTER usage calculations so team totals remain numeric.
    dnp = df["_minutes_numeric"].eq(0)
    stat_columns = [c for c in df.columns if c not in ("Starter", "Player", "MIN", "_minutes_numeric", "_espn_player_id")]
    df.loc[dnp, "MIN"] = "DNP"
    df.loc[dnp, stat_columns] = ""
    return df.drop(columns="_minutes_numeric")


@st.cache_data(ttl=1800, show_spinner=False)
def daily_top_50(day):
    """All completed/available box scores for the selected NBA calendar day."""
    events = games_on(day)
    ids = [str(e["id"]) for e in events if e.get("id")]
    results, errors = [], []
    def load(event_id):
        box, _ = boxscore(event_id)
        rows = []
        for group in box.get("players", []):
            team = (group.get("team") or {}).get("abbreviation", "")
            for r in athlete_rows(group):
                if r["_minutes_numeric"] > 0 and r["_espn_player_id"]:
                    r["Team"] = team
                    r["Game ID"] = event_id
                    r["FPTS"] = fantasy_points(r)
                    r["FPTS/36"] = round(r["FPTS"] * 36 / r["_minutes_numeric"], 1)
                    rows.append(r)
        return rows
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(load, eid): eid for eid in ids}
        for future in as_completed(futures):
            try:
                results.extend(future.result())
            except (requests.RequestException, ValueError, RuntimeError, KeyError, TypeError) as exc:
                errors.append(f"{futures[future]}: {exc}")
    results.sort(key=lambda r: r["FPTS"], reverse=True)
    return results[:50], errors, len(ids)


def fantasy_points(r):
    """Custom fantasy score: PTS + 1.2 REB + 1.5 AST + 3 STL + 3 BLK
    - 2 TOV - 2 PF + 3PM + FGM - FGA + FTM - FTA.
    """
    return float(r["PTS"] + 1.2*r["REB"] + 1.5*r["AST"]
                 + 3*r["STL"] + 3*r["BLK"] - 2*r["TOV"] - 2*r["PF"]
                 + r["3PM"] + r["FGM"] - r["FGA"]
                 + r["FTM"] - r["FTA"])


def is_non_preseason_event(event):
    """Exclude ESPN preseason (season.type=1) from historical fantasy averages.

    Keep unknown classifications rather than incorrectly dropping regular-season games.
    """
    season = event.get("season") or {}
    season_type = season.get("type") if isinstance(season, dict) else None
    if isinstance(season_type, dict):
        season_type = season_type.get("id") or season_type.get("type")
    return str(season_type).strip().lower() not in {"1", "preseason", "pre-season"}


@st.cache_data(ttl=3600, show_spinner=False)
def recent_fantasy_history(day, player_ids, team_abbreviations):
    """Look back 21 calendar days for up to 5 prior non-preseason appearances.

    Fetch scoreboard indexes then only relevant games; cache both underlying requests.
    """
    wanted = set(player_ids)
    teams = set(team_abbreviations)
    history = {pid: [] for pid in wanted}
    days = [day - timedelta(days=i) for i in range(1, 22)]
    def get_day(d):
        try:
            return d, games_on(d)
        except (requests.RequestException, ValueError):
            return d, []
    with ThreadPoolExecutor(max_workers=8) as pool:
        days_events = list(pool.map(get_day, days))
    # Check the newest events first. Limit to games involving today's top-50 teams.
    # ESPN athlete IDs are the join key, not names.
    event_ids = []
    seen = set()
    for d, events in sorted(days_events, reverse=True):
        for event in events:
            eid = str(event.get("id") or "")
            competitors = ((event.get("competitions") or [{}])[0].get("competitors") or [])
            participating = {str((c.get("team") or {}).get("abbreviation", "")) for c in competitors}
            if eid and eid not in seen and participating.intersection(teams) and is_non_preseason_event(event):
                seen.add(eid)
                event_ids.append((d, eid))
    def get_event(item):
        d, eid = item
        try:
            box, _ = boxscore(eid)
            rows = []
            for group in box.get("players", []):
                for r in athlete_rows(group):
                    pid = r["_espn_player_id"]
                    if pid in wanted and r["_minutes_numeric"] > 0:
                        rows.append((pid, d, fantasy_points(r)))
            return rows
        except (requests.RequestException, ValueError, RuntimeError, KeyError, TypeError):
            return []
    with ThreadPoolExecutor(max_workers=10) as pool:
        for rows in pool.map(get_event, event_ids):
            for pid, d, fpts in rows:
                history[pid].append((d, fpts))
    return {pid: [score for _, score in sorted(games, reverse=True)[:5]]
            for pid, games in history.items()}


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
st.caption("Version 4.9 (based on 3.8) · Free ESPN data · ESPN player positions · Starters first · MM:SS minutes · Estimated USG% · No API key required")
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

# Hover details use the same 21-day, non-preseason history as the Top 50.
# Load once for both teams and cache the result; no requests occur on mouse hover.
hover_ids = tuple(sorted({r["_espn_player_id"] for g in team_groups
                          for r in athlete_rows(g)
                          if r["_espn_player_id"] and r["_minutes_numeric"] > 0}))
hover_teams = tuple(sorted({str((g.get("team") or {}).get("abbreviation", ""))
                            for g in team_groups if (g.get("team") or {}).get("abbreviation")}))
hover_history = {}
if hover_ids and hover_teams:
    try:
        with st.spinner("Preparing fantasy hover details (first load may take a moment)..."):
            hover_history = recent_fantasy_history(selected_date, hover_ids, hover_teams)
    except Exception:
        st.caption("Historical hover data is temporarily unavailable; current-game fantasy points remain visible.")

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
        sort = st.selectbox("Sort by", ["FPTS", "MIN", "PTS", "USG% (Est.)", "REB", "AST", "Player"], key=f"sort_{i}")
        # Keep ESPN-confirmed starters at the top on initial load and when changing sort options.
        df = df.assign(_starter_first=df["Starter"].eq("★"))
        # MM:SS sorts lexicographically for varying minute widths; convert to seconds.
        # Keep the human-readable MIN column untouched in the display and CSV.
        if sort == "MIN":
            df["_sort_minutes"] = df["MIN"].map(
                lambda v: float("nan") if v in ("—", "DNP") else minutes(v)
            )
        # DNP cells are blank strings: convert numeric sorts safely to avoid
        # comparing strings with floats when sorting a team with DNP players.
        if sort not in ("MIN", "Player"):
            df["_sort_numeric"] = pd.to_numeric(df[sort], errors="coerce")
        sort_column = "_sort_minutes" if sort == "MIN" else ("Player" if sort == "Player" else "_sort_numeric")
        df = df.sort_values(
            ["_starter_first", sort_column],
            ascending=[False, sort == "Player"],
            na_position="last",
            kind="stable",
        ).drop(columns=["_starter_first", "_sort_minutes", "_sort_numeric"], errors="ignore")
        # Streamlit's dataframe grid cannot apply a smaller font to only part of a
        # cell. Render a safe, scrollable HTML table so the ESPN position can be
        # visually secondary while retaining starter highlighting and Sort by.
        def display_cell(column, value, player_id="", player_minutes=""):
            if column == "Player":
                name, sep, role = str(value).rpartition(" (")
                if sep and role.endswith(")") and re.fullmatch(r"[A-Za-z]{1,4}", role[:-1]):
                    label = f'{escape(name)} <span class="nba-role">({escape(role[:-1])})</span>'
                else:
                    label = escape(str(value))
                # ESPN's player ID is authoritative; no name-based guessing.
                if str(player_id).isdigit():
                    url = f"https://www.espn.com/nba/player/gamelog/_/id/{player_id}/type/nba/year/{current_nba_season_year()}"
                    return f'<a class="nba-player-link" href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{label}</a>'
                return label
            if value == "":
                return ""
            if pd.isna(value):
                return "—"
            if column == "FPTS":
                score = float(value)
                prior = hover_history.get(str(player_id), [])
                last_five = f"{sum(prior) / len(prior):.1f} ({len(prior)} games)" if prior else "No eligible prior games"
                played = minutes(player_minutes)
                rate = f"{score * 36 / played:.1f}" if played > 0 else "N/A"
                tooltip = f"FPTS L5: {last_five} | FPTS/36: {rate}"
                return (f'<span class="nba-fpts-hover" title="{escape(tooltip, quote=True)}" '
                        f'tabindex="0" aria-label="custom fantasy points {score:.1f}; {escape(tooltip, quote=True)}">'
                        f'{score:.1f}</span>')
            if column in ("FG%", "USG% (Est.)"):
                return f"{float(value):.1f}%"
            return escape(str(value))

        # Internal ESPN athlete ID is used only for player links, not shown as a column.
        display_columns = [c for c in df.columns if c != "_espn_player_id"]

        # Group related stats with subtle vertical dividers after selected columns.
        # Apply the same class to headers and body cells, regardless of column order.
        divider_after = {"MIN", "PTS", "FG%", "3PA", "FTA", "REB", "AST", "BLK", "PF"}
        headers = "".join(
            f'<th class="{"nba-group-end" if c in divider_after else ""}">{escape(str(c))}</th>'
            for c in display_columns
        )
        body_rows = []
        for _, player_row in df.iterrows():
            css_class = ' class="nba-starter"' if player_row["Starter"] == "★" else ""
            cells = "".join(
                f'<td class="{" ".join(x for x in ("nba-player" if col == "Player" else "", "nba-group-end" if col in divider_after else "") if x)}">{display_cell(col, player_row[col], player_row.get("_espn_player_id", ""), player_row.get("MIN", ""))}</td>'
                for col in display_columns
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
          .nba-box a.nba-player-link {{ color: inherit; text-decoration: none; cursor: pointer; }}
          .nba-box a.nba-player-link:hover {{ color: #1768bd; text-decoration: underline; }}
          .nba-box .nba-fpts-hover {{ cursor: help; border-bottom: 1px dotted #718096; }}
          .nba-box .nba-fpts-hover:focus {{ outline: 2px solid #1768bd; outline-offset: 2px; }}
          /* Slightly stronger than row rules, without crowding the compact box score. */
          .nba-box th.nba-group-end, .nba-box td.nba-group-end {{ border-right: 2px solid #aeb8c4; }}
        </style>
        <div class="nba-scroll"><table class="nba-box"><thead><tr>{headers}</tr></thead>
        <tbody>{''.join(body_rows)}</tbody></table></div>
        """
        st.html(table_html)
        st.download_button("Download CSV", data=df.drop(columns=["_espn_player_id"], errors="ignore").to_csv(index=False).encode("utf-8"),
                           file_name=f"espn_{selected_id}_team{i+1}.csv", mime="text/csv", key=f"download_{i}")

st.divider()
with st.expander("🏆 Top 50 Fantasy Performers of the Day · Custom FPTS", expanded=False):
    st.caption("Ranks all players with recorded minutes across games on the selected date. "
               "FPTS L5 averages up to five *previous* non-preseason appearances (excluding the selected day). "
               "FPTS/36 is the selected game's rate, not a projection.")
    # Keep the expensive league-wide requests behind a button inside the expander.
    if st.button("Load top 50 for selected date", key="load_daily_top50"):
        st.session_state["daily_top50_date"] = selected_date
    if st.session_state.get("daily_top50_date") == selected_date:
        with st.spinner("Collecting the day's NBA box scores..."):
            leaders, failed, game_count = daily_top_50(selected_date)
        if failed:
            st.warning(f"ESPN data was unavailable for {len(failed)} of {game_count} games. Rankings may be incomplete.")
        if not leaders:
            st.info("No player box scores available for this date.")
        else:
            with st.spinner("Calculating last-five-game averages from prior box scores (first load may take a while)..."):
                ids = tuple(sorted({r["_espn_player_id"] for r in leaders}))
                history = recent_fantasy_history(selected_date, ids, tuple(sorted({r["Team"] for r in leaders})))
            display = []
            for rank, r in enumerate(leaders, 1):
                past = history.get(r["_espn_player_id"], [])
                display.append({
                    "Rank": rank, "Player": r["Player"], "Team": r["Team"],
                    "MIN": r["MIN"], "FPTS": r["FPTS"],
                    "FPTS L5": round(sum(past)/len(past), 1) if past else None,
                    "L5 GP": len(past), "FPTS/36": r["FPTS/36"],
                })
            leaders_df = pd.DataFrame(display)
            st.dataframe(leaders_df, hide_index=True, use_container_width=True,
                         column_config={"FPTS": st.column_config.NumberColumn(format="%.1f"),
                                        "FPTS L5": st.column_config.NumberColumn(format="%.1f"),
                                        "FPTS/36": st.column_config.NumberColumn(format="%.1f")})
            st.download_button("Download top 50 CSV", leaders_df.to_csv(index=False).encode("utf-8"),
                               file_name=f"nba_top50_fpts_{selected_date:%Y%m%d}.csv", mime="text/csv")
            st.caption("L5 GP shows how many prior non-preseason appearances were found in the 21-day lookback. "
                       "If fewer than five, the average uses the available games. "
                       "Missing historical games or ESPN outages can affect the result.")

st.caption("Hover over an FPTS value to see FPTS L5 and FPTS/36. FPTS L5 uses up to five prior non-preseason appearances within 21 days; the game being viewed is excluded. The first historical lookup may take longer. ")
st.caption("Click a player's name to open their ESPN Game Log in a new tab. Players without an ESPN ID remain unlinked.")
st.caption("Positions use ESPN game data first, then ESPN’s season-specific team roster. ESPN may omit some detailed roles; these are left blank rather than guessed.")
st.caption("**FPTS (custom)** = PTS + 1.2×REB + 1.5×AST + 3×STL + 3×BLK − 2×TOV − 2×PF + 3PM + FGM − FGA + FTM − FTA.")
st.caption("**FG%** = 100 × FGM / FGA. A blank percentage means the player attempted no field goals.")
st.info("**USG% (Est.)** = 100 × (player FGA + 0.44 × FTA + TOV) × (team player-minutes / 5) ÷ [player minutes × (team FGA + 0.44 × FTA + TOV)]. This is an estimate, **not** official NBA on-court usage. Players with no minutes have no estimate. Team totals are computed from the player rows supplied by ESPN.")
st.caption("ESPN endpoints are unofficial for third-party use and can change or restrict access. This project is not affiliated with ESPN or the NBA.")
