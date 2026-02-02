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
    now = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")

    content_lines = []
    content_lines.append(f"USC WOMEN'S BASKETBALL")
    content_lines.append(f"Updated: {now}")
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
    <style>
        * {{
            box-sizing: border-box;
        }}
        body {{
            font-family: monospace;
            background: #1a1a1a;
            color: #e0e0e0;
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
            font-size: 14px;
        }}
        a {{
            color: #90caf9;
        }}
    </style>
</head>
<body>
<pre>
{content}
</pre>
</body>
</html>
"""
    return html


def generate_schedule_html(schedule_data: dict, rankings: dict) -> str:
    """Generate the full schedule/results page."""
    now = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")

    content_lines = []
    content_lines.append("USC WOMEN'S BASKETBALL")
    content_lines.append("Full Schedule/Results")
    content_lines.append(f"Updated: {now}")
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
        status = comp.get("status", {}).get("type", {})
        state = status.get("state", "")

        # Date
        date_raw = comp.get("date", "")
        if date_raw:
            try:
                dt = datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
                dt_pt = dt.astimezone(PT)
                if state == "pre":
                    date_str = dt_pt.strftime("%b %d %I:%M%p")
                else:
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

        opp_abbrev = opponent.get("team", {}).get("abbreviation", "OPP")
        home_away = "vs" if opponent.get("homeAway") == "away" else "at"

        # Ranking
        opp_rank = rankings.get(opp_abbrev, 0)
        opp_str = f"#{opp_rank} {opp_abbrev}" if opp_rank else opp_abbrev

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

        opp_abbrev = opponent.get("team", {}).get("abbreviation", "OPP")
        home_away = "vs" if opponent.get("homeAway") == "away" else "at"

        # Ranking
        opp_rank = rankings.get(opp_abbrev, 0)
        opp_str = f"#{opp_rank} {opp_abbrev}" if opp_rank else opp_abbrev

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
    <style>
        * {{
            box-sizing: border-box;
        }}
        body {{
            font-family: monospace;
            background: #1a1a1a;
            color: #e0e0e0;
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
            font-size: 14px;
        }}
        a {{
            color: #90caf9;
        }}
    </style>
</head>
<body>
<pre>
{content}
</pre>
</body>
</html>
"""
    return html


def generate_game_page(event_id: str) -> str:
    """Generate a detailed game report page."""
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
    home_record = home.get("record", [{}])[0].get("displayValue", "") if home.get("record") else ""
    away_record = away.get("record", [{}])[0].get("displayValue", "") if away.get("record") else ""

    # Quarter scores
    home_quarters = [q.get("displayValue", "0") for q in home.get("linescores", [])]
    away_quarters = [q.get("displayValue", "0") for q in away.get("linescores", [])]

    # Game status
    status = comp.get("status", {}).get("type", {})
    status_detail = status.get("detail", "Final")

    content_lines = []

    # Header
    content_lines.append(f"{away_team.get('displayName', away_abbrev):<24} {status_detail:^10} {home_team.get('displayName', home_abbrev):>24}")
    content_lines.append(f"{away_record:<24} {away_score:>4} - {home_score:<4} {home_record:>24}")
    content_lines.append("")

    # Quarter by quarter
    num_periods = max(len(home_quarters), len(away_quarters), 4)
    period_labels = ["1", "2", "3", "4"] + [f"OT{i}" for i in range(1, num_periods - 3)]
    period_labels = period_labels[:num_periods]

    header_row = "     " + "".join(f"{p:>4}" for p in period_labels) + "    T"
    content_lines.append(header_row)
    content_lines.append("-" * len(header_row))

    away_row = f"{away_abbrev:<5}" + "".join(f"{q:>4}" for q in away_quarters) + f"  {away_score:>3}"
    home_row = f"{home_abbrev:<5}" + "".join(f"{q:>4}" for q in home_quarters) + f"  {home_score:>3}"
    content_lines.append(away_row)
    content_lines.append(home_row)
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

    # Team stats comparison
    content_lines.append("Team Stats:")
    content_lines.append("-" * 40)

    teams_stats = boxscore.get("teams", [])
    away_stats = next((t for t in teams_stats if t.get("homeAway") == "away"), {})
    home_stats = next((t for t in teams_stats if t.get("homeAway") == "home"), {})

    away_stat_dict = {s.get("label"): s.get("displayValue") for s in away_stats.get("statistics", [])}
    home_stat_dict = {s.get("label"): s.get("displayValue") for s in home_stats.get("statistics", [])}

    stat_labels = ["FG", "3PT", "FT", "REB", "AST", "TO", "STL", "BLK"]
    content_lines.append(f"{'':>12} {away_abbrev:>10} {home_abbrev:>10}")
    for label in stat_labels:
        away_val = away_stat_dict.get(label, "-")
        home_val = home_stat_dict.get(label, "-")
        content_lines.append(f"{label:>12} {away_val:>10} {home_val:>10}")
    content_lines.append("")

    # Player stats for each team (USC first)
    players_data = boxscore.get("players", [])
    players_data_sorted = sorted(players_data, key=lambda t: t.get("team", {}).get("id") != USC_TEAM_ID)

    for team_data in players_data_sorted:
        team = team_data.get("team", {})
        team_name = team.get("shortDisplayName", team.get("abbreviation", "TEAM"))

        statistics = team_data.get("statistics", [])
        if not statistics:
            continue

        athletes = statistics[0].get("athletes", [])

        # Separate starters and bench
        starters = [a for a in athletes if a.get("starter")]
        bench = [a for a in athletes if not a.get("starter")]

        # Stats header line
        stats_header = " MIN    FG   3PT    FT  R  A  S  B TO PF PTS"

        # Helper to get sort key for player (pts desc, mins desc, reb desc)
        def player_sort_key(a):
            stats = a.get("stats", [])
            if not stats or len(stats) < 10:
                return (0, 0, 0)
            try:
                pts = int(stats[1]) if stats[1] and stats[1] != '--' else 0
                mins = int(stats[0]) if stats[0] and stats[0] != '--' else 0
                reb = int(stats[5]) if stats[5] and stats[5] != '--' else 0
                return (-pts, -mins, -reb)
            except:
                return (0, 0, 0)

        # Sort starters and bench by points
        starters_sorted = sorted(starters, key=player_sort_key)
        bench_sorted = sorted(bench, key=player_sort_key)

        # Starters section
        content_lines.append(f"{team_name} Starters:")
        content_lines.append(stats_header)

        row_idx = 0
        for a in starters_sorted:
            athlete = a.get("athlete", {})
            name = athlete.get("displayName", "Unknown")
            jersey = athlete.get("jersey", "")
            position = athlete.get("position", {}).get("abbreviation", "")
            stats = a.get("stats", [])

            row_class = "row-even" if row_idx % 2 == 0 else "row-odd"
            row_idx += 1

            # Player name line with number
            jersey_str = f"{int(jersey):>2}" if jersey else "  "
            player_line = f"#{jersey_str} {name} {position}" if position else f"#{jersey_str} {name}"

            # Stats line (indices: 0=MIN, 1=PTS, 2=FG, 3=3PT, 4=FT, 5=REB, 6=AST, 7=TO, 8=STL, 9=BLK, 12=PF)
            if not stats or len(stats) < 10:
                stats_line = "  Did not play"
            else:
                mins = stats[0] if stats[0] and stats[0] != '--' else "0"
                pts = stats[1] if stats[1] and stats[1] != '--' else "0"
                fg = stats[2] if stats[2] and stats[2] != '--' else "0-0"
                threept = stats[3] if stats[3] and stats[3] != '--' else "0-0"
                ft = stats[4] if stats[4] and stats[4] != '--' else "0-0"
                reb = stats[5] if stats[5] and stats[5] != '--' else "0"
                ast = stats[6] if stats[6] and stats[6] != '--' else "0"
                to = stats[7] if stats[7] and stats[7] != '--' else "0"
                stl = stats[8] if stats[8] and stats[8] != '--' else "0"
                blk = stats[9] if stats[9] and stats[9] != '--' else "0"
                pf = stats[12] if len(stats) > 12 and stats[12] and stats[12] != '--' else "0"

                if mins == "0" or mins == "0:00":
                    stats_line = "  Did not play"
                else:
                    stats_line = f"{mins:>5} {fg:>5} {threept:>5} {ft:>5} {reb:>2} {ast:>2} {stl:>2} {blk:>2} {to:>2} {pf:>2} {pts:>3}"

            content_lines.append(f'<span class="{row_class}">{player_line}\n{stats_line}</span>')

        # Bench section
        content_lines.append(f"{team_name} Bench:")
        content_lines.append(stats_header)

        row_idx = 0
        for a in bench_sorted:
            athlete = a.get("athlete", {})
            name = athlete.get("displayName", "Unknown")
            jersey = athlete.get("jersey", "")
            position = athlete.get("position", {}).get("abbreviation", "")
            stats = a.get("stats", [])

            row_class = "row-even" if row_idx % 2 == 0 else "row-odd"
            row_idx += 1

            # Player name line with number
            jersey_str = f"{int(jersey):>2}" if jersey else "  "
            player_line = f"#{jersey_str} {name} {position}" if position else f"#{jersey_str} {name}"

            # Stats line
            if not stats or len(stats) < 10:
                stats_line = "  Did not play"
            else:
                mins = stats[0] if stats[0] and stats[0] != '--' else "0"
                pts = stats[1] if stats[1] and stats[1] != '--' else "0"
                fg = stats[2] if stats[2] and stats[2] != '--' else "0-0"
                threept = stats[3] if stats[3] and stats[3] != '--' else "0-0"
                ft = stats[4] if stats[4] and stats[4] != '--' else "0-0"
                reb = stats[5] if stats[5] and stats[5] != '--' else "0"
                ast = stats[6] if stats[6] and stats[6] != '--' else "0"
                to = stats[7] if stats[7] and stats[7] != '--' else "0"
                stl = stats[8] if stats[8] and stats[8] != '--' else "0"
                blk = stats[9] if stats[9] and stats[9] != '--' else "0"
                pf = stats[12] if len(stats) > 12 and stats[12] and stats[12] != '--' else "0"

                if mins == "0" or mins == "0:00":
                    stats_line = "  Did not play"
                else:
                    stats_line = f"{mins:>5} {fg:>5} {threept:>5} {ft:>5} {reb:>2} {ast:>2} {stl:>2} {blk:>2} {to:>2} {pf:>2} {pts:>3}"

            content_lines.append(f'<span class="{row_class}">{player_line}\n{stats_line}</span>')

        content_lines.append("")

    # Link back
    content_lines.append('<a href="../schedule.html">Back to Schedule</a>')
    content_lines.append('<a href="../index.html">Back to Home</a>')

    content = "\n".join(content_lines)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{away_abbrev} vs {home_abbrev} - USC WBB</title>
    <style>
        * {{
            box-sizing: border-box;
        }}
        body {{
            font-family: monospace;
            background: #1a1a1a;
            color: #e0e0e0;
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
            color: #90caf9;
        }}
        .row-even {{
            background: #252525;
            display: block;
        }}
        .row-odd {{
            background: transparent;
            display: block;
        }}
    </style>
</head>
<body>
<pre>
{content}
</pre>
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

    print(f"Generating {len(completed)} game pages...")
    for event in completed:
        event_id = event.get("id", "")
        if event_id:
            try:
                game_html = generate_game_page(event_id)
                game_path = games_dir / f"{event_id}.html"
                game_path.write_text(game_html)
            except Exception as e:
                print(f"  Error generating game {event_id}: {e}")
    print(f"Written game pages to {games_dir}")


if __name__ == "__main__":
    main()
