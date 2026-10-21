import re
import pandas as pd
import streamlit as st
from nba_api.stats.endpoints import boxscoretraditionalv3, boxscoreadvancedv3

st.set_page_config(page_title='NBA Box Score + Usage', layout='wide')
st.title('NBA Box Score + Player Usage')
st.caption('Traditional NBA box score enhanced with official USG% (when available).')

@st.cache_data(ttl=900, show_spinner=False)
def get_box_score(game_id):
    traditional = boxscoretraditionalv3.BoxScoreTraditionalV3(game_id=game_id, timeout=25)
    base = traditional.player_stats.get_data_frame()
    try:
        advanced = boxscoreadvancedv3.BoxScoreAdvancedV3(game_id=game_id, timeout=25)
        usage = advanced.player_stats.get_data_frame()[['gameId','teamId','personId','usagePercentage']]
        base = base.merge(usage, on=['gameId','teamId','personId'], how='left', validate='one_to_one')
        note = None
    except Exception as exc:
        base['usagePercentage'] = float('nan')
        note = f'Advanced stats could not be loaded ({type(exc).__name__}). Usage is shown as unavailable.'
    return base, note

def ratio(made, attempted):
    if pd.isna(attempted) or attempted == 0:
        return '—'
    return f'{100 * made / attempted:.1f}%'

def pair(made, attempted):
    return f'{int(made)}-{int(attempted)}' if pd.notna(made) and pd.notna(attempted) else '—'

game_id = st.text_input('NBA Game ID', value='0022600001', max_chars=10)
if not re.fullmatch(r'\d{10}', game_id):
    st.info('Enter a 10-digit NBA game ID.')
    st.stop()

try:
    with st.spinner('Fetching official NBA box scores...'):
        df, note = get_box_score(game_id)
except Exception as exc:
    st.error(f'NBA data could not be retrieved: {type(exc).__name__}: {exc}')
    st.info('The NBA Stats API may block requests from some hosting environments. Try running locally or using an authorized alternative provider.')
    st.stop()

if df.empty:
    st.warning('No player box-score data returned for this game.')
    st.stop()
if note:
    st.warning(note)

teams = df[['teamId','teamCity','teamName']].drop_duplicates()
team_labels = {int(row.teamId): f'{row.teamCity} {row.teamName}' for row in teams.itertuples()}
selected = st.radio('Team', options=list(team_labels), format_func=lambda k: team_labels[k], horizontal=True)
players = df[df['teamId'] == selected].copy()
players['Player'] = players['firstName'].fillna('') + ' ' + players['familyName'].fillna('')
players['MIN'] = players['minutes'].fillna('—')
players['FG'] = players.apply(lambda r: pair(r['fieldGoalsMade'],r['fieldGoalsAttempted']), axis=1)
players['FG%'] = players.apply(lambda r: ratio(r['fieldGoalsMade'],r['fieldGoalsAttempted']), axis=1)
players['3PT'] = players.apply(lambda r: pair(r['threePointersMade'],r['threePointersAttempted']), axis=1)
players['3P%'] = players.apply(lambda r: ratio(r['threePointersMade'],r['threePointersAttempted']), axis=1)
players['FT'] = players.apply(lambda r: pair(r['freeThrowsMade'],r['freeThrowsAttempted']), axis=1)
players['FT%'] = players.apply(lambda r: ratio(r['freeThrowsMade'],r['freeThrowsAttempted']), axis=1)
players['USG%'] = (pd.to_numeric(players['usagePercentage'], errors='coerce') * 100).round(1)
rename = {'points':'PTS','reboundsOffensive':'OREB','reboundsDefensive':'DREB','reboundsTotal':'REB','assists':'AST','steals':'STL','blocks':'BLK','turnovers':'TO','foulsPersonal':'PF','plusMinusPoints':'+/-'}
players = players.rename(columns=rename)
cols = ['Player','MIN','PTS','FG','FG%','3PT','3P%','FT','FT%','OREB','DREB','REB','AST','STL','BLK','TO','PF','+/-','USG%']
st.dataframe(players[cols], hide_index=True, use_container_width=True, column_config={'USG%': st.column_config.NumberColumn('USG%', format='%.1f%%', help='NBA official usage percentage')})
st.download_button('Download CSV', data=players[cols].to_csv(index=False), file_name=f'nba_{game_id}_{selected}_box_score.csv', mime='text/csv')
st.caption('USG% comes from the NBA advanced box score. Blank means unavailable, not zero. DNP players may have missing statistics.')
