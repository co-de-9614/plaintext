#!/usr/bin/env python3
"""
Fetch USC Women's Basketball game data from ESPN API and generate static HTML.

Usage:
    python fetch_games.py          # Only update if game is live or starting within 60 min
    python fetch_games.py --force  # Always update (used hourly and for manual triggers)
"""

import json
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Pacific Time zone
PT = ZoneInfo("America/Los_Angeles")

# USC Women's Basketball team ID on ESPN
USC_TEAM_ID = "30"
TEAM_NAME = "USC Trojans"
SPORT = "basketball"
LEAGUE = "womens-college-basketball"

BASE_API = f"https://site.api.espn.com/apis/site/v2/sports/{SPORT}/{LEAGUE}"

# How many minutes before game start to begin frequent updates
PREGAME_WINDOW_MINUTES = 60


def fetch_json(url: str) -> dict:
    """Fetch JSON from URL."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def get_roster_with_stats() -> list:
    """Get USC roster with current season stats aggregated from game box scores."""
    # Get schedule to find completed games
    schedule_url = f"{BASE_API}/teams/{USC_TEAM_ID}/schedule"
    schedule_data = fetch_json(schedule_url)

    events = schedule_data.get("events", [])
    completed = [e for e in events if e.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("state") == "post"]

    # Aggregate stats from each game
    # Stats indices: 0=MIN, 1=PTS, 5=REB, 6=AST, 8=STL, 9=BLK
    player_totals = {}  # {athlete_id: {name, jersey, pts, reb, ast, stl, blk, gp}}

    for event in completed:
        event_id = event.get("id")
        if not event_id:
            continue

        try:
            summary_url = f"{BASE_API}/summary?event={event_id}"
            game_data = fetch_json(summary_url)

            boxscore = game_data.get("boxscore", {})
            players = boxscore.get("players", [])

            for team in players:
                if team.get("team", {}).get("id") != USC_TEAM_ID:
                    continue

                statistics = team.get("statistics", [])
                if not statistics:
                    continue

                athletes = statistics[0].get("athletes", [])
                for a in athletes:
                    athlete = a.get("athlete", {})
                    athlete_id = athlete.get("id")
                    if not athlete_id:
                        continue

                    stats = a.get("stats", [])
                    if len(stats) < 10:
                        continue

                    # Parse stats (handle DNP)
                    try:
                        mins = int(stats[0]) if stats[0] and stats[0] != '--' else 0
                        pts = int(stats[1]) if stats[1] and stats[1] != '--' else 0
                        reb = int(stats[5]) if stats[5] and stats[5] != '--' else 0
                        ast = int(stats[6]) if stats[6] and stats[6] != '--' else 0
                        stl = int(stats[8]) if stats[8] and stats[8] != '--' else 0
                        blk = int(stats[9]) if stats[9] and stats[9] != '--' else 0
                    except (ValueError, IndexError):
                        continue

                    # Only count if player actually played
                    if mins == 0:
                        continue

                    if athlete_id not in player_totals:
                        player_totals[athlete_id] = {
                            "name": athlete.get("displayName", "Unknown"),
                            "jersey": athlete.get("jersey", ""),
                            "pts": 0, "reb": 0, "ast": 0, "stl": 0, "blk": 0, "gp": 0
                        }

                    player_totals[athlete_id]["pts"] += pts
                    player_totals[athlete_id]["reb"] += reb
                    player_totals[athlete_id]["ast"] += ast
                    player_totals[athlete_id]["stl"] += stl
                    player_totals[athlete_id]["blk"] += blk
                    player_totals[athlete_id]["gp"] += 1

        except Exception:
            continue

    # Calculate averages
    players = []
    for athlete_id, totals in player_totals.items():
        gp = totals["gp"]
        if gp > 0:
            players.append({
                "name": totals["name"],
                "jersey": totals["jersey"],
                "ppg": f"{totals['pts'] / gp:.1f}",
                "rpg": f"{totals['reb'] / gp:.1f}",
                "apg": f"{totals['ast'] / gp:.1f}",
                "spg": f"{totals['stl'] / gp:.1f}",
                "bpg": f"{totals['blk'] / gp:.1f}",
                "gp": str(gp)
            })

    # Sort by PPG descending
    players.sort(key=lambda x: float(x.get("ppg", 0)), reverse=True)
    return players


def get_rankings() -> dict:
    """Get current AP Top 25 rankings as a lookup dict {team_abbrev: rank}."""
    url = f"{BASE_API}/rankings"
    try:
        data = fetch_json(url)
        rankings = {}
        for ranking in data.get("rankings", []):
            if "AP" in ranking.get("name", ""):
                for team in ranking.get("ranks", []):
                    abbrev = team.get("team", {}).get("abbreviation", "")
                    rank = team.get("current", 0)
                    if abbrev and rank:
                        rankings[abbrev] = rank
                break
        return rankings
    except Exception:
        return {}


def get_team_schedule() -> dict:
    """Get USC's schedule and recent results."""
    url = f"{BASE_API}/teams/{USC_TEAM_ID}/schedule"
    return fetch_json(url)


def get_team_info() -> dict:
    """Get USC team information."""
    url = f"{BASE_API}/teams/{USC_TEAM_ID}"
    return fetch_json(url)


def get_scoreboard() -> dict:
    """Get today's scoreboard for all games."""
    url = f"{BASE_API}/scoreboard"
    return fetch_json(url)


def get_game_summary(event_id: str) -> dict:
    """Get detailed game summary including play-by-play."""
    url = f"{BASE_API}/summary?event={event_id}"
    return fetch_json(url)


def format_game_status(competition: dict) -> str:
    """Format the game status line."""
    status = competition.get("status", {})
    status_type = status.get("type", {})
    state = status_type.get("state", "")

    if state == "pre":
        # Game hasn't started
        date_str = status.get("type", {}).get("detail", "")
        return f"Scheduled: {date_str}"
    elif state == "in":
        # Game in progress
        display_clock = status.get("displayClock", "")
        period = status.get("period", 0)
        period_name = f"Q{period}" if period <= 4 else f"OT{period - 4}"
        return f"LIVE: {period_name} {display_clock}"
    elif state == "post":
        # Game finished
        return "FINAL"
    else:
        return status_type.get("detail", "Unknown")


def format_score_display(competition: dict) -> str:
    """Format the main score display like plaintextsports."""
    competitors = competition.get("competitors", [])
    if len(competitors) < 2:
        return "No score data"

    # Sort by home/away
    home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
    away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])

    home_team = home.get("team", {})
    away_team = away.get("team", {})

    home_abbrev = home_team.get("abbreviation", "HOME")
    away_abbrev = away_team.get("abbreviation", "AWAY")
    home_score = home.get("score", "-")
    away_score = away.get("score", "-")

    # Get records if available
    home_record = ""
    away_record = ""
    for rec in home.get("records", []):
        if rec.get("type") == "total":
            home_record = f" ({rec.get('summary', '')})"
            break
    for rec in away.get("records", []):
        if rec.get("type") == "total":
            away_record = f" ({rec.get('summary', '')})"
            break

    lines = []
    lines.append(f"{'AWAY':<6} {'':>20} {'HOME':<6}")
    lines.append(f"{away_abbrev:<6} {away_score:>8}  -  {home_score:<8} {home_abbrev:<6}")
    lines.append(f"{away_record:<20} {home_record:>20}")

    return "\n".join(lines)


def format_box_score(game_summary: dict) -> str:
    """Format a simple box score from game summary."""
    boxscore = game_summary.get("boxscore", {})
    players = boxscore.get("players", [])

    if not players:
        return "No box score available"

    lines = []

    for team_data in players:
        team = team_data.get("team", {})
        team_name = team.get("abbreviation", "TEAM")
        lines.append(f"\n{team_name}")
        lines.append("-" * 47)
        lines.append(f"{'PLAYER':<20} {'MIN':>5} {'PTS':>5} {'REB':>5} {'AST':>5} {'FG':>8}")
        lines.append("-" * 47)

        statistics = team_data.get("statistics", [])
        if statistics:
            stat_athletes = statistics[0].get("athletes", [])
            for athlete in stat_athletes[:10]:  # Top 10 players
                name = athlete.get("athlete", {}).get("shortName", "Unknown")
                stats = athlete.get("stats", [])
                if len(stats) >= 13:
                    # ESPN stat order: MIN, FG, 3PT, FT, OREB, DREB, REB, AST, STL, BLK, TO, PF, PTS
                    mins = stats[0] if stats[0] else "0"
                    fg = stats[1] if stats[1] else "0-0"
                    pts = stats[12] if len(stats) > 12 and stats[12] else "0"
                    reb = stats[6] if len(stats) > 6 and stats[6] else "0"
                    ast = stats[7] if len(stats) > 7 and stats[7] else "0"
                    lines.append(f"{name:<20} {mins:>5} {pts:>5} {reb:>5} {ast:>5} {fg:>8}")

    return "\n".join(lines)


def format_play_by_play(game_summary: dict, last_n: int = 10) -> str:
    """Format recent plays."""
    plays = game_summary.get("plays", [])

    if not plays:
        return "No play-by-play available"

    recent = plays[-last_n:] if len(plays) > last_n else plays
    recent.reverse()  # Most recent first

    lines = ["RECENT PLAYS", "-" * 47]

    for play in recent:
        clock = play.get("clock", {}).get("displayValue", "")
        period = play.get("period", {}).get("number", 0)
        period_name = f"Q{period}" if period <= 4 else f"OT{period - 4}"
        text = play.get("text", "")
        score = play.get("scoreValue", 0)

        if score:
            lines.append(f"{period_name} {clock:>5} | +{score} {text}")
        else:
            lines.append(f"{period_name} {clock:>5} | {text}")

    return "\n".join(lines)


def find_usc_game(scoreboard: dict, schedule: dict) -> dict | None:
    """Find USC's live or recent game from scoreboard or schedule."""
    # First check scoreboard
    for event in scoreboard.get("events", []):
        competitions = event.get("competitions", [])
        for comp in competitions:
            competitors = comp.get("competitors", [])
            for c in competitors:
                team_id = c.get("team", {}).get("id", "")
                if team_id == USC_TEAM_ID:
                    return {"event": event, "competition": comp}

    # Also check schedule for live game (not always on scoreboard)
    for event in schedule.get("events", []):
        comp = event.get("competitions", [{}])[0]
        status = comp.get("status", {}).get("type", {})
        state = status.get("state", "")
        if state == "in":  # Game in progress
            return {"event": event, "competition": comp}

    return None


def is_game_live_or_imminent(schedule: dict, scoreboard: dict) -> tuple[bool, str]:
    """
    Check if USC has a game that is:
    - Currently in progress
    - Starting within PREGAME_WINDOW_MINUTES

    Returns (should_update, reason)
    """
    now = datetime.now(timezone.utc)  # Use UTC for comparison since ESPN uses UTC

    # First check scoreboard and schedule for live game
    usc_game = find_usc_game(scoreboard, schedule)
    if usc_game:
        state = usc_game["competition"].get("status", {}).get("type", {}).get("state", "")
        if state == "in":
            return True, "Game is LIVE"
        elif state == "post":
            # Game just ended - update to show final
            return True, "Game just finished"

    # Check schedule for upcoming games
    events = schedule.get("events", [])
    for event in events:
        comp = event.get("competitions", [{}])[0]
        status = comp.get("status", {}).get("type", {})
        state = status.get("state", "")

        if state == "in":
            return True, "Game is LIVE"

        if state == "pre":
            date_str = comp.get("date", "")
            if date_str:
                try:
                    game_time = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    time_until = game_time - now
                    minutes_until = time_until.total_seconds() / 60

                    if -30 <= minutes_until <= PREGAME_WINDOW_MINUTES:
                        if minutes_until < 0:
                            return True, "Game should be starting now"
                        else:
                            return True, f"Game starts in {int(minutes_until)} minutes"
                except Exception:
                    pass

    return False, "No game live or imminent"


def generate_game_html(game_data: dict | None, schedule_data: dict, rankings: dict, roster: list) -> str:
    """Generate the main game page HTML."""
    now = datetime.now(PT)
    now_str = now.strftime("%I:%M:%S %p")
    now_iso = now.isoformat()

    content_lines = []
    content_lines.append(f"USC WOMEN'S BASKETBALL")
    content_lines.append(f'<span id="timestamps">Data loaded: {now_str}</span>')
    content_lines.append("=" * 47)

    if game_data:
        event = game_data["event"]
        competition = game_data["competition"]
        event_id = event.get("id", "")

        # Game status
        status_line = format_game_status(competition)
        content_lines.append(f"\n{status_line}")
        content_lines.append("")

        # Score display
        content_lines.append(format_score_display(competition))
        content_lines.append("")

        # Try to get detailed game summary for live/finished games
        state = competition.get("status", {}).get("type", {}).get("state", "")
        if state in ("in", "post") and event_id:
            try:
                summary = get_game_summary(event_id)

                # Play by play for live games
                if state == "in":
                    content_lines.append("")
                    content_lines.append(format_play_by_play(summary))

                # Box score
                content_lines.append("")
                content_lines.append(format_box_score(summary))
            except Exception as e:
                content_lines.append(f"\nCould not load game details: {e}")
    else:
        content_lines.append("\nNo game in progress today.")

        # Show player stats when no game
        if roster:
            content_lines.append("")
            content_lines.append("=" * 47)
            content_lines.append("SEASON STATS")
            content_lines.append("-" * 47)
            content_lines.append(f"{'PLAYER':<18} {'PPG':>5} {'RPG':>4} {'APG':>4} {'STL':>4} {'BLK':>4}")
            content_lines.append("-" * 47)
            for p in roster:  # Full roster
                name = p.get("name", "")[:14]
                jersey = p.get("jersey", "")
                player_str = f"#{jersey:>2} {name}" if jersey else f"    {name}"
                ppg = p.get("ppg", "-")
                rpg = p.get("rpg", "-")
                apg = p.get("apg", "-")
                spg = p.get("spg", "-")
                bpg = p.get("bpg", "-")
                content_lines.append(f"{player_str:<18} {ppg:>5} {rpg:>4} {apg:>4} {spg:>4} {bpg:>4}")

    # Upcoming schedule
    content_lines.append("\n")
    content_lines.append("=" * 47)
    content_lines.append("UPCOMING SCHEDULE")
    content_lines.append("-" * 47)

    events = schedule_data.get("events", [])
    upcoming = [e for e in events if e.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("state") == "pre"]

    for event in upcoming[:5]:
        comp = event.get("competitions", [{}])[0]
        date_raw = comp.get("date", "")
        if date_raw:
            try:
                dt = datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
                dt_pt = dt.astimezone(PT)
                date_str = dt_pt.strftime("%a %b %d %I:%M%p PT")
            except:
                date_str = date_raw[:10]
        else:
            date_str = "TBD"

        competitors = comp.get("competitors", [])
        opponent = next((c for c in competitors if c.get("team", {}).get("id") != USC_TEAM_ID), None)
        if opponent:
            opp_abbrev = opponent.get("team", {}).get("abbreviation", "OPP")
            home_away = "vs" if opponent.get("homeAway") == "away" else "at"

            # Get rankings from lookup
            opp_rank = rankings.get(opp_abbrev, 0)
            usc_rank = rankings.get("USC", 0)

            opp_str = f"#{opp_rank} {opp_abbrev}" if opp_rank else opp_abbrev
            usc_str = f"(#{usc_rank})" if usc_rank else ""

            content_lines.append(f"{date_str} {home_away} {opp_str} {usc_str}".rstrip())

    # Recent results
    content_lines.append("\n")
    content_lines.append("RECENT RESULTS")
    content_lines.append("-" * 47)

    completed = [e for e in events if e.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("state") == "post"]

    for event in completed[-5:]:
        comp = event.get("competitions", [{}])[0]
        date_raw = comp.get("date", "")
        if date_raw:
            try:
                dt = datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
                date_str = dt.strftime("%b %d")
            except:
                date_str = date_raw[:10]
        else:
            date_str = ""

        competitors = comp.get("competitors", [])
        usc = next((c for c in competitors if c.get("team", {}).get("id") == USC_TEAM_ID), None)
        opponent = next((c for c in competitors if c.get("team", {}).get("id") != USC_TEAM_ID), None)

        if usc and opponent:
            usc_score_raw = usc.get("score", "")
            opp_score_raw = opponent.get("score", "")
            opp_abbrev = opponent.get("team", {}).get("abbreviation", "OPP")

            # Handle score being a dict or string
            if isinstance(usc_score_raw, dict):
                usc_score = usc_score_raw.get("displayValue", str(usc_score_raw.get("value", "")))
            else:
                usc_score = str(usc_score_raw)

            if isinstance(opp_score_raw, dict):
                opp_score = opp_score_raw.get("displayValue", str(opp_score_raw.get("value", "")))
            else:
                opp_score = str(opp_score_raw)

            try:
                result = "W" if float(usc_score) > float(opp_score) else "L"
            except:
                result = "-"

            # Add ranking if opponent is ranked
            opp_rank = rankings.get(opp_abbrev, 0)
            opp_str = f"#{opp_rank} {opp_abbrev}" if opp_rank else opp_abbrev

            # Home vs away
            home_away = "vs" if opponent.get("homeAway") == "away" else "at"

            content_lines.append(f"{date_str} {result} {usc_score}-{opp_score} {home_away} {opp_str}")

    # Link to full schedule
    content_lines.append("")
    content_lines.append('<a href="schedule.html">Full Schedule/Results</a>')

    content = "\n".join(content_lines)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>USC Women's Basketball</title>
    <meta name="data-loaded" content="{now_iso}">
    <style>
        * {{
            box-sizing: border-box;
        }}
        body {{
            font-family: monospace;
            background: #ffffff;
            color: #1a1a1a;
            padding: 16px;
            max-width: 100%;
            margin: 0 auto;
            line-height: 1.4;
            overflow-x: hidden;
        }}
        pre {{
            white-space: pre-wrap;
            word-wrap: break-word;
            overflow-wrap: break-word;
            margin: 0;
            font-size: 12px;
        }}
        a {{
            color: #0066cc;
        }}
    </style>
</head>
<body>
<pre>
{content}
</pre>
<script>
(function() {{
    const dataLoaded = new Date(document.querySelector('meta[name="data-loaded"]').content);
    const pageLoaded = new Date();

    function formatTime(date) {{
        return date.toLocaleTimeString('en-US', {{ hour: 'numeric', minute: '2-digit', second: '2-digit', hour12: true }});
    }}

    function timeAgo(date) {{
        const seconds = Math.floor((new Date() - date) / 1000);
        if (seconds < 60) return 'just now';
        const minutes = Math.floor(seconds / 60);
        if (minutes < 60) return minutes + ' min ago';
        const hours = Math.floor(minutes / 60);
        if (hours < 24) return hours + ' hr ago';
        const days = Math.floor(hours / 24);
        return days + ' day' + (days > 1 ? 's' : '') + ' ago';
    }}

    function updateTimestamps() {{
        const el = document.getElementById('timestamps');
        if (el) {{
            const pageLoadedStr = 'Page loaded: ' + formatTime(pageLoaded);
            const pageAgo = '(' + timeAgo(pageLoaded) + ')';
            const pagePadding = 55 - pageLoadedStr.length - pageAgo.length;
            const pageSpaces = pagePadding > 0 ? ' '.repeat(pagePadding) : ' ';

            const dataLoadedStr = 'Data loaded: ' + formatTime(dataLoaded);
            const dataAgo = '(' + timeAgo(dataLoaded) + ')';
            const dataPadding = 55 - dataLoadedStr.length - dataAgo.length;
            const dataSpaces = dataPadding > 0 ? ' '.repeat(dataPadding) : ' ';

            el.innerHTML = pageLoadedStr + pageSpaces + pageAgo + '\\n' + dataLoadedStr + dataSpaces + dataAgo;
        }}
    }}

    updateTimestamps();
    setInterval(updateTimestamps, 60000); // Update every minute
}})();
</script>
</body>
</html>
"""
    return html


def generate_schedule_html(schedule_data: dict, rankings: dict) -> str:
    """Generate the full schedule/results page."""
    now = datetime.now(PT)
    now_str = now.strftime("%I:%M:%S %p")
    now_iso = now.isoformat()

    content_lines = []
    content_lines.append(f'<span id="timestamps">Data loaded: {now_str}</span>')
    content_lines.append("USC WOMEN'S BASKETBALL")
    content_lines.append("Full Schedule/Results")
    content_lines.append("=" * 47)

    events = schedule_data.get("events", [])

    # Split into completed and upcoming
    completed = [e for e in events if e.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("state") == "post"]
    upcoming = [e for e in events if e.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("state") in ("pre", "in")]

    # Results section
    content_lines.append("RESULTS")
    content_lines.append("-" * 47)

    for event in completed:
        event_id = event.get("id", "")
        comp = event.get("competitions", [{}])[0]

        # Date
        date_raw = comp.get("date", "")
        if date_raw:
            try:
                dt = datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
                dt_pt = dt.astimezone(PT)
                date_str = dt_pt.strftime("%b %d")
            except:
                date_str = date_raw[:10]
        else:
            date_str = "TBD"

        competitors = comp.get("competitors", [])
        usc = next((c for c in competitors if c.get("team", {}).get("id") == USC_TEAM_ID), None)
        opponent = next((c for c in competitors if c.get("team", {}).get("id") != USC_TEAM_ID), None)

        if not opponent:
            continue

        opp_team = opponent.get("team", {})
        opp_abbrev = opp_team.get("abbreviation", "OPP")
        opp_school = opp_team.get("location", opp_abbrev)
        home_away = "vs" if opponent.get("homeAway") == "away" else "at"

        # Ranking
        opp_rank = rankings.get(opp_abbrev, 0)
        opp_str = f"#{opp_rank} {opp_school}" if opp_rank else opp_school

        # Completed game
        usc_score_raw = usc.get("score", "") if usc else ""
        opp_score_raw = opponent.get("score", "")

        if isinstance(usc_score_raw, dict):
            usc_score = usc_score_raw.get("displayValue", str(usc_score_raw.get("value", "")))
        else:
            usc_score = str(usc_score_raw)

        if isinstance(opp_score_raw, dict):
            opp_score = opp_score_raw.get("displayValue", str(opp_score_raw.get("value", "")))
        else:
            opp_score = str(opp_score_raw)

        try:
            result = "W" if float(usc_score) > float(opp_score) else "L"
        except:
            result = "-"

        game_link = f'<a href="games/{event_id}.html">{date_str} {result} {usc_score}-{opp_score} {home_away} {opp_str}</a>'
        content_lines.append(game_link)

    # Upcoming section
    content_lines.append("")
    content_lines.append("UPCOMING SCHEDULE")
    content_lines.append("-" * 47)

    for event in upcoming:
        comp = event.get("competitions", [{}])[0]
        status = comp.get("status", {}).get("type", {})
        state = status.get("state", "")

        # Date
        date_raw = comp.get("date", "")
        if date_raw:
            try:
                dt = datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
                dt_pt = dt.astimezone(PT)
                date_str = dt_pt.strftime("%b %d %I:%M%p")
            except:
                date_str = date_raw[:10]
        else:
            date_str = "TBD"

        competitors = comp.get("competitors", [])
        opponent = next((c for c in competitors if c.get("team", {}).get("id") != USC_TEAM_ID), None)

        if not opponent:
            continue

        opp_team = opponent.get("team", {})
        opp_abbrev = opp_team.get("abbreviation", "OPP")
        opp_school = opp_team.get("location", opp_abbrev)
        home_away = "vs" if opponent.get("homeAway") == "away" else "at"

        # Ranking
        opp_rank = rankings.get(opp_abbrev, 0)
        opp_str = f"#{opp_rank} {opp_school}" if opp_rank else opp_school

        if state == "in":
            content_lines.append(f"{date_str} LIVE {home_away} {opp_str}")
        else:
            content_lines.append(f"{date_str} {home_away} {opp_str}")

    # Link back to main page
    content_lines.append("")
    content_lines.append('<a href="index.html">Back to Home</a>')

    content = "\n".join(content_lines)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>USC WBB Schedule</title>
    <meta name="data-loaded" content="{now_iso}">
    <style>
        * {{
            box-sizing: border-box;
        }}
        body {{
            font-family: monospace;
            background: #ffffff;
            color: #1a1a1a;
            padding: 16px;
            max-width: 100%;
            margin: 0 auto;
            line-height: 1.4;
            overflow-x: hidden;
        }}
        pre {{
            white-space: pre-wrap;
            word-wrap: break-word;
            overflow-wrap: break-word;
            margin: 0;
            font-size: 12px;
        }}
        a {{
            color: #0066cc;
        }}
    </style>
</head>
<body>
<pre>
{content}
</pre>
<script>
(function() {{
    const dataLoaded = new Date(document.querySelector('meta[name="data-loaded"]').content);
    const pageLoaded = new Date();

    function formatTime(date) {{
        return date.toLocaleTimeString('en-US', {{ hour: 'numeric', minute: '2-digit', second: '2-digit', hour12: true }});
    }}

    function timeAgo(date) {{
        const seconds = Math.floor((new Date() - date) / 1000);
        if (seconds < 60) return 'just now';
        const minutes = Math.floor(seconds / 60);
        if (minutes < 60) return minutes + ' min ago';
        const hours = Math.floor(minutes / 60);
        if (hours < 24) return hours + ' hr ago';
        const days = Math.floor(hours / 24);
        return days + ' day' + (days > 1 ? 's' : '') + ' ago';
    }}

    function updateTimestamps() {{
        const el = document.getElementById('timestamps');
        if (el) {{
            const pageLoadedStr = 'Page loaded: ' + formatTime(pageLoaded);
            const pageAgo = '(' + timeAgo(pageLoaded) + ')';
            const pagePadding = 55 - pageLoadedStr.length - pageAgo.length;
            const pageSpaces = pagePadding > 0 ? ' '.repeat(pagePadding) : ' ';

            const dataLoadedStr = 'Data loaded: ' + formatTime(dataLoaded);
            const dataAgo = '(' + timeAgo(dataLoaded) + ')';
            const dataPadding = 55 - dataLoadedStr.length - dataAgo.length;
            const dataSpaces = dataPadding > 0 ? ' '.repeat(dataPadding) : ' ';

            el.innerHTML = pageLoadedStr + pageSpaces + pageAgo + '\\n' + dataLoadedStr + dataSpaces + dataAgo;
        }}
    }}

    updateTimestamps();
    setInterval(updateTimestamps, 60000); // Update every minute
}})();
</script>
</body>
</html>
"""
    return html


def generate_game_page(event_id: str, rankings: dict = None, team_records: dict = None) -> str:
    """Generate a detailed game report page."""
    if rankings is None:
        rankings = {}
    if team_records is None:
        team_records = {}

    now = datetime.now(PT)
    now_str = now.strftime("%I:%M:%S %p")
    now_iso = now.isoformat()

    summary_url = f"{BASE_API}/summary?event={event_id}"
    game = fetch_json(summary_url)

    header = game.get("header", {})
    competitions = header.get("competitions", [{}])
    comp = competitions[0] if competitions else {}

    boxscore = game.get("boxscore", {})
    gameInfo = game.get("gameInfo", {})

    # Get teams and scores
    competitors = comp.get("competitors", [])
    home = next((c for c in competitors if c.get("homeAway") == "home"), {})
    away = next((c for c in competitors if c.get("homeAway") == "away"), {})

    home_team = home.get("team", {})
    away_team = away.get("team", {})
    home_abbrev = home_team.get("abbreviation", "HOME")
    away_abbrev = away_team.get("abbreviation", "AWAY")
    home_score = home.get("score", "0")
    away_score = away.get("score", "0")

    # Get current records from team_records dict, fallback to game-time record
    home_record = team_records.get(home_abbrev, "")
    away_record = team_records.get(away_abbrev, "")
    if not home_record:
        home_record = home.get("record", [{}])[0].get("displayValue", "") if home.get("record") else ""
    if not away_record:
        away_record = away.get("record", [{}])[0].get("displayValue", "") if away.get("record") else ""

    # Get rankings
    home_rank = rankings.get(home_abbrev, 0)
    away_rank = rankings.get(away_abbrev, 0)

    # Quarter scores
    home_quarters = [q.get("displayValue", "0") for q in home.get("linescores", [])]
    away_quarters = [q.get("displayValue", "0") for q in away.get("linescores", [])]

    # Game status
    status = comp.get("status", {}).get("type", {})
    status_state = status.get("state", "post")
    status_detail = status.get("detail", "Final")

    # Get live game data if in progress
    is_live = status_state == "in"
    game_clock = comp.get("status", {}).get("displayClock", "")
    game_period = comp.get("status", {}).get("period", 0)

    # Get team fouls and timeouts from situation if available
    situation = game.get("situation", {})
    home_fouls = ""
    away_fouls = ""
    home_timeouts = ""
    away_timeouts = ""

    if situation:
        home_fouls = str(situation.get("homeTeamFouls", ""))
        away_fouls = str(situation.get("awayTeamFouls", ""))
        home_timeouts = str(situation.get("homeTimeouts", ""))
        away_timeouts = str(situation.get("awayTimeouts", ""))

    # Page width is 55 characters
    PAGE_WIDTH = 55
    # Team centers: USC at 14, opponent at 42 (1-indexed), center at 28
    LEFT_CENTER = 13   # 0-indexed position 14
    RIGHT_CENTER = 41  # 0-indexed position 42
    PAGE_CENTER = 27   # 0-indexed position 28

    content_lines = []
    content_lines.append(f'<span id="timestamps">Data loaded: {now_str}</span>')
    content_lines.append('<a href="../schedule.html">&lt; USC Schedule</a>')
    content_lines.append("")

    # Determine USC and opponent - always show USC first (left side)
    usc_is_home = home_team.get("id") == USC_TEAM_ID
    if usc_is_home:
        usc_team = home_team
        usc_score = home_score
        usc_record = home_record
        usc_rank = home_rank
        usc_quarters = home_quarters
        opp_team = away_team
        opp_score = away_score
        opp_record = away_record
        opp_rank = away_rank
        opp_quarters = away_quarters
    else:
        usc_team = away_team
        usc_score = away_score
        usc_record = away_record
        usc_rank = away_rank
        usc_quarters = away_quarters
        opp_team = home_team
        opp_score = home_score
        opp_record = home_record
        opp_rank = home_rank
        opp_quarters = home_quarters

    # Get full team names
    usc_school = "USC"
    usc_name = "Trojans"
    opp_school = opp_team.get("location", opp_team.get("abbreviation", "OPP"))
    opp_name = opp_team.get("name", "")
    opp_abbrev_display = opp_team.get("abbreviation", "OPP")

    # Add ranking prefix if applicable
    usc_rank_str = f"#{usc_rank} " if usc_rank else ""
    opp_rank_str = f"#{opp_rank} " if opp_rank else ""

    # Helper to center text at a position within PAGE_WIDTH
    def center_at(text, pos):
        start = pos - len(text) // 2
        return " " * max(0, start) + text

    # Build header lines
    if is_live:
        # Live game format: centered clock in red, score, fouls, timeouts
        period_name = f"Q{game_period}" if game_period <= 4 else f"OT{game_period - 4}"
        clock_str = f"{period_name} {game_clock}"

        # Line 1: Period and clock in red, centered
        clock_padding = " " * (PAGE_CENTER - len(clock_str) // 2)
        content_lines.append(f'{clock_padding}<span class="live-clock">{clock_str}</span>')

        # Line 2: Score centered
        score_str = f"{usc_score} - {opp_score}"
        score_padding = " " * (PAGE_CENTER - len(score_str) // 2)
        content_lines.append(f"{score_padding}{score_str}")

        # Line 3: Team fouls (if available)
        if usc_is_home:
            usc_fouls = home_fouls
            opp_fouls = away_fouls
            usc_timeouts = home_timeouts
            opp_timeouts = away_timeouts
        else:
            usc_fouls = away_fouls
            opp_fouls = home_fouls
            usc_timeouts = away_timeouts
            opp_timeouts = home_timeouts

        if usc_fouls and opp_fouls:
            fouls_str = f"{usc_fouls} TF {opp_fouls}"
            fouls_padding = " " * (PAGE_CENTER - len(fouls_str) // 2)
            content_lines.append(f"{fouls_padding}{fouls_str}")

        # Line 4: Timeouts remaining (if available)
        if usc_timeouts and opp_timeouts:
            timeouts_str = f"{usc_timeouts} TOL {opp_timeouts}"
            timeouts_padding = " " * (PAGE_CENTER - len(timeouts_str) // 2)
            content_lines.append(f"{timeouts_padding}{timeouts_str}")

        content_lines.append("")
    else:
        # Final/scheduled game format: standard three-line header
        # Line 1: School names and status
        line1 = [" "] * PAGE_WIDTH
        usc_school_full = f"{usc_rank_str}{usc_school}"
        opp_school_full = f"{opp_rank_str}{opp_school}"

        # Place USC school at LEFT_CENTER
        usc_start = LEFT_CENTER - len(usc_school_full) // 2
        for i, c in enumerate(usc_school_full):
            if 0 <= usc_start + i < PAGE_WIDTH:
                line1[usc_start + i] = c

        # Place status at PAGE_CENTER
        status_start = PAGE_CENTER - len(status_detail) // 2
        for i, c in enumerate(status_detail):
            if 0 <= status_start + i < PAGE_WIDTH:
                line1[status_start + i] = c

        # Place opponent school at RIGHT_CENTER
        opp_start = RIGHT_CENTER - len(opp_school_full) // 2
        for i, c in enumerate(opp_school_full):
            if 0 <= opp_start + i < PAGE_WIDTH:
                line1[opp_start + i] = c

        # Line 2: Team names and score
        line2 = [" "] * PAGE_WIDTH
        score_str = f"{usc_score} - {opp_score}"

        # Place USC team name at LEFT_CENTER
        usc_name_start = LEFT_CENTER - len(usc_name) // 2
        for i, c in enumerate(usc_name):
            if 0 <= usc_name_start + i < PAGE_WIDTH:
                line2[usc_name_start + i] = c

        # Place score at PAGE_CENTER
        score_start = PAGE_CENTER - len(score_str) // 2
        for i, c in enumerate(score_str):
            if 0 <= score_start + i < PAGE_WIDTH:
                line2[score_start + i] = c

        # Place opponent team name at RIGHT_CENTER
        opp_name_start = RIGHT_CENTER - len(opp_name) // 2
        for i, c in enumerate(opp_name):
            if 0 <= opp_name_start + i < PAGE_WIDTH:
                line2[opp_name_start + i] = c

        # Line 3: Records
        line3 = [" "] * PAGE_WIDTH

        # Place USC record at LEFT_CENTER
        usc_rec_start = LEFT_CENTER - len(usc_record) // 2
        for i, c in enumerate(usc_record):
            if 0 <= usc_rec_start + i < PAGE_WIDTH:
                line3[usc_rec_start + i] = c

        # Place opponent record at RIGHT_CENTER
        opp_rec_start = RIGHT_CENTER - len(opp_record) // 2
        for i, c in enumerate(opp_record):
            if 0 <= opp_rec_start + i < PAGE_WIDTH:
                line3[opp_rec_start + i] = c

        content_lines.append("".join(line1).rstrip())
        content_lines.append("".join(line2).rstrip())
        content_lines.append("".join(line3).rstrip())
        content_lines.append("")

    # Quarter by quarter box score - centered within 55 chars, USC first
    num_periods = max(len(usc_quarters), len(opp_quarters), 4)
    period_labels = ["1", "2", "3", "4"] + [f"OT{i}" for i in range(1, num_periods - 3)]
    period_labels = period_labels[:num_periods]

    # Build box score rows
    box_header = "     " + "".join(f"{p:>4}" for p in period_labels) + "    T"
    box_width = len(box_header)
    box_padding = (PAGE_WIDTH - box_width) // 2
    pad = " " * box_padding

    content_lines.append(pad + box_header)
    content_lines.append(pad + "-" * box_width)

    # USC first, then opponent
    usc_row = f"{'USC':<5}" + "".join(f"{q:>4}" for q in usc_quarters) + f"  {usc_score:>3}"
    opp_row = f"{opp_abbrev_display:<5}" + "".join(f"{q:>4}" for q in opp_quarters) + f"  {opp_score:>3}"
    content_lines.append(pad + usc_row)
    content_lines.append(pad + opp_row)
    content_lines.append("")

    # Game Flow visualization (based on game lead)
    plays = game.get("plays", [])
    scoring_plays = [p for p in plays if p.get("scoringPlay")]

    # Get opponent color for game flow
    opp_color = opp_team.get("color", "888888")
    opp_abbrev = opp_abbrev_display

    if scoring_plays:
        # Settings: 11 columns per quarter (start + 10 minutes), plus breaks
        # Col layout per quarter: "+" (break) then "=" (start) then 10 "=" (minutes 1-10)
        cols_per_quarter = 12  # 1 "+" break + 11 "=" columns (1 start + 10 minutes)
        total_cols = num_periods * cols_per_quarter + 1  # +1 for final "+"

        # Track USC lead at each column
        # Positive = USC leading, negative = opponent leading
        lead_at_col = {}

        for play in scoring_plays:
            period = play.get("period", {}).get("number", 1)
            clock_str = play.get("clock", {}).get("displayValue", "10:00")
            away_sc = play.get("awayScore", 0)
            home_sc = play.get("homeScore", 0)

            # Parse clock to determine which minute we're in
            try:
                parts = clock_str.split(":")
                minutes_left = int(parts[0])
                seconds_left = int(parts[1]) if len(parts) > 1 else 0
                seconds_remaining = minutes_left * 60 + seconds_left
                seconds_elapsed = 600 - seconds_remaining  # 10-min quarters

                # Calculate which minute (1-10) - dot represents score at end of that minute
                if seconds_elapsed <= 0:
                    minute = 1
                else:
                    minute = min(10, max(1, (seconds_elapsed + 59) // 60))  # ceil division
            except:
                minute = 5  # default to middle

            # Calculate column: break at 0, start at 1, minutes 1-10 at cols 2-11
            # For quarter q: break at (q-1)*12, start at (q-1)*12+1, minutes at (q-1)*12+2 to +11
            col = (period - 1) * cols_per_quarter + minute + 1  # +1 for start column

            # Lead from USC perspective: positive = USC leading
            if usc_is_home:
                lead = home_sc - away_sc
            else:
                lead = away_sc - home_sc
            lead_at_col[col] = lead

        # Fill in gaps by carrying forward the last known lead
        # Break columns (multiples of cols_per_quarter) get None - no dots there
        # Start columns show the lead carried from previous quarter
        last_lead = 0
        filled_lead = []
        for col in range(total_cols):
            is_break = (col % cols_per_quarter == 0)
            if col in lead_at_col:
                last_lead = lead_at_col[col]
            if is_break:
                filled_lead.append(None)  # No dots at break positions
            else:
                filled_lead.append(last_lead)

        # Calculate separate heights for USC (positive leads) and opponent (negative leads)
        valid_leads = [l for l in filled_lead if l is not None]
        max_usc_lead = max(0, max(valid_leads)) if valid_leads else 0
        max_opp_lead = abs(min(0, min(valid_leads))) if valid_leads else 0
        usc_height = max(1, (max_usc_lead + 2) // 3) if max_usc_lead > 0 else 0
        opp_height = max(1, (max_opp_lead + 2) // 3) if max_opp_lead > 0 else 0

        # Build the visualization
        # Total chart width = 6 (padding) + total_cols
        chart_width = 6 + total_cols
        legend = "(1 dot = 3 pts)"
        game_flow_label = "<b>Game Flow:</b>"
        # Right-justify the legend to align with the final "+"
        spacing = chart_width - 10 - len(legend)  # 10 = len("Game Flow:")
        content_lines.append(f"{game_flow_label}{' ' * spacing}{legend}")
        content_lines.append("")
        content_lines.append('<span class="game-flow">')

        # USC rows (dots going up when USC is leading) - cardinal color
        for row in range(usc_height, 0, -1):
            threshold = row * 3
            line = ""
            for col in range(total_cols):
                if filled_lead[col] is None:
                    line += " "  # No dot at break positions
                elif filled_lead[col] >= threshold:
                    line += "."
                else:
                    line += " "
            # Put USC label on the bottom row (row 1) of USC dots
            if row == 1:
                content_lines.append(f'<span class="usc-dots">USC   {line}</span>')
            else:
                content_lines.append(f'<span class="usc-dots">      {line}</span>')

        # Blank line before timeline to prevent overlap with compact line-height
        content_lines.append("")

        # Timeline: + at breaks, = for minutes
        timeline = ""
        for col in range(total_cols):
            if col % cols_per_quarter == 0:
                timeline += "+"
            else:
                timeline += "="
        content_lines.append(f"      {timeline}")

        # Opponent rows (dots going down when opponent is leading)
        for row in range(1, opp_height + 1):
            threshold = row * 3
            line = ""
            for col in range(total_cols):
                if filled_lead[col] is None:
                    line += " "  # No dot at break positions
                elif filled_lead[col] <= -threshold:
                    line += "."
                else:
                    line += " "
            # Put opponent label on the first row of opponent dots
            if row == 1:
                content_lines.append(f'<span style="color: #{opp_color};">{opp_abbrev:<6}{line}</span>')
            else:
                content_lines.append(f'<span style="color: #{opp_color};">      {line}</span>')

        content_lines.append('</span>')
        content_lines.append("")

    # Calculate lead changes, times tied, and biggest leads from scoring plays
    if scoring_plays:
        lead_changes = 0
        times_tied = 0
        usc_biggest_lead = 0
        opp_biggest_lead = 0
        prev_leader = None  # None = tied, "usc" = USC leading, "opp" = opponent leading

        for play in scoring_plays:
            away_sc = play.get("awayScore", 0)
            home_sc = play.get("homeScore", 0)

            # Calculate lead from USC perspective
            if usc_is_home:
                lead = home_sc - away_sc
            else:
                lead = away_sc - home_sc

            # Track biggest leads
            if lead > 0:
                usc_biggest_lead = max(usc_biggest_lead, lead)
            elif lead < 0:
                opp_biggest_lead = max(opp_biggest_lead, abs(lead))

            # Determine current leader
            if lead > 0:
                current_leader = "usc"
            elif lead < 0:
                current_leader = "opp"
            else:
                current_leader = None

            # Count lead changes (when lead switches from one team to the other)
            if prev_leader is not None and current_leader is not None and prev_leader != current_leader:
                lead_changes += 1

            # Count times tied (when score becomes tied after not being tied)
            if current_leader is None and prev_leader is not None:
                times_tied += 1

            prev_leader = current_leader

        # Display lead stats
        content_lines.append(f"Lead Changes: {lead_changes}")
        content_lines.append(f"Times Tied: {times_tied}")
        usc_lead_str = str(usc_biggest_lead) if usc_biggest_lead > 0 else "N/A"
        opp_lead_str = str(opp_biggest_lead) if opp_biggest_lead > 0 else "N/A"
        content_lines.append(f"Biggest Lead: USC: {usc_lead_str}, {opp_abbrev}: {opp_lead_str}")
        content_lines.append("")

    # Game info
    venue = gameInfo.get("venue", {})
    venue_name = venue.get("fullName", "")
    attendance = gameInfo.get("attendance", 0)
    if venue_name:
        content_lines.append(f"Venue: {venue_name}")
    if attendance:
        content_lines.append(f"Attendance: {attendance:,}")
    content_lines.append("")

    # Calculate plus/minus for each player from play-by-play
    def calculate_plus_minus(plays, boxscore):
        """Calculate plus/minus for each player by tracking who's on court during scoring."""
        plus_minus = {}  # athlete_id -> +/- value

        # Get starters for each team from boxscore
        on_court = {}  # team_id -> set of athlete_ids currently on court

        players_data = boxscore.get("players", [])
        for team_data in players_data:
            team_id = team_data.get("team", {}).get("id", "")
            statistics = team_data.get("statistics", [])
            if statistics:
                athletes = statistics[0].get("athletes", [])
                starters = [a.get("athlete", {}).get("id") for a in athletes if a.get("starter")]
                on_court[team_id] = set(starters)
                # Initialize plus/minus for all players
                for a in athletes:
                    athlete_id = a.get("athlete", {}).get("id")
                    if athlete_id:
                        plus_minus[athlete_id] = 0

        prev_home_score = 0
        prev_away_score = 0

        for play in plays:
            play_type = play.get("type", {}).get("text", "").lower()
            play_text = play.get("text", "").lower()
            home_score = play.get("homeScore", prev_home_score)
            away_score = play.get("awayScore", prev_away_score)

            # Handle substitutions - parse from play text
            if "substitution" in play_type:
                participants = play.get("participants", [])
                team_id = play.get("team", {}).get("id", "")
                if participants and team_id in on_court:
                    athlete_id = participants[0].get("athlete", {}).get("id")
                    if athlete_id:
                        if "subbing out" in play_text or "exits" in play_text:
                            on_court[team_id].discard(athlete_id)
                        elif "subbing in" in play_text or "enters" in play_text:
                            on_court[team_id].add(athlete_id)

            # Calculate score change and attribute to players on court
            home_diff = home_score - prev_home_score
            away_diff = away_score - prev_away_score

            if home_diff != 0 or away_diff != 0:
                # For each team's players on court, add/subtract the differential
                for team_id, players_on in on_court.items():
                    for athlete_id in players_on:
                        if athlete_id in plus_minus:
                            # Home team: +home_diff - away_diff
                            # Away team: +away_diff - home_diff
                            is_home = team_id == home.get("team", {}).get("id", "")
                            if is_home:
                                plus_minus[athlete_id] += home_diff - away_diff
                            else:
                                plus_minus[athlete_id] += away_diff - home_diff

            prev_home_score = home_score
            prev_away_score = away_score

        return plus_minus

    # Calculate plus/minus from plays
    player_plus_minus = calculate_plus_minus(plays, boxscore) if plays else {}

    # Stats header line for player stats
    stats_header = "MIN     FG   3PT    FT ORB DRB AST STL BLK  TO FLS  PTS"

    # Helper to convert dash to slash in shooting stats
    def to_slash(stat):
        return stat.replace("-", "/") if stat else "0/0"

    # Helper to parse shooting stats for totals
    def parse_shooting(stat):
        if not stat or stat == '--':
            return (0, 0)
        parts = stat.replace("/", "-").split("-")
        if len(parts) == 2:
            try:
                return (int(parts[0]), int(parts[1]))
            except:
                pass
        return (0, 0)

    # Helper to get sort key for player (mins desc, pts desc, then alphabetical by last name)
    # ESPN indices: 0=MIN, 1=PTS, 2=FG, 3=3PT, 4=FT, 5=REB, 6=AST, 7=TO, 8=STL, 9=BLK, 10=OREB, 11=DREB, 12=PF
    def player_sort_key(a):
        stats = a.get("stats", [])
        athlete = a.get("athlete", {})
        name = athlete.get("displayName", "Unknown")
        # Get last name for alphabetical sort
        last_name = name.split()[-1] if name else "ZZZ"

        if not stats or len(stats) < 6:
            return (0, 0, last_name)
        try:
            mins = int(stats[0]) if stats[0] and stats[0] != '--' else 0
            pts = int(stats[1]) if stats[1] and stats[1] != '--' else 0
            return (-mins, -pts, last_name)
        except:
            return (0, 0, last_name)

    # Player stats for each team (USC first)
    players_data = boxscore.get("players", [])
    players_data_sorted = sorted(players_data, key=lambda t: t.get("team", {}).get("id") != USC_TEAM_ID)

    for team_data in players_data_sorted:
        team = team_data.get("team", {})
        team_abbrev = team.get("abbreviation", "TEAM")
        team_id = team.get("id", "")

        # Get team color - use cardinal for USC, team color for opponents
        if team_id == USC_TEAM_ID:
            team_color = "990000"  # USC cardinal
        else:
            team_color = team.get("color", "888888")

        statistics = team_data.get("statistics", [])
        if not statistics:
            continue

        athletes = statistics[0].get("athletes", [])

        # Separate starters and bench
        starters = [a for a in athletes if a.get("starter")]
        bench = [a for a in athletes if not a.get("starter")]

        # Sort starters and bench by points
        starters_sorted = sorted(starters, key=player_sort_key)
        bench_sorted = sorted(bench, key=player_sort_key)

        # Team totals accumulators
        team_totals = {
            "fg_made": 0, "fg_att": 0,
            "three_made": 0, "three_att": 0,
            "ft_made": 0, "ft_att": 0,
            "pts": 0, "orb": 0, "drb": 0, "ast": 0, "stl": 0, "blk": 0, "to": 0, "fls": 0
        }

        # Build all spans with continuous zebra striping
        all_spans = []
        row_idx = 0

        # Starters header (team colored)
        row_class = "row-even" if row_idx % 2 == 0 else "row-odd"
        all_spans.append(f'<span class="{row_class}" style="color: #{team_color};"><b>{team_abbrev} STARTERS</b>\n{stats_header}</span>')
        row_idx += 1

        # Starters
        for a in starters_sorted:
            athlete = a.get("athlete", {})
            athlete_id = athlete.get("id", "")
            name = athlete.get("displayName", "Unknown")
            jersey = athlete.get("jersey", "")
            stats = a.get("stats", [])

            row_class = "row-even" if row_idx % 2 == 0 else "row-odd"
            row_idx += 1

            jersey_str = f"#{jersey}" if jersey else ""
            name_part = f"{name} {jersey_str}"

            # Get plus/minus for this player
            pm_val = player_plus_minus.get(athlete_id, 0)
            pm_str = f"+{pm_val}" if pm_val > 0 else str(pm_val)
            # Pad player line to align +/- at position 52 (before PTS column)
            padding = 52 - len(name_part)
            player_line = f'{name_part}{" " * padding}<span class="plusminus">{pm_str:>4}</span>'

            if not stats or len(stats) < 13:
                stats_line = '<span class="dnp">  Did not play</span>'
                player_line = name_part  # No +/- for DNP
            else:
                mins = stats[0] if stats[0] and stats[0] != '--' else "0"
                pts = stats[1] if stats[1] and stats[1] != '--' else "0"
                fg = stats[2] if stats[2] and stats[2] != '--' else "0-0"
                threept = stats[3] if stats[3] and stats[3] != '--' else "0-0"
                ft = stats[4] if stats[4] and stats[4] != '--' else "0-0"
                orb = stats[10] if stats[10] and stats[10] != '--' else "0"
                drb = stats[11] if stats[11] and stats[11] != '--' else "0"
                ast = stats[6] if stats[6] and stats[6] != '--' else "0"
                stl = stats[8] if stats[8] and stats[8] != '--' else "0"
                blk = stats[9] if stats[9] and stats[9] != '--' else "0"
                to = stats[7] if stats[7] and stats[7] != '--' else "0"
                fls = stats[12] if stats[12] and stats[12] != '--' else "0"

                if mins == "0" or mins == "0:00":
                    stats_line = '<span class="dnp">  Did not play</span>'
                    player_line = name_part  # No +/- for DNP
                else:
                    fg_m, fg_a = parse_shooting(fg)
                    three_m, three_a = parse_shooting(threept)
                    ft_m, ft_a = parse_shooting(ft)
                    team_totals["fg_made"] += fg_m
                    team_totals["fg_att"] += fg_a
                    team_totals["three_made"] += three_m
                    team_totals["three_att"] += three_a
                    team_totals["ft_made"] += ft_m
                    team_totals["ft_att"] += ft_a
                    team_totals["pts"] += int(pts) if pts else 0
                    team_totals["orb"] += int(orb) if orb else 0
                    team_totals["drb"] += int(drb) if drb else 0
                    team_totals["ast"] += int(ast) if ast else 0
                    team_totals["stl"] += int(stl) if stl else 0
                    team_totals["blk"] += int(blk) if blk else 0
                    team_totals["to"] += int(to) if to else 0
                    team_totals["fls"] += int(fls) if fls else 0

                    fg_slash = to_slash(fg)
                    three_slash = to_slash(threept)
                    ft_slash = to_slash(ft)
                    stats_line = f"{mins:>3} {fg_slash:>6} {three_slash:>5} {ft_slash:>5} {orb:>3} {drb:>3} {ast:>3} {stl:>3} {blk:>3} {to:>3} {fls:>3} {pts:>4}"

            all_spans.append(f'<span class="{row_class}">{player_line}\n{stats_line}</span>')

        # Bench header (team colored)
        row_class = "row-even" if row_idx % 2 == 0 else "row-odd"
        all_spans.append(f'<span class="{row_class}" style="color: #{team_color};"><b>{team_abbrev} BENCH</b>\n{stats_header}</span>')
        row_idx += 1

        # Bench
        for a in bench_sorted:
            athlete = a.get("athlete", {})
            athlete_id = athlete.get("id", "")
            name = athlete.get("displayName", "Unknown")
            jersey = athlete.get("jersey", "")
            stats = a.get("stats", [])

            row_class = "row-even" if row_idx % 2 == 0 else "row-odd"
            row_idx += 1

            jersey_str = f"#{jersey}" if jersey else ""
            name_part = f"{name} {jersey_str}"

            # Get plus/minus for this player
            pm_val = player_plus_minus.get(athlete_id, 0)
            pm_str = f"+{pm_val}" if pm_val > 0 else str(pm_val)
            # Pad player line to align +/- at position 52 (before PTS column)
            padding = 52 - len(name_part)
            player_line = f'{name_part}{" " * padding}<span class="plusminus">{pm_str:>4}</span>'

            if not stats or len(stats) < 13:
                stats_line = '<span class="dnp">  Did not play</span>'
                player_line = name_part  # No +/- for DNP
            else:
                mins = stats[0] if stats[0] and stats[0] != '--' else "0"
                pts = stats[1] if stats[1] and stats[1] != '--' else "0"
                fg = stats[2] if stats[2] and stats[2] != '--' else "0-0"
                threept = stats[3] if stats[3] and stats[3] != '--' else "0-0"
                ft = stats[4] if stats[4] and stats[4] != '--' else "0-0"
                orb = stats[10] if stats[10] and stats[10] != '--' else "0"
                drb = stats[11] if stats[11] and stats[11] != '--' else "0"
                ast = stats[6] if stats[6] and stats[6] != '--' else "0"
                stl = stats[8] if stats[8] and stats[8] != '--' else "0"
                blk = stats[9] if stats[9] and stats[9] != '--' else "0"
                to = stats[7] if stats[7] and stats[7] != '--' else "0"
                fls = stats[12] if stats[12] and stats[12] != '--' else "0"

                if mins == "0" or mins == "0:00":
                    stats_line = '<span class="dnp">  Did not play</span>'
                    player_line = name_part  # No +/- for DNP
                else:
                    fg_m, fg_a = parse_shooting(fg)
                    three_m, three_a = parse_shooting(threept)
                    ft_m, ft_a = parse_shooting(ft)
                    team_totals["fg_made"] += fg_m
                    team_totals["fg_att"] += fg_a
                    team_totals["three_made"] += three_m
                    team_totals["three_att"] += three_a
                    team_totals["ft_made"] += ft_m
                    team_totals["ft_att"] += ft_a
                    team_totals["pts"] += int(pts) if pts else 0
                    team_totals["orb"] += int(orb) if orb else 0
                    team_totals["drb"] += int(drb) if drb else 0
                    team_totals["ast"] += int(ast) if ast else 0
                    team_totals["stl"] += int(stl) if stl else 0
                    team_totals["blk"] += int(blk) if blk else 0
                    team_totals["to"] += int(to) if to else 0
                    team_totals["fls"] += int(fls) if fls else 0

                    fg_slash = to_slash(fg)
                    three_slash = to_slash(threept)
                    ft_slash = to_slash(ft)
                    stats_line = f"{mins:>3} {fg_slash:>6} {three_slash:>5} {ft_slash:>5} {orb:>3} {drb:>3} {ast:>3} {stl:>3} {blk:>3} {to:>3} {fls:>3} {pts:>4}"

            all_spans.append(f'<span class="{row_class}">{player_line}\n{stats_line}</span>')

        # Totals header (team colored)
        row_class = "row-even" if row_idx % 2 == 0 else "row-odd"
        all_spans.append(f'<span class="{row_class}" style="color: #{team_color};"><b>{team_abbrev} TOTALS</b>\n{stats_header}</span>')
        row_idx += 1

        # Totals data
        fg_total = f"{team_totals['fg_made']}/{team_totals['fg_att']}"
        three_total = f"{team_totals['three_made']}/{team_totals['three_att']}"
        ft_total = f"{team_totals['ft_made']}/{team_totals['ft_att']}"
        totals_line = f"    {fg_total:>6} {three_total:>5} {ft_total:>5} {team_totals['orb']:>3} {team_totals['drb']:>3} {team_totals['ast']:>3} {team_totals['stl']:>3} {team_totals['blk']:>3} {team_totals['to']:>3} {team_totals['fls']:>3} {team_totals['pts']:>4}"
        # Percentages
        fg_pct = f"{100 * team_totals['fg_made'] / team_totals['fg_att']:.0f}%" if team_totals['fg_att'] > 0 else "0%"
        three_pct = f"{100 * team_totals['three_made'] / team_totals['three_att']:.0f}%" if team_totals['three_att'] > 0 else "0%"
        ft_pct = f"{100 * team_totals['ft_made'] / team_totals['ft_att']:.0f}%" if team_totals['ft_att'] > 0 else "0%"
        pct_line = f"    {fg_pct:>6} {three_pct:>5} {ft_pct:>5}"

        row_class = "row-even" if row_idx % 2 == 0 else "row-odd"
        all_spans.append(f'<span class="{row_class}">{totals_line}\n{pct_line}</span>')

        content_lines.append("".join(all_spans))
        content_lines.append("")

    # No bottom links - navigation is at top

    content = "\n".join(content_lines)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{away_abbrev} vs {home_abbrev} - USC WBB</title>
    <meta name="data-loaded" content="{now_iso}">
    <style>
        * {{
            box-sizing: border-box;
        }}
        body {{
            font-family: monospace;
            background: #ffffff;
            color: #1a1a1a;
            padding: 16px;
            max-width: 100%;
            margin: 0 auto;
            line-height: 1.3;
            overflow-x: hidden;
        }}
        pre {{
            white-space: pre-wrap;
            word-wrap: break-word;
            overflow-wrap: break-word;
            margin: 0;
            font-size: 12px;
        }}
        a {{
            color: #0066cc;
        }}
        .row-even {{
            background: #f0f0f0;
            display: block;
            width: 55ch;
            margin: 0;
            padding: 0;
        }}
        .row-odd {{
            background: transparent;
            display: block;
            width: 55ch;
            margin: 0;
            padding: 0;
        }}
        .game-flow {{
            line-height: 0.5;
            display: block;
        }}
        .usc-dots {{
            color: #990000;
        }}
        .dnp {{
            color: #999999;
        }}
        .plusminus {{
            color: #999999;
        }}
        .live-clock {{
            color: #cc0000;
            font-weight: bold;
        }}
    </style>
</head>
<body>
<pre>
{content}
</pre>
<script>
(function() {{
    const dataLoaded = new Date(document.querySelector('meta[name="data-loaded"]').content);
    const pageLoaded = new Date();

    function formatTime(date) {{
        return date.toLocaleTimeString('en-US', {{ hour: 'numeric', minute: '2-digit', second: '2-digit', hour12: true }});
    }}

    function timeAgo(date) {{
        const seconds = Math.floor((new Date() - date) / 1000);
        if (seconds < 60) return 'just now';
        const minutes = Math.floor(seconds / 60);
        if (minutes < 60) return minutes + ' min ago';
        const hours = Math.floor(minutes / 60);
        if (hours < 24) return hours + ' hr ago';
        const days = Math.floor(hours / 24);
        return days + ' day' + (days > 1 ? 's' : '') + ' ago';
    }}

    function updateTimestamps() {{
        const el = document.getElementById('timestamps');
        if (el) {{
            const pageLoadedStr = 'Page loaded: ' + formatTime(pageLoaded);
            const pageAgo = '(' + timeAgo(pageLoaded) + ')';
            const pagePadding = 55 - pageLoadedStr.length - pageAgo.length;
            const pageSpaces = pagePadding > 0 ? ' '.repeat(pagePadding) : ' ';

            const dataLoadedStr = 'Data loaded: ' + formatTime(dataLoaded);
            const dataAgo = '(' + timeAgo(dataLoaded) + ')';
            const dataPadding = 55 - dataLoadedStr.length - dataAgo.length;
            const dataSpaces = dataPadding > 0 ? ' '.repeat(dataPadding) : ' ';

            el.innerHTML = pageLoadedStr + pageSpaces + pageAgo + '\\n' + dataLoadedStr + dataSpaces + dataAgo;
        }}
    }}

    updateTimestamps();
    setInterval(updateTimestamps, 60000); // Update every minute
}})();
</script>
</body>
</html>
"""
    return html


def main():
    force_update = "--force" in sys.argv

    print("Fetching USC Women's Basketball data...")

    # Get schedule and scoreboard (lightweight calls)
    schedule = get_team_schedule()
    scoreboard = get_scoreboard()

    # Check if we should update
    should_update, reason = is_game_live_or_imminent(schedule, scoreboard)

    if not should_update and not force_update:
        print(f"Skipping update: {reason}")
        print("Use --force to update anyway")
        sys.exit(1)  # Non-zero exit tells workflow to skip commit

    print(f"Updating: {reason}" if should_update else "Forced update")

    # Fetch rankings
    rankings = get_rankings()

    # Fetch roster with stats (only if no live game, to save API calls)
    usc_game = find_usc_game(scoreboard, schedule)
    roster = []
    if not usc_game:
        print("Fetching player stats...")
        roster = get_roster_with_stats()

    # Generate HTML
    html = generate_game_html(usc_game, schedule, rankings, roster)

    # Write output
    output_path = Path(__file__).parent.parent / "index.html"
    output_path.write_text(html)
    print(f"Written to {output_path}")

    # Generate full schedule page
    schedule_html = generate_schedule_html(schedule, rankings)
    schedule_path = Path(__file__).parent.parent / "schedule.html"
    schedule_path.write_text(schedule_html)
    print(f"Written to {schedule_path}")

    # Generate individual game pages for completed games
    games_dir = Path(__file__).parent.parent / "games"
    games_dir.mkdir(exist_ok=True)

    events = schedule.get("events", [])
    completed = [e for e in events if e.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("state") == "post"]

    # Get current team records from schedule (most recent game has current records)
    team_records = {}
    if completed:
        # Get records from the most recent completed game
        latest_comp = completed[-1].get("competitions", [{}])[0]
        for competitor in latest_comp.get("competitors", []):
            abbrev = competitor.get("team", {}).get("abbreviation", "")
            records = competitor.get("records", [])
            for rec in records:
                if rec.get("type") == "total":
                    team_records[abbrev] = rec.get("summary", "")
                    break

    print(f"Generating {len(completed)} game pages...")
    for event in completed:
        event_id = event.get("id", "")
        if event_id:
            try:
                game_html = generate_game_page(event_id, rankings, team_records)
                game_path = games_dir / f"{event_id}.html"
                game_path.write_text(game_html)
            except Exception as e:
                print(f"  Error generating game {event_id}: {e}")
    print(f"Written game pages to {games_dir}")


if __name__ == "__main__":
    main()
