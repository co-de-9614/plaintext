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


if __name__ == "__main__":
    main()
