#!/usr/bin/env python3
"""
Fetch USC Women's Basketball game data from ESPN API and generate static HTML.

Usage:
    python fetch_games.py          # Only update if game is live or starting within 60 min
    python fetch_games.py --force  # Always update (used hourly and for manual triggers)
"""

import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Pacific Time zone
PT = ZoneInfo("America/Los_Angeles")

# USC Women's Basketball team ID on ESPN
USC_TEAM_ID = "30"
NU_TEAM_ID = "77"
TEAM_NAME = "USC Trojans"
SPORT = "basketball"
LEAGUE = "womens-college-basketball"

BASE_API = f"https://site.api.espn.com/apis/site/v2/sports/{SPORT}/{LEAGUE}"

# How many minutes before game start to begin frequent updates
PREGAME_WINDOW_MINUTES = 60

# Version string based on last commit
try:
    _commit_hash = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL).decode().strip()
    _commit_date = subprocess.check_output(["git", "log", "-1", "--format=%ai"], stderr=subprocess.DEVNULL).decode().strip()
    _commit_dt = datetime.fromisoformat(_commit_date).astimezone(ZoneInfo("America/Los_Angeles"))
    VERSION = _commit_dt.strftime("v%Y.%m.%d-%H:%M") + "." + _commit_hash
except Exception:
    VERSION = "v0.0.0"


def fetch_json(url: str) -> dict:
    """Fetch JSON from URL."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


ODDS_CACHE_PATH = Path(__file__).parent.parent / "data" / "odds_cache.json"


def load_odds_cache() -> dict:
    """Load cached odds data from disk."""
    if ODDS_CACHE_PATH.exists():
        try:
            return json.loads(ODDS_CACHE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_odds_cache(cache: dict):
    """Save odds cache to disk."""
    ODDS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ODDS_CACHE_PATH.write_text(json.dumps(cache, indent=2))


def fetch_game_odds(event_id: str, home_display_name: str, away_display_name: str) -> dict | None:
    """Fetch pregame betting odds for a game from The Odds API.

    Returns cached data if available. Fetches from API only if not cached.
    Returns None if no API key or no matching game found.
    """
    cache = load_odds_cache()
    if event_id in cache:
        return cache[event_id]

    api_key = os.environ.get("ODDS_API_KEY", "")
    if not api_key:
        return None

    url = (
        f"https://api.the-odds-api.com/v4/sports/basketball_wncaab/odds"
        f"?apiKey={api_key}&regions=us&markets=h2h,spreads,totals&oddsFormat=american"
    )

    try:
        data = fetch_json(url)
    except Exception as e:
        print(f"  Odds API error: {e}")
        return None

    # Find matching game by team names
    matched = None
    for game in data:
        api_home = game.get("home_team", "")
        api_away = game.get("away_team", "")
        if api_home == home_display_name and api_away == away_display_name:
            matched = game
            break

    if not matched or not matched.get("bookmakers"):
        return None

    book = matched["bookmakers"][0]
    markets = {m["key"]: m for m in book.get("markets", [])}

    odds_data = {}

    # Spread
    spreads = markets.get("spreads", {}).get("outcomes", [])
    if spreads:
        # Find the home team spread
        for outcome in spreads:
            if outcome.get("name") == home_display_name:
                odds_data["spread"] = {
                    "team": home_display_name,
                    "line": str(outcome.get("point", "")),
                    "price": str(outcome.get("price", "")),
                }
                break

    # Total
    totals = markets.get("totals", {}).get("outcomes", [])
    if totals:
        over = next((o for o in totals if o.get("name") == "Over"), {})
        under = next((o for o in totals if o.get("name") == "Under"), {})
        odds_data["total"] = {
            "line": str(over.get("point", "")),
            "over_price": str(over.get("price", "")),
            "under_price": str(under.get("price", "")),
        }

    # Moneyline (h2h)
    h2h = markets.get("h2h", {}).get("outcomes", [])
    if h2h:
        home_ml = next((o for o in h2h if o.get("name") == home_display_name), {})
        away_ml = next((o for o in h2h if o.get("name") == away_display_name), {})
        odds_data["moneyline"] = {
            "home": str(home_ml.get("price", "")),
            "away": str(away_ml.get("price", "")),
            "home_team": home_display_name,
            "away_team": away_display_name,
        }

    if odds_data:
        cache[event_id] = odds_data
        save_odds_cache(cache)
        print(f"  Cached odds for event {event_id}")
        return odds_data

    return None


def calculate_plus_minus(plays, boxscore, home_team_id):
    """Calculate plus/minus for each player by tracking who's on court during scoring."""
    plus_minus = {}
    on_court = {}

    players_data = boxscore.get("players", [])
    for team_data in players_data:
        team_id = team_data.get("team", {}).get("id", "")
        statistics = team_data.get("statistics", [])
        if statistics:
            athletes = statistics[0].get("athletes", [])
            starters = [a.get("athlete", {}).get("id") for a in athletes if a.get("starter")]
            on_court[team_id] = set(starters)
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

        home_diff = home_score - prev_home_score
        away_diff = away_score - prev_away_score

        if home_diff != 0 or away_diff != 0:
            for team_id, players_on in on_court.items():
                for athlete_id in players_on:
                    if athlete_id in plus_minus:
                        if team_id == home_team_id:
                            plus_minus[athlete_id] += home_diff - away_diff
                        else:
                            plus_minus[athlete_id] += away_diff - home_diff

        prev_home_score = home_score
        prev_away_score = away_score

    return plus_minus


def get_roster_with_stats(team_id=USC_TEAM_ID) -> list:
    """Get team roster with current season stats aggregated from game box scores."""
    # Get schedule to find completed games
    schedule_url = f"{BASE_API}/teams/{team_id}/schedule"
    schedule_data = fetch_json(schedule_url)

    events = schedule_data.get("events", [])
    completed = [e for e in events if e.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("state") == "post"]

    # Aggregate stats from each game
    # ESPN indices: 0=MIN, 1=PTS, 2=FG, 3=3PT, 4=FT, 5=REB, 6=AST, 7=TO, 8=STL, 9=BLK, 10=OREB, 11=DREB, 12=PF
    player_totals = {}

    def parse_shooting(stat):
        if not stat or stat == '--':
            return (0, 0)
        parts = stat.replace("/", "-").split("-")
        if len(parts) == 2:
            try:
                return (int(parts[0]), int(parts[1]))
            except ValueError:
                pass
        return (0, 0)

    for event in completed:
        event_id = event.get("id")
        if not event_id:
            continue

        try:
            summary_url = f"{BASE_API}/summary?event={event_id}"
            game_data = fetch_json(summary_url)

            boxscore = game_data.get("boxscore", {})
            players = boxscore.get("players", [])

            # Calculate plus/minus for this game
            game_pm = {}
            plays = game_data.get("plays", [])
            if plays:
                header = game_data.get("header", {})
                header_comps = header.get("competitions", [{}])[0]
                header_competitors = header_comps.get("competitors", [])
                home_comp = next((c for c in header_competitors if c.get("homeAway") == "home"), {})
                home_id = home_comp.get("team", {}).get("id", "")
                game_pm = calculate_plus_minus(plays, boxscore, home_id)

            for team in players:
                if team.get("team", {}).get("id") != team_id:
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
                    if len(stats) < 13:
                        continue

                    # Parse stats (handle DNP)
                    try:
                        mins = int(stats[0]) if stats[0] and stats[0] != '--' else 0
                        pts = int(stats[1]) if stats[1] and stats[1] != '--' else 0
                        ast = int(stats[6]) if stats[6] and stats[6] != '--' else 0
                        stl = int(stats[8]) if stats[8] and stats[8] != '--' else 0
                        blk = int(stats[9]) if stats[9] and stats[9] != '--' else 0
                        to = int(stats[7]) if stats[7] and stats[7] != '--' else 0
                        orb = int(stats[10]) if stats[10] and stats[10] != '--' else 0
                        drb = int(stats[11]) if stats[11] and stats[11] != '--' else 0
                        fls = int(stats[12]) if stats[12] and stats[12] != '--' else 0
                    except (ValueError, IndexError):
                        continue

                    fg_m, fg_a = parse_shooting(stats[2] if stats[2] and stats[2] != '--' else "0-0")
                    three_m, three_a = parse_shooting(stats[3] if stats[3] and stats[3] != '--' else "0-0")
                    ft_m, ft_a = parse_shooting(stats[4] if stats[4] and stats[4] != '--' else "0-0")

                    # Only count if player actually played
                    if mins == 0:
                        continue

                    if athlete_id not in player_totals:
                        player_totals[athlete_id] = {
                            "name": athlete.get("displayName", "Unknown"),
                            "jersey": athlete.get("jersey", ""),
                            "min": 0, "pts": 0, "ast": 0, "stl": 0, "blk": 0,
                            "fg_made": 0, "fg_att": 0, "three_made": 0, "three_att": 0,
                            "ft_made": 0, "ft_att": 0, "orb": 0, "drb": 0,
                            "to": 0, "fls": 0, "pm": 0, "gp": 0
                        }

                    t = player_totals[athlete_id]
                    t["min"] += mins
                    t["pts"] += pts
                    t["ast"] += ast
                    t["stl"] += stl
                    t["blk"] += blk
                    t["to"] += to
                    t["orb"] += orb
                    t["drb"] += drb
                    t["fls"] += fls
                    t["fg_made"] += fg_m
                    t["fg_att"] += fg_a
                    t["three_made"] += three_m
                    t["three_att"] += three_a
                    t["ft_made"] += ft_m
                    t["ft_att"] += ft_a
                    t["pm"] += game_pm.get(athlete_id, 0)
                    t["gp"] += 1

        except Exception:
            continue

    # Return raw totals
    players = []
    for athlete_id, t in player_totals.items():
        if t["gp"] > 0:
            players.append({
                "name": t["name"],
                "jersey": t["jersey"],
                "gp": t["gp"],
                "min": t["min"],
                "fg_made": t["fg_made"], "fg_att": t["fg_att"],
                "three_made": t["three_made"], "three_att": t["three_att"],
                "ft_made": t["ft_made"], "ft_att": t["ft_att"],
                "orb": t["orb"], "drb": t["drb"],
                "ast": t["ast"], "stl": t["stl"], "blk": t["blk"],
                "to": t["to"], "fls": t["fls"], "pts": t["pts"], "pm": t["pm"],
            })

    # Sort by minutes desc, points desc, last name asc (same as game page)
    players.sort(key=lambda x: (-x.get("min", 0), -x.get("pts", 0), x.get("name", "").split()[-1] if x.get("name") else "ZZZ"))
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


def get_team_schedule(team_id=USC_TEAM_ID) -> dict:
    """Get team's schedule and recent results."""
    url = f"{BASE_API}/teams/{team_id}/schedule"
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


def get_b1g_standings() -> list:
    """Get Big Ten conference standings from ESPN API."""
    url = f"https://site.web.api.espn.com/apis/v2/sports/{SPORT}/{LEAGUE}/standings?group=7"
    data = fetch_json(url)
    entries = data.get("standings", {}).get("entries", [])

    # Extract playoff seed from stats array and sort by it
    for entry in entries:
        seed = 99
        for stat in entry.get("stats", []):
            if stat.get("type") == "playoffseed":
                try:
                    seed = int(stat.get("displayValue", "99"))
                except ValueError:
                    pass
                break
        entry["_seed"] = seed

    entries.sort(key=lambda e: e["_seed"])
    return entries


def get_b1g_leaders(team_id_map: dict) -> dict:
    """Get B1G conference stat leaders from ESPN core API.

    Args:
        team_id_map: dict mapping team ID (str) to abbreviation (str),
                     built from standings data.

    Returns:
        Ordered dict: {category_display: [{name, team, value}, ...], ...}
    """
    url = ("https://sports.core.api.espn.com/v2/sports/basketball/leagues/"
           "womens-college-basketball/seasons/2026/types/2/groups/7/leaders")
    data = fetch_json(url)

    target_cats = {
        "pointsPerGame": "PTS PER GAME",
        "3PointsMadePerGame": "3PT PER GAME",
        "reboundsPerGame": "REB PER GAME",
        "assistsPerGame": "AST PER GAME",
        "stealsPerGame": "STL PER GAME",
        "blocksPerGame": "BLK PER GAME",
    }
    cat_order = list(target_cats.keys())

    # Collect leaders per category and unique athlete refs
    raw = {}  # cat_name -> [(display_value, athlete_ref, team_abbrev), ...]
    athlete_refs = {}  # athlete_ref -> None (to deduplicate)

    for cat in data.get("categories", []):
        name = cat.get("name", "")
        if name not in target_cats:
            continue
        entries = []
        for leader in cat.get("leaders", [])[:10]:
            value = leader.get("displayValue", "")
            athlete_ref = leader.get("athlete", {}).get("$ref", "")
            team_ref = leader.get("team", {}).get("$ref", "")

            # Extract team ID from $ref URL
            team_match = re.search(r"/teams/(\d+)", team_ref)
            team_id = team_match.group(1) if team_match else ""
            team_abbrev = team_id_map.get(team_id, "???")

            entries.append((value, athlete_ref, team_abbrev))
            if athlete_ref:
                athlete_refs[athlete_ref] = None

        raw[name] = entries

    # Resolve athlete names
    athlete_names = {}
    for ref in athlete_refs:
        try:
            adata = fetch_json(ref)
            athlete_names[ref] = adata.get("displayName", "Unknown")
        except Exception:
            athlete_names[ref] = "Unknown"

    # Build result in category order
    result = {}
    for cat_name in cat_order:
        if cat_name not in raw:
            continue
        display = target_cats[cat_name]
        leaders = []
        for value, athlete_ref, team_abbrev in raw[cat_name]:
            leaders.append({
                "name": athlete_names.get(athlete_ref, "Unknown"),
                "team": team_abbrev,
                "value": value,
            })
        result[display] = leaders

    return result


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


def find_usc_game(scoreboard: dict, schedule: dict, team_id=USC_TEAM_ID) -> dict | None:
    """Find team's live or recent game from scoreboard or schedule."""
    # First check scoreboard
    for event in scoreboard.get("events", []):
        competitions = event.get("competitions", [])
        for comp in competitions:
            competitors = comp.get("competitors", [])
            for c in competitors:
                c_team_id = c.get("team", {}).get("id", "")
                if c_team_id == team_id:
                    return {"event": event, "competition": comp}

    # Also check schedule for live game (not always on scoreboard)
    for event in schedule.get("events", []):
        comp = event.get("competitions", [{}])[0]
        status = comp.get("status", {}).get("type", {})
        state = status.get("state", "")
        if state not in ("pre", "post", ""):  # Game in progress (covers "in", halftime, etc.)
            return {"event": event, "competition": comp}

    return None


def is_game_live_or_imminent(schedule: dict, scoreboard: dict, team_id=USC_TEAM_ID) -> tuple[bool, str]:
    """
    Check if a team has a game that is:
    - Currently in progress
    - Starting within PREGAME_WINDOW_MINUTES

    Returns (should_update, reason)
    """
    now = datetime.now(timezone.utc)  # Use UTC for comparison since ESPN uses UTC

    # First check scoreboard and schedule for live game
    usc_game = find_usc_game(scoreboard, schedule, team_id=team_id)
    if usc_game:
        state = usc_game["competition"].get("status", {}).get("type", {}).get("state", "")
        if state == "post":
            # Game just ended - update to show final
            return True, "Game just finished"
        elif state not in ("pre", ""):
            # Game in progress (covers "in", halftime, etc.)
            return True, "Game is LIVE"

    # Check schedule for upcoming games
    events = schedule.get("events", [])
    for event in events:
        comp = event.get("competitions", [{}])[0]
        status = comp.get("status", {}).get("type", {})
        state = status.get("state", "")

        if state not in ("pre", "post", ""):
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


def generate_game_html(game_data: dict | None, schedule_data: dict, rankings: dict, roster: list,
                       team_id=USC_TEAM_ID, team_abbrev="USC", home_page="index.html", schedule_page="schedule.html",
                       games_dir="games",
                       other_game_data: dict | None = None, other_schedule: dict | None = None,
                       other_team_id=None, other_team_abbrev="", other_games_dir="") -> str:
    """Generate the main game page HTML."""
    now = datetime.now(PT)
    now_str = now.strftime("%I:%M:%S %p")
    now_iso = now.isoformat()

    content_lines = []
    content_lines.append(f'<span id="timestamps">Data loaded: {now_str}</span>')
    content_lines.append("")
    if team_abbrev == "USC":
        content_lines.append('<b>USC</b>  <a href="nu.html">NU</a>  <a href="b1g.html">B1G</a>')
    else:
        content_lines.append('<a href="index.html">USC</a>  <b>NU</b>  <a href="b1g.html">B1G</a>')
    content_lines.append("")
    content_lines.append(f'<a href="{schedule_page}">Full Schedule/Results</a>')
    content_lines.append("")
    content_lines.append("=" * 47)

    # Build list of today's games across both teams
    # Each entry: (sort_key, abbrev, matchup, status_str)
    #   matchup = "at Illinois" or "vs Iowa"
    #   status_str = "LIVE 2nd 8:58 70-62" (with HTML) or "5:00 PM"
    today_pt = now.date()
    today_games = []

    teams_info = [(team_id, team_abbrev, games_dir, game_data, schedule_data)]
    if other_schedule is not None and other_team_id is not None:
        teams_info.append((other_team_id, other_team_abbrev, other_games_dir, other_game_data, other_schedule))

    for t_id, t_abbrev, t_games_dir, t_game_data, t_schedule in teams_info:
        # Check for live game
        if t_game_data:
            comp = t_game_data["competition"]
            eid = t_game_data["event"].get("id", "")
            state = comp.get("status", {}).get("type", {}).get("state", "")

            if state not in ("pre", "post", ""):
                try:
                    summary = get_game_summary(eid)
                    summary_comps = summary.get("header", {}).get("competitions", [])
                    if summary_comps:
                        comp = summary_comps[0]
                except Exception:
                    pass

                status = comp.get("status", {})
                period = status.get("period", 0)
                clock = status.get("displayClock", "")
                period_names = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}
                period_str = period_names.get(period, f"OT{period - 4}" if period > 4 else "")
                if status.get("type", {}).get("detail", "").lower().startswith("half"):
                    period_str = "Half"
                    clock = ""

                competitors = comp.get("competitors", [])
                us = next((c for c in competitors if c.get("team", {}).get("id") == t_id), {})
                opp = next((c for c in competitors if c.get("team", {}).get("id") != t_id), {})
                our_score = us.get("score", "0")
                opp_score = opp.get("score", "0")
                opp_name = opp.get("team", {}).get("location", opp.get("team", {}).get("abbreviation", "OPP"))
                home_away = "vs" if us.get("homeAway") == "home" else "at"

                clock_part = f" {clock}" if clock else ""
                live_link = f'<a href="{t_games_dir}/{eid}.html"><span style="color: #cc0000; font-weight: bold;">LIVE</span></a>'
                matchup = f"{home_away} {opp_name}"
                status_str = f"{live_link} {period_str}{clock_part} {our_score}-{opp_score}"
                today_games.append((0, t_abbrev, matchup, status_str))
                continue

        # Check schedule for today's pregame
        for sched_event in t_schedule.get("events", []):
            sched_comp = sched_event.get("competitions", [{}])[0]
            sched_state = sched_comp.get("status", {}).get("type", {}).get("state", "")
            if sched_state != "pre":
                continue
            date_str = sched_comp.get("date", "")
            if not date_str:
                continue
            try:
                game_time_utc = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                game_time_pt = game_time_utc.astimezone(PT)
                if game_time_pt.date() == today_pt:
                    sched_competitors = sched_comp.get("competitors", [])
                    us = next((c for c in sched_competitors if c.get("team", {}).get("id") == t_id), {})
                    opp = next((c for c in sched_competitors if c.get("team", {}).get("id") != t_id), {})
                    opp_name = opp.get("team", {}).get("location", opp.get("team", {}).get("abbreviation", "OPP"))
                    home_away = "vs" if us.get("homeAway") == "home" else "at"
                    time_str = game_time_pt.strftime("%-I:%M %p")
                    matchup = f"{home_away} {opp_name}"
                    today_games.append((game_time_utc.timestamp(), t_abbrev, matchup, time_str))
            except Exception:
                pass

    if today_games:
        today_games.sort(key=lambda x: x[0])
        # Align columns if multiple games
        if len(today_games) > 1:
            max_matchup = max(len(g[2]) for g in today_games)
            for _, abbrev, matchup, status_str in today_games:
                content_lines.append("")
                content_lines.append(f"{abbrev:<3} {matchup:<{max_matchup}} {status_str}")
        else:
            _, abbrev, matchup, status_str = today_games[0]
            content_lines.append("")
            content_lines.append(f"{abbrev} {matchup} {status_str}")
    else:
        content_lines.append("\nNo game today.")

    # Show player season stats
    if roster:
        content_lines.append("")
        content_lines.append("=" * 47)
        stats_header = " MIN ORB DRB AST STL BLK  TO FLS      FG     3PT      FT  PTS"
        all_spans = []
        row_idx = 0

        # Section header (team colored)
        team_color = "990000" if team_abbrev == "USC" else "4E2A84"
        row_class = "row-even" if row_idx % 2 == 0 else "row-odd"
        all_spans.append(f'<span class="{row_class}" style="color: #{team_color};"><b>{team_abbrev} SEASON STATS</b>\n{stats_header}</span>')
        row_idx += 1

        for p in roster:
            name = p.get("name", "")
            jersey = p.get("jersey", "")
            jersey_str = f"#{jersey}" if jersey else ""
            name_part = f"{name} {jersey_str}"

            mins = p.get("min", 0)
            fg_made = p.get("fg_made", 0)
            fg_att = p.get("fg_att", 0)
            three_made = p.get("three_made", 0)
            three_att = p.get("three_att", 0)
            ft_made = p.get("ft_made", 0)
            ft_att = p.get("ft_att", 0)
            orb = p.get("orb", 0)
            drb = p.get("drb", 0)
            ast = p.get("ast", 0)
            stl = p.get("stl", 0)
            blk = p.get("blk", 0)
            to = p.get("to", 0)
            fls = p.get("fls", 0)
            pts = p.get("pts", 0)

            fg_pct = f"{f'{100 * fg_made / fg_att:.2f}%':>7}" if fg_att > 0 else "     --"
            three_pct = f"{f'{100 * three_made / three_att:.2f}%':>7}" if three_att > 0 else "     --"
            ft_pct = f"{f'{100 * ft_made / ft_att:.2f}%':>7}" if ft_att > 0 else "     --"
            pm_val = p.get("pm", 0)
            pm_str = f"+{pm_val}" if pm_val > 0 else str(pm_val)
            grey_part = f"{fg_pct:<8}{three_pct:<8}{ft_pct:<8}{pm_str:>4} "
            name_line = f'{name_part:<33}<span style="color:#999">{grey_part}</span>'

            fg_str = f"{fg_made}/{fg_att}"
            three_str = f"{three_made}/{three_att}"
            ft_str = f"{ft_made}/{ft_att}"
            stats_line = f"{mins:>4}{orb:>4}{drb:>4}{ast:>4}{stl:>4}{blk:>4}{to:>4}{fls:>4}{fg_str:>8}{three_str:>8}{ft_str:>8}{pts:>5} "

            row_class = "row-even" if row_idx % 2 == 0 else "row-odd"
            all_spans.append(f'<span class="{row_class}">{name_line}\n{stats_line}</span>')
            row_idx += 1

        content_lines.append("".join(all_spans))

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
        opponent = next((c for c in competitors if c.get("team", {}).get("id") != team_id), None)
        if opponent:
            opp_abbrev = opponent.get("team", {}).get("abbreviation", "OPP")
            home_away = "vs" if opponent.get("homeAway") == "away" else "at"

            # Get rankings from lookup
            opp_rank = rankings.get(opp_abbrev, 0)
            usc_rank = rankings.get(team_abbrev, 0)

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
        usc = next((c for c in competitors if c.get("team", {}).get("id") == team_id), None)
        opponent = next((c for c in competitors if c.get("team", {}).get("id") != team_id), None)

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

    content_lines.append(f"\n{VERSION}")

    content = "\n".join(content_lines)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=700">
    <title>{team_abbrev} Women's Basketball</title>
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
            overflow-x: auto;
        }}
        pre {{
            white-space: pre;
            min-width: 55ch;
            margin: 0;
            font-size: 12px;
        }}
        a {{
            color: #0066cc;
        }}
        .row-even {{
            background: #f0f0f0;
            display: block;
            margin: 0 -16px;
            padding: 0 16px;
        }}
        .row-odd {{
            background: transparent;
            display: block;
            margin: 0 -16px;
            padding: 0 16px;
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
            const pagePadding = 61 - pageLoadedStr.length - pageAgo.length;
            const pageSpaces = pagePadding > 0 ? ' '.repeat(pagePadding) : ' ';

            const dataLoadedStr = 'Data loaded: ' + formatTime(dataLoaded);
            const dataAgo = '(' + timeAgo(dataLoaded) + ')';
            const dataPadding = 61 - dataLoadedStr.length - dataAgo.length;
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


def generate_schedule_html(schedule_data: dict, rankings: dict,
                           team_id=USC_TEAM_ID, team_abbrev="USC", home_page="index.html", games_dir="games") -> str:
    """Generate the full schedule/results page."""
    now = datetime.now(PT)
    now_str = now.strftime("%I:%M:%S %p")
    now_iso = now.isoformat()

    content_lines = []
    content_lines.append(f'<span id="timestamps">Data loaded: {now_str}</span>')
    content_lines.append("")
    if team_abbrev == "USC":
        content_lines.append('<b>USC</b>  <a href="nu.html">NU</a>  <a href="b1g.html">B1G</a>')
    else:
        content_lines.append('<a href="index.html">USC</a>  <b>NU</b>  <a href="b1g.html">B1G</a>')
    content_lines.append("")
    content_lines.append(f'<a href="{home_page}">Back to Home</a>')
    content_lines.append("")
    content_lines.append("Full Schedule/Results")
    content_lines.append("=" * 47)

    events = schedule_data.get("events", [])

    # Split into completed and upcoming
    completed = [e for e in events if e.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("state") == "post"]
    upcoming = [e for e in events if e.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("state") != "post"]

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
        usc = next((c for c in competitors if c.get("team", {}).get("id") == team_id), None)
        opponent = next((c for c in competitors if c.get("team", {}).get("id") != team_id), None)

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

        game_link = f'<a href="{games_dir}/{event_id}.html">{date_str} {result} {usc_score}-{opp_score} {home_away} {opp_str}</a>'
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
        opponent = next((c for c in competitors if c.get("team", {}).get("id") != team_id), None)

        if not opponent:
            continue

        opp_team = opponent.get("team", {})
        opp_abbrev = opp_team.get("abbreviation", "OPP")
        opp_school = opp_team.get("location", opp_abbrev)
        home_away = "vs" if opponent.get("homeAway") == "away" else "at"

        # Ranking
        opp_rank = rankings.get(opp_abbrev, 0)
        opp_str = f"#{opp_rank} {opp_school}" if opp_rank else opp_school

        if state not in ("pre", "post", ""):
            event_id = event.get("id", "")
            # Replace game time with red "LIVE" label
            date_raw_dt = comp.get("date", "")
            try:
                dt = datetime.fromisoformat(date_raw_dt.replace("Z", "+00:00"))
                dt_pt = dt.astimezone(PT)
                live_date = dt_pt.strftime("%b %d")
            except Exception:
                live_date = date_str.split()[0] if date_str else ""
            content_lines.append(f'<a href="{games_dir}/{event_id}.html">{live_date} <span style="color: #cc0000; font-weight: bold;">LIVE</span> {home_away} {opp_str}</a>')
        else:
            content_lines.append(f"{date_str} {home_away} {opp_str}")

    content_lines.append(f"\n{VERSION}")

    content = "\n".join(content_lines)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=700">
    <title>{team_abbrev} WBB Schedule</title>
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
            overflow-x: auto;
        }}
        pre {{
            white-space: pre;
            min-width: 55ch;
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
            const pagePadding = 61 - pageLoadedStr.length - pageAgo.length;
            const pageSpaces = pagePadding > 0 ? ' '.repeat(pagePadding) : ' ';

            const dataLoadedStr = 'Data loaded: ' + formatTime(dataLoaded);
            const dataAgo = '(' + timeAgo(dataLoaded) + ')';
            const dataPadding = 61 - dataLoadedStr.length - dataAgo.length;
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


def generate_standings_html(standings: list, rankings: dict, leaders: dict = None) -> str:
    """Generate B1G conference standings page."""
    now = datetime.now(PT)
    now_str = now.strftime("%I:%M:%S %p")
    now_iso = now.isoformat()

    content_lines = []
    content_lines.append(f'<span id="timestamps">Data loaded: {now_str}</span>')
    content_lines.append("")
    content_lines.append('<a href="index.html">USC</a>  <a href="nu.html">NU</a>  <b>B1G</b>')
    content_lines.append("")
    content_lines.append("Big Ten Standings")
    content_lines.append("=" * 47)

    # Header row
    content_lines.append(f'{"":>2}  {"Team":<18} {"Conf":>7} {"Overall":>7} {"Strk":>5}')
    content_lines.append("-" * 47)

    row_idx = 0
    standings_spans = []
    for entry in standings:
        team = entry.get("team", {})
        abbrev = team.get("abbreviation", "???")
        location = team.get("location", abbrev)
        seed = entry.get("_seed", 0)

        # Get rank from rankings
        rank = rankings.get(abbrev, 0)
        rank_str = f"#{rank} " if rank else ""
        team_display = f"{rank_str}{location}"
        if len(team_display) > 18:
            team_display = team_display[:18]

        # Extract stats (flat list with type as key)
        conf_record = ""
        overall_record = ""
        streak = ""
        for stat in entry.get("stats", []):
            stype = stat.get("type", "")
            if stype == "vsconf":
                conf_record = stat.get("displayValue", "")
            elif stype == "total":
                overall_record = stat.get("displayValue", "")
            elif stype == "streak":
                streak = stat.get("displayValue", "")

        # Build the row text
        line_text = f"{seed:>2}  {team_display:<18} {conf_record:>7} {overall_record:>7} {streak:>5}"

        # Highlight USC and NU rows with team colors
        row_class = "row-even" if row_idx // 2 % 2 == 0 else "row-odd"
        if abbrev == "USC":
            standings_spans.append(f'<span class="{row_class}" style="color: #990000;"><b>{line_text}</b></span>')
        elif abbrev == "NU":
            standings_spans.append(f'<span class="{row_class}" style="color: #4E2A84;"><b>{line_text}</b></span>')
        else:
            standings_spans.append(f'<span class="{row_class}">{line_text}</span>')
        row_idx += 1
    content_lines.append("".join(standings_spans))

    # Conference leaders sections
    if leaders:
        for cat_display, entries in leaders.items():
            content_lines.append("")
            content_lines.append("=" * 47)
            content_lines.append(f'{"":>2}  {cat_display:<28} {"Value":>7}')
            content_lines.append("-" * 47)

            leader_spans = []
            for i, entry in enumerate(entries):
                rank = i + 1
                name = entry["name"]
                team = entry["team"]
                value = entry["value"]

                line_text = f"{rank:>2}  {name:<21} {team:<7} {value:>7}"

                row_class = "row-even" if i // 2 % 2 == 0 else "row-odd"
                if team == "USC":
                    leader_spans.append(f'<span class="{row_class}" style="color: #990000;"><b>{line_text}</b></span>')
                elif team == "NU":
                    leader_spans.append(f'<span class="{row_class}" style="color: #4E2A84;"><b>{line_text}</b></span>')
                else:
                    leader_spans.append(f'<span class="{row_class}">{line_text}</span>')
            content_lines.append("".join(leader_spans))

    content_lines.append(f"\n{VERSION}")

    content = "\n".join(content_lines)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=700">
    <title>Big Ten WBB Standings</title>
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
            overflow-x: auto;
        }}
        pre {{
            white-space: pre;
            min-width: 55ch;
            margin: 0;
            font-size: 12px;
        }}
        a {{
            color: #0066cc;
        }}
        .row-even {{
            background: #f0f0f0;
            display: block;
            margin: 0 -16px;
            padding: 0 16px;
        }}
        .row-odd {{
            background: transparent;
            display: block;
            margin: 0 -16px;
            padding: 0 16px;
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
            const pagePadding = 61 - pageLoadedStr.length - pageAgo.length;
            const pageSpaces = pagePadding > 0 ? ' '.repeat(pagePadding) : ' ';

            const dataLoadedStr = 'Data loaded: ' + formatTime(dataLoaded);
            const dataAgo = '(' + timeAgo(dataLoaded) + ')';
            const dataPadding = 61 - dataLoadedStr.length - dataAgo.length;
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


def generate_game_page(event_id: str, rankings: dict = None, team_records: dict = None,
                       team_id=USC_TEAM_ID, team_abbrev="USC", home_page="index.html", schedule_page="schedule.html",
                       odds: dict = None) -> str:
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
    is_live = status_state not in ("pre", "post", "")
    game_clock = comp.get("status", {}).get("displayClock", "")
    game_period = comp.get("status", {}).get("period", 0)

    # Parse team fouls and timeouts from play-by-play
    home_fouls = ""
    away_fouls = ""
    home_timeouts = ""
    away_timeouts = ""

    plays = game.get("plays", [])
    if plays:
        home_id = home_team.get("id", "")
        away_id = away_team.get("id", "")

        # Count fouls in current quarter (fouls reset each quarter in NCAA WBB)
        home_foul_count = 0
        away_foul_count = 0
        # Count team timeouts used in the game (4 per game in NCAA WBB)
        home_to_used = 0
        away_to_used = 0

        for p in plays:
            ptype = p.get("type", {}).get("text", "")
            period = p.get("period", {}).get("number", 0)
            play_team_id = p.get("team", {}).get("id", "") if p.get("team") else ""

            # Fouls in current quarter
            if "Foul" in ptype and play_team_id and period == game_period:
                if play_team_id == home_id:
                    home_foul_count += 1
                elif play_team_id == away_id:
                    away_foul_count += 1

            # Team timeouts (exclude OfficialTVTimeOut which has no team)
            if "timeout" in ptype.lower() and play_team_id:
                if play_team_id == home_id:
                    home_to_used += 1
                elif play_team_id == away_id:
                    away_to_used += 1

        home_fouls = str(home_foul_count)
        away_fouls = str(away_foul_count)
        # NCAA WBB: 4 timeouts per game (+ 1 per OT)
        ot_periods = max(0, game_period - 4)
        total_timeouts = 4 + ot_periods
        home_timeouts = str(total_timeouts - home_to_used)
        away_timeouts = str(total_timeouts - away_to_used)

    # Page width is 55 characters
    PAGE_WIDTH = 55
    # Team centers: USC at 14, opponent at 42 (1-indexed), center at 28
    LEFT_CENTER = 13   # 0-indexed position 14
    RIGHT_CENTER = 41  # 0-indexed position 42
    PAGE_CENTER = 27   # 0-indexed position 28

    content_lines = []
    content_lines.append(f'<span id="timestamps">Data loaded: {now_str}</span>')
    content_lines.append("")
    content_lines.append(f'<a href="../{schedule_page}">&lt; {team_abbrev} Schedule</a>')
    content_lines.append("")

    # Determine our team and opponent - always show our team first (left side)
    usc_is_home = home_team.get("id") == team_id
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
    usc_school = usc_team.get("location", team_abbrev)
    usc_name = usc_team.get("name", "")
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

    # Build header lines - same layout for live and final
    usc_school_full = f"{usc_rank_str}{usc_school}"
    opp_school_full = f"{opp_rank_str}{opp_school}"

    # For live games, use red clock as the center status
    if is_live:
        period_name = f"Q{game_period}" if game_period <= 4 else f"OT{game_period - 4}"
        center_text = f"{period_name} {game_clock}"
        center_html = f'<span class="live-clock">{center_text}</span>'
    else:
        center_text = status_detail
        center_html = f"<b>{center_text}</b>"

    # Line 1: School names and status
    usc_school_pad = LEFT_CENTER - len(usc_school_full) // 2
    status_pad = PAGE_CENTER - len(center_text) // 2 - (usc_school_pad + len(usc_school_full))
    opp_school_pad = RIGHT_CENTER - len(opp_school_full) // 2 - (usc_school_pad + len(usc_school_full) + status_pad + len(center_text))

    line1 = " " * usc_school_pad + f"<b>{usc_school_full}</b>"
    line1 += " " * max(1, status_pad) + center_html
    line1 += " " * max(1, opp_school_pad) + f"<b>{opp_school_full}</b>"

    # Line 2: Team names (bold) and score
    score_str = f"{usc_score} - {opp_score}"
    usc_name_pad = LEFT_CENTER - len(usc_name) // 2
    score_pad = PAGE_CENTER - len(score_str) // 2 - (usc_name_pad + len(usc_name))
    opp_name_pad = RIGHT_CENTER - len(opp_name) // 2 - (usc_name_pad + len(usc_name) + score_pad + len(score_str))

    line2 = " " * usc_name_pad + f"<b>{usc_name}</b>"
    line2 += " " * max(1, score_pad) + score_str
    line2 += " " * max(1, opp_name_pad) + f"<b>{opp_name}</b>"

    # Line 3: Records
    usc_rec_pad = LEFT_CENTER - len(usc_record) // 2
    opp_rec_pad = RIGHT_CENTER - len(opp_record) // 2 - (usc_rec_pad + len(usc_record))

    line3 = " " * usc_rec_pad + usc_record
    line3 += " " * max(1, opp_rec_pad) + opp_record

    content_lines.append(line1.rstrip())
    content_lines.append(line2.rstrip())

    # For live games, merge TF/TOL into the records and next line
    if is_live:
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

        # Records line with TF in the center
        fouls_str = f"{usc_fouls} TF {opp_fouls}" if usc_fouls and opp_fouls else ""
        usc_rec_left = " " * (LEFT_CENTER - len(usc_record) // 2) + usc_record
        fouls_pad = PAGE_CENTER - len(fouls_str) // 2 - len(usc_rec_left)
        opp_rec_start = RIGHT_CENTER - len(opp_record) // 2
        opp_rec_pad = opp_rec_start - (len(usc_rec_left) + max(1, fouls_pad) + len(fouls_str))
        line3 = usc_rec_left + " " * max(1, fouls_pad) + fouls_str + " " * max(1, opp_rec_pad) + opp_record
        content_lines.append(line3.rstrip())

        # TOL line centered
        if usc_timeouts and opp_timeouts:
            timeouts_str = f"{usc_timeouts} TOL {opp_timeouts}"
            timeouts_padding = " " * (PAGE_CENTER - len(timeouts_str) // 2)
            content_lines.append(f"{timeouts_padding}{timeouts_str}")
    else:
        content_lines.append(line3.rstrip())

    # Quarter by quarter box score - centered within 55 chars, USC first
    num_periods = max(len(usc_quarters), len(opp_quarters), 4)
    period_labels = ["1", "2", "3", "4"] + [f"OT{i}" for i in range(1, num_periods - 3)]
    period_labels = period_labels[:num_periods]

    # Build box score rows
    box_header = "    " + "".join(f"{p:>3}" for p in period_labels) + "   T"
    box_width = len(box_header)
    box_padding = (PAGE_WIDTH - box_width) // 2
    pad = " " * box_padding

    content_lines.append(pad + box_header)
    content_lines.append(pad + "-" * box_width)

    # USC first, then opponent - pad quarters to full width
    usc_q_padded = usc_quarters + [""] * (num_periods - len(usc_quarters))
    opp_q_padded = opp_quarters + [""] * (num_periods - len(opp_quarters))
    usc_row = f"{team_abbrev:<4}" + "".join(f"{q:>3}" for q in usc_q_padded) + f" {usc_score:>3}"
    opp_row = f"{opp_abbrev_display:<4}" + "".join(f"{q:>3}" for q in opp_q_padded) + f" {opp_score:>3}"
    content_lines.append(pad + usc_row)
    content_lines.append(pad + opp_row)
    content_lines.append("")

    # Betting odds section (if available)
    if odds:
        spread = odds.get("spread", {})
        total = odds.get("total", {})
        moneyline = odds.get("moneyline", {})

        # Map full team names to abbreviations
        name_to_abbrev = {
            home_team.get("displayName", ""): home_abbrev,
            away_team.get("displayName", ""): away_abbrev,
        }

        content_lines.append("CURRENT LINE".center(PAGE_WIDTH))

        # Line 1: Spread and O/U
        if spread and total:
            spread_abbrev = name_to_abbrev.get(spread.get("team", ""), home_abbrev)
            line = spread.get("line", "")
            if line and not line.startswith("-"):
                line = "+" + line
            spread_str = f"{spread_abbrev} {line} ({spread.get('price', '')})"
            total_str = f"O/U {total.get('line', '')}"
            odds_line1 = f"{spread_str}  {total_str}"
            content_lines.append(odds_line1.center(PAGE_WIDTH))

        # Line 2: Moneyline
        if moneyline:
            ml_home_abbrev = name_to_abbrev.get(moneyline.get("home_team", ""), home_abbrev)
            ml_away_abbrev = name_to_abbrev.get(moneyline.get("away_team", ""), away_abbrev)
            home_price = moneyline.get("home", "")
            away_price = moneyline.get("away", "")
            if home_price and not home_price.startswith("-"):
                home_price = "+" + home_price
            if away_price and not away_price.startswith("-"):
                away_price = "+" + away_price
            ml_str = f"{ml_home_abbrev} {home_price}    {ml_away_abbrev} {away_price}"
            content_lines.append(ml_str.center(PAGE_WIDTH))

        content_lines.append("")

    # Game Flow visualization (based on game lead)
    plays = game.get("plays", [])
    scoring_plays = [p for p in plays if p.get("scoringPlay")]

    # Get opponent color for game flow
    opp_color = opp_team.get("color", "888888")
    opp_abbrev = opp_abbrev_display

    if scoring_plays:
        # Settings: 12 "=" columns per quarter (50 sec each), plus "+" breaks
        cols_per_quarter = 13  # 1 "+" break + 12 "=" columns (50 sec each in 10-min quarter)
        total_cols = num_periods * cols_per_quarter + 1  # +1 for final "+"

        # For live games, calculate cutoff column from current period/clock
        # Dots only appear up to where the game has actually reached
        cutoff_col = total_cols  # default: show everything (completed games)
        if is_live and game_period > 0:
            try:
                clock_parts = game_clock.split(":")
                mins_left = int(clock_parts[0])
                secs_left = int(clock_parts[1]) if len(clock_parts) > 1 else 0
                secs_elapsed = 600 - (mins_left * 60 + secs_left)
                current_seg = min(12, max(1, int(secs_elapsed / 50) + 1)) if secs_elapsed > 0 else 0
            except Exception:
                current_seg = 0
            cutoff_col = (game_period - 1) * cols_per_quarter + current_seg

        # Track USC lead at each column
        # Positive = USC leading, negative = opponent leading
        lead_at_col = {}

        for play in scoring_plays:
            period = play.get("period", {}).get("number", 1)
            clock_str = play.get("clock", {}).get("displayValue", "10:00")
            away_sc = play.get("awayScore", 0)
            home_sc = play.get("homeScore", 0)

            # Parse clock to determine which 50-sec segment we're in (1-12)
            try:
                parts = clock_str.split(":")
                minutes_left = int(parts[0])
                seconds_left = int(parts[1]) if len(parts) > 1 else 0
                seconds_remaining = minutes_left * 60 + seconds_left
                seconds_elapsed = 600 - seconds_remaining  # 10-min quarters

                # 12 segments of 50 seconds each
                if seconds_elapsed <= 0:
                    segment = 1
                else:
                    segment = min(12, int(seconds_elapsed / 50) + 1)
            except:
                segment = 6  # default to middle

            # Column: break at 0, segments 1-12 at cols 1-12
            col = (period - 1) * cols_per_quarter + segment

            # Lead from USC perspective: positive = USC leading
            if usc_is_home:
                lead = home_sc - away_sc
            else:
                lead = away_sc - home_sc
            lead_at_col[col] = lead

        # Fill in gaps by carrying forward the last known lead
        # Break columns (multiples of cols_per_quarter) get None - no dots there
        # Columns past the cutoff get None (future game time, no dots yet)
        last_lead = 0
        filled_lead = []
        for col in range(total_cols):
            is_break = (col % cols_per_quarter == 0)
            if col in lead_at_col:
                last_lead = lead_at_col[col]
            if is_break or col > cutoff_col:
                filled_lead.append(None)  # No dots at break positions or future columns
            else:
                filled_lead.append(last_lead)

        # Calculate separate heights for USC (positive leads) and opponent (negative leads)
        valid_leads = [l for l in filled_lead if l is not None]
        max_usc_lead = max(0, max(valid_leads)) if valid_leads else 0
        max_opp_lead = abs(min(0, min(valid_leads))) if valid_leads else 0
        usc_height = max(1, (max_usc_lead + 2) // 3) if max_usc_lead > 0 else 0
        opp_height = max(1, (max_opp_lead + 2) // 3) if max_opp_lead > 0 else 0

        # Build the visualization
        # Total chart width = 7 (padding) + total_cols
        chart_width = 7 + total_cols
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
                content_lines.append(f'<span class="usc-dots"> {team_abbrev:<6}{line}</span>')
            else:
                content_lines.append(f'<span class="usc-dots">       {line}</span>')

        # Blank line before timeline to prevent overlap with compact line-height
        content_lines.append("")

        # Timeline: + at breaks, = for minutes
        timeline = ""
        for col in range(total_cols):
            if col % cols_per_quarter == 0:
                timeline += "+"
            else:
                timeline += "="
        content_lines.append(f"       {timeline}")

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
                content_lines.append(f'<span style="color: #{opp_color};"> {opp_abbrev:<6}{line}</span>')
            else:
                content_lines.append(f'<span style="color: #{opp_color};">       {line}</span>')

        content_lines.append('</span>')
        content_lines.append("")

    # Calculate lead changes, times tied, and biggest leads from scoring plays
    if scoring_plays:
        lead_changes = 0
        times_tied = 0
        usc_biggest_lead = 0
        opp_biggest_lead = 0
        last_leader = None   # last team that held a lead (ignores ties)
        prev_leader = None   # leader after previous play (None = tied)

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

            # Count lead changes (when a different team takes the lead, even through ties)
            if current_leader is not None and last_leader is not None and current_leader != last_leader:
                lead_changes += 1

            # Count times tied (when score becomes tied after not being tied)
            if current_leader is None and prev_leader is not None:
                times_tied += 1

            if current_leader is not None:
                last_leader = current_leader
            prev_leader = current_leader

        # Display lead stats
        content_lines.append(f"<b>Lead Changes:</b> {lead_changes}")
        content_lines.append(f"<b>Times Tied:</b> {times_tied}")
        usc_lead_str = str(usc_biggest_lead) if usc_biggest_lead > 0 else "N/A"
        opp_lead_str = str(opp_biggest_lead) if opp_biggest_lead > 0 else "N/A"
        content_lines.append(f"<b>Biggest Lead:</b> {team_abbrev}: {usc_lead_str}, {opp_abbrev}: {opp_lead_str}")
        content_lines.append("")

    # Game info
    venue = gameInfo.get("venue", {})
    venue_name = venue.get("fullName", "")
    attendance = gameInfo.get("attendance", 0)
    if venue_name:
        content_lines.append(f"<b>Venue:</b> {venue_name}")
    if attendance:
        content_lines.append(f"<b>Attendance:</b> {attendance:,}")
    content_lines.append("")

    # Calculate plus/minus from plays
    player_plus_minus = calculate_plus_minus(plays, boxscore, home_team.get("id", "")) if plays else {}

    # Stats header line for player stats (matches home page format)
    stats_header = " MIN ORB DRB AST STL BLK  TO FLS      FG     3PT      FT  PTS"

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

    # Calculate second chance points from play-by-play
    def calculate_second_chance_pts(plays, home_team_id, away_team_id):
        """Calculate second chance points by tracking offensive rebounds and subsequent scoring."""
        home_2ch = 0
        away_2ch = 0
        # Track which team is in a "second chance" state (got an offensive rebound)
        second_chance_team = None  # "home" or "away" or None

        for p in plays:
            ptype = p.get("type", {}).get("text", "")
            play_team_id = p.get("team", {}).get("id", "") if p.get("team") else ""
            score_val = p.get("scoreValue", 0)

            # Offensive rebound: team enters second chance state
            if "Offensive Rebound" in ptype and play_team_id:
                if play_team_id == home_team_id:
                    second_chance_team = "home"
                elif play_team_id == away_team_id:
                    second_chance_team = "away"
                continue

            # Scoring play: if team is in second chance state, count the points
            is_scoring = p.get("scoringPlay", False)
            if is_scoring and score_val and score_val > 0 and play_team_id:
                if second_chance_team == "home" and play_team_id == home_team_id:
                    home_2ch += score_val
                elif second_chance_team == "away" and play_team_id == away_team_id:
                    away_2ch += score_val
                # Made free throws don't end second chance (could be and-1 or multiple FTs)
                # Only end on made field goals (possession change)
                if "FreeThrow" not in ptype:
                    second_chance_team = None
                continue

            # A missed shot by the second-chance team doesn't end it
            # (they could get another offensive rebound)
            # But a missed shot by the OTHER team means they had possession,
            # so second chance is over
            if score_val and not is_scoring and play_team_id:
                if second_chance_team == "home" and play_team_id != home_team_id:
                    second_chance_team = None
                elif second_chance_team == "away" and play_team_id != away_team_id:
                    second_chance_team = None
                continue

            # Events that end the second chance opportunity
            if any(x in ptype for x in ("Defensive Rebound", "Turnover", "End Period",
                                         "Jumpball", "Dead Ball Rebound", "Steal")):
                second_chance_team = None

        return home_2ch, away_2ch

    home_2ch_pts, away_2ch_pts = calculate_second_chance_pts(
        plays, home_team.get("id", ""), away_team.get("id", "")
    ) if plays else (0, 0)

    # Team Stats section
    team_players = boxscore.get("players", [])
    if team_players:
        pre_stats = {}
        for td in team_players:
            tid = td.get("team", {}).get("id", "")
            tab = td.get("team", {}).get("abbreviation", "TEAM")
            ts = {"abbrev": tab, "fg_m": 0, "fg_a": 0, "three_m": 0, "three_a": 0,
                  "ft_m": 0, "ft_a": 0, "pts": 0, "orb": 0, "drb": 0,
                  "ast": 0, "stl": 0, "blk": 0, "to": 0, "fls": 0, "bench_pts": 0}
            stat_sections = td.get("statistics", [])
            if stat_sections:
                for a in stat_sections[0].get("athletes", []):
                    st = a.get("stats", [])
                    if not st or len(st) < 13:
                        continue
                    mins = st[0] if st[0] and st[0] != '--' else "0"
                    if mins == "0" or mins == "0:00":
                        continue
                    fm, fa = parse_shooting(st[2] if st[2] and st[2] != '--' else "0-0")
                    tm, ta = parse_shooting(st[3] if st[3] and st[3] != '--' else "0-0")
                    ftm, fta = parse_shooting(st[4] if st[4] and st[4] != '--' else "0-0")
                    p = int(st[1]) if st[1] and st[1] != '--' else 0
                    ts["fg_m"] += fm; ts["fg_a"] += fa
                    ts["three_m"] += tm; ts["three_a"] += ta
                    ts["ft_m"] += ftm; ts["ft_a"] += fta
                    ts["pts"] += p
                    ts["orb"] += int(st[10]) if st[10] and st[10] != '--' else 0
                    ts["drb"] += int(st[11]) if st[11] and st[11] != '--' else 0
                    ts["ast"] += int(st[6]) if st[6] and st[6] != '--' else 0
                    ts["stl"] += int(st[8]) if st[8] and st[8] != '--' else 0
                    ts["blk"] += int(st[9]) if st[9] and st[9] != '--' else 0
                    ts["to"] += int(st[7]) if st[7] and st[7] != '--' else 0
                    ts["fls"] += int(st[12]) if st[12] and st[12] != '--' else 0
                    if not a.get("starter"):
                        ts["bench_pts"] += p
            pre_stats[tid] = ts

        # Try to get advanced stats from boxscore teams data
        for td in boxscore.get("teams", []):
            tid = td.get("team", {}).get("id", "")
            if tid in pre_stats:
                for stat in td.get("statistics", []):
                    name = stat.get("name", "")
                    val = stat.get("displayValue", "0")
                    if name == "pointsInPaint":
                        pre_stats[tid]["pitp"] = val
                    elif name == "fastBreakPoints":
                        pre_stats[tid]["fb_pts"] = val
                    elif name == "turnoverPoints":
                        pre_stats[tid]["pts_off_to"] = val

        # Add second chance points from play-by-play calculation
        home_id = home_team.get("id", "")
        away_id = away_team.get("id", "")
        if home_id in pre_stats:
            pre_stats[home_id]["second_ch"] = str(home_2ch_pts)
        if away_id in pre_stats:
            pre_stats[away_id]["second_ch"] = str(away_2ch_pts)

        usc_tid = next((t for t in pre_stats if t == team_id), None)
        opp_tid = next((t for t in pre_stats if t != team_id), None)

        if usc_tid and opp_tid:
            usc_ts = pre_stats[usc_tid]
            opp_ts = pre_stats[opp_tid]

            content_lines.append("<b>Team Stats:</b>")
            content_lines.append(f"{'':>5}{'PTS':>3}  {'FG':>5} {'3PT':>5} {'FT':>5} {'OR/DR/TR':>8} {'A':>2} {'S':>2} {'B':>2}")

            for ts in [usc_ts, opp_ts]:
                ab = ts["abbrev"]
                fg = f"{ts['fg_m']}/{ts['fg_a']}"
                thr = f"{ts['three_m']}/{ts['three_a']}"
                ft = f"{ts['ft_m']}/{ts['ft_a']}"
                reb = f"{ts['orb']}/{ts['drb']}/{ts['orb']+ts['drb']}"
                content_lines.append(f"{ab:<5}{ts['pts']:>3}  {fg:>5} {thr:>5} {ft:>5} {reb:>8} {ts['ast']:>2} {ts['stl']:>2} {ts['blk']:>2}")
                fg_pct = f"{100*ts['fg_m']/ts['fg_a']:.1f}%" if ts['fg_a'] > 0 else "0.0%"
                thr_pct = f"{100*ts['three_m']/ts['three_a']:.1f}%" if ts['three_a'] > 0 else "0.0%"
                ft_pct = f"{100*ts['ft_m']/ts['ft_a']:.1f}%" if ts['ft_a'] > 0 else "0.0%"
                content_lines.append(f"{'':>10}{fg_pct:>5} {thr_pct:>5} {ft_pct:>5}")

            content_lines.append("")

            # Advanced stats table (only if ESPN provides the data)
            has_advanced = any(k in usc_ts for k in ("pitp", "fb_pts", "pts_off_to"))
            if has_advanced:
                content_lines.append(f"{'':>5}{'PITP':>4}{'FB PTS':>8}{'BNCH':>6}{'OR':>4}{'2CH':>5}{'TO':>4}{'POTO':>5}{'PF':>4}")
                for ts in [usc_ts, opp_ts]:
                    ab = ts["abbrev"]
                    pitp = ts.get("pitp", "-")
                    fb = ts.get("fb_pts", "-")
                    bnch = str(ts["bench_pts"])
                    orb = str(ts["orb"])
                    sch = ts.get("second_ch", "-")
                    to_v = str(ts["to"])
                    poto = ts.get("pts_off_to", "-")
                    pf = str(ts["fls"])
                    content_lines.append(f"{ab:<5}{pitp:>4}{fb:>8}{bnch:>6}{orb:>4}{sch:>5}{to_v:>4}{poto:>5}{pf:>4}")
                content_lines.append("")

    # Player stats for each team (USC first)
    players_data = boxscore.get("players", [])
    players_data_sorted = sorted(players_data, key=lambda t: t.get("team", {}).get("id") != team_id)

    # Build all spans with continuous zebra striping across both teams
    all_spans = []
    row_idx = 0

    for team_data in players_data_sorted:
        team = team_data.get("team", {})
        td_abbrev = team.get("abbreviation", "TEAM")
        td_id = team.get("id", "")

        # Get team color - use our team color for our team, team color for opponents
        if td_id == team_id:
            team_color = "990000" if team_abbrev == "USC" else "4E2A84"
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

        # Starters header (team colored)
        row_class = "row-even" if row_idx % 2 == 0 else "row-odd"
        all_spans.append(f'<span class="{row_class}" style="color: #{team_color};"><b>{td_abbrev} STARTERS</b>\n{stats_header}</span>')
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

            if not stats or len(stats) < 13:
                player_line = name_part
                stats_line = '<span class="dnp">  Did not play</span>'
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
                    player_line = name_part
                    stats_line = '<span class="dnp">  Did not play</span>'
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

                    fg_pct = f"{f'{100 * fg_m / fg_a:.2f}%':>7}" if fg_a > 0 else "     --"
                    three_pct = f"{f'{100 * three_m / three_a:.2f}%':>7}" if three_a > 0 else "     --"
                    ft_pct = f"{f'{100 * ft_m / ft_a:.2f}%':>7}" if ft_a > 0 else "     --"
                    pm_val = player_plus_minus.get(athlete_id, 0)
                    pm_str = f"+{pm_val}" if pm_val > 0 else str(pm_val)
                    grey_part = f"{fg_pct:<8}{three_pct:<8}{ft_pct:<8}{pm_str:>4} "
                    player_line = f'{name_part:<33}<span style="color:#999">{grey_part}</span>'

                    fg_str = f"{fg_m}/{fg_a}"
                    three_str = f"{three_m}/{three_a}"
                    ft_str = f"{ft_m}/{ft_a}"
                    stats_line = f"{mins:>4}{orb:>4}{drb:>4}{ast:>4}{stl:>4}{blk:>4}{to:>4}{fls:>4}{fg_str:>8}{three_str:>8}{ft_str:>8}{pts:>5} "

            all_spans.append(f'<span class="{row_class}">{player_line}\n{stats_line}</span>')

        # Bench header (team colored)
        row_class = "row-even" if row_idx % 2 == 0 else "row-odd"
        all_spans.append(f'<span class="{row_class}" style="color: #{team_color};"><b>{td_abbrev} BENCH</b>\n{stats_header}</span>')
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

            if not stats or len(stats) < 13:
                player_line = name_part
                stats_line = '<span class="dnp">  Did not play</span>'
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
                    player_line = name_part
                    stats_line = '<span class="dnp">  Did not play</span>'
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

                    fg_pct = f"{f'{100 * fg_m / fg_a:.2f}%':>7}" if fg_a > 0 else "     --"
                    three_pct = f"{f'{100 * three_m / three_a:.2f}%':>7}" if three_a > 0 else "     --"
                    ft_pct = f"{f'{100 * ft_m / ft_a:.2f}%':>7}" if ft_a > 0 else "     --"
                    pm_val = player_plus_minus.get(athlete_id, 0)
                    pm_str = f"+{pm_val}" if pm_val > 0 else str(pm_val)
                    grey_part = f"{fg_pct:<8}{three_pct:<8}{ft_pct:<8}{pm_str:>4} "
                    player_line = f'{name_part:<33}<span style="color:#999">{grey_part}</span>'

                    fg_str = f"{fg_m}/{fg_a}"
                    three_str = f"{three_m}/{three_a}"
                    ft_str = f"{ft_m}/{ft_a}"
                    stats_line = f"{mins:>4}{orb:>4}{drb:>4}{ast:>4}{stl:>4}{blk:>4}{to:>4}{fls:>4}{fg_str:>8}{three_str:>8}{ft_str:>8}{pts:>5} "

            all_spans.append(f'<span class="{row_class}">{player_line}\n{stats_line}</span>')

        # Totals header (team colored)
        row_class = "row-even" if row_idx % 2 == 0 else "row-odd"
        all_spans.append(f'<span class="{row_class}" style="color: #{team_color};"><b>{td_abbrev} TOTALS</b>\n{stats_header}</span>')
        row_idx += 1

        # Totals data
        fg_total = f"{team_totals['fg_made']}/{team_totals['fg_att']}"
        three_total = f"{team_totals['three_made']}/{team_totals['three_att']}"
        ft_total = f"{team_totals['ft_made']}/{team_totals['ft_att']}"

        fg_pct_val = f"{100 * team_totals['fg_made'] / team_totals['fg_att']:.2f}%" if team_totals['fg_att'] > 0 else "--"
        three_pct_val = f"{100 * team_totals['three_made'] / team_totals['three_att']:.2f}%" if team_totals['three_att'] > 0 else "--"
        ft_pct_val = f"{100 * team_totals['ft_made'] / team_totals['ft_att']:.2f}%" if team_totals['ft_att'] > 0 else "--"
        fg_pct = f"{fg_pct_val:>7}"
        three_pct = f"{three_pct_val:>7}"
        ft_pct = f"{ft_pct_val:>7}"

        pct_line = f"{'':33}<span style=\"color:#999\">{fg_pct:<8}{three_pct:<8}{ft_pct:<8}</span>"
        totals_line = f"    {team_totals['orb']:>4}{team_totals['drb']:>4}{team_totals['ast']:>4}{team_totals['stl']:>4}{team_totals['blk']:>4}{team_totals['to']:>4}{team_totals['fls']:>4}{fg_total:>8}{three_total:>8}{ft_total:>8}{team_totals['pts']:>5} "

        row_class = "row-even" if row_idx % 2 == 0 else "row-odd"
        all_spans.append(f'<span class="{row_class}">{pct_line}\n{totals_line}</span>')
        row_idx += 1

        # Add two blank lines (same zebra stripe) between our team and opponent sections
        if td_id == team_id:
            row_class = "row-even" if row_idx % 2 == 0 else "row-odd"
            all_spans.append(f'<span class="{row_class}">\n\n</span>')
            row_idx += 1

    content_lines.append("".join(all_spans))
    content_lines.append("")

    # No bottom links - navigation is at top
    content_lines.append(VERSION)

    content = "\n".join(content_lines)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=700">
    <title>{away_abbrev} vs {home_abbrev} - {team_abbrev} WBB</title>
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
            overflow-x: auto;
        }}
        pre {{
            white-space: pre;
            margin: 0;
            font-size: 12px;
        }}
        a {{
            color: #0066cc;
        }}
        .row-even {{
            background: #f0f0f0;
            display: block;
            margin: 0 -16px;
            padding: 0 16px;
        }}
        .row-odd {{
            background: transparent;
            display: block;
            margin: 0 -16px;
            padding: 0 16px;
        }}
        .game-flow {{
            line-height: 0.5;
            display: block;
        }}
        .usc-dots {{
            color: #{"990000" if team_abbrev == "USC" else "4E2A84"};
        }}
        .dnp {{
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
            const pagePadding = 61 - pageLoadedStr.length - pageAgo.length;
            const pageSpaces = pagePadding > 0 ? ' '.repeat(pagePadding) : ' ';

            const dataLoadedStr = 'Data loaded: ' + formatTime(dataLoaded);
            const dataAgo = '(' + timeAgo(dataLoaded) + ')';
            const dataPadding = 61 - dataLoadedStr.length - dataAgo.length;
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

    print("Fetching Women's Basketball data...")

    # Get schedules and scoreboard (lightweight calls)
    schedule = get_team_schedule()
    nu_schedule = get_team_schedule(team_id=NU_TEAM_ID)
    scoreboard = get_scoreboard()

    # Check if we should update (either team live/imminent triggers update)
    usc_should, usc_reason = is_game_live_or_imminent(schedule, scoreboard)
    nu_should, nu_reason = is_game_live_or_imminent(nu_schedule, scoreboard, team_id=NU_TEAM_ID)
    should_update = usc_should or nu_should
    reason = usc_reason if usc_should else nu_reason

    if not should_update and not force_update:
        print(f"Skipping update: {reason}")
        print("Use --force to update anyway")
        sys.exit(1)  # Non-zero exit tells workflow to skip commit

    print(f"Updating: {reason}" if should_update else "Forced update")

    # Fetch rankings
    rankings = get_rankings()
    now_utc = datetime.now(timezone.utc)

    # Find live/recent games for both teams (needed for cross-team display on homepages)
    usc_game = find_usc_game(scoreboard, schedule)
    nu_game = find_usc_game(scoreboard, nu_schedule, team_id=NU_TEAM_ID)

    # --- USC pages ---
    print("Generating USC pages...")

    print("Fetching USC player stats...")
    roster = get_roster_with_stats()

    # Generate HTML
    html = generate_game_html(usc_game, schedule, rankings, roster,
        other_game_data=nu_game, other_schedule=nu_schedule,
        other_team_id=NU_TEAM_ID, other_team_abbrev="NU", other_games_dir="nu-games")

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
    usc_games_dir = Path(__file__).parent.parent / "games"
    usc_games_dir.mkdir(exist_ok=True)

    events = schedule.get("events", [])
    completed = [e for e in events if e.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("state") == "post"]
    live = [e for e in events if e.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("state") not in ("pre", "post", "")]

    # Get current team records from schedule
    # Iterate all completed games so each team's record reflects their latest appearance
    team_records = {}
    for event in completed:
        for competitor in event.get("competitions", [{}])[0].get("competitors", []):
            abbrev = competitor.get("team", {}).get("abbreviation", "")
            records = competitor.get("records", [])
            for rec in records:
                if rec.get("type") == "total":
                    team_records[abbrev] = rec.get("summary", "")
                    break

    # Fetch odds for imminent/live USC games
    usc_odds_map = {}
    imminent_pre = [e for e in events if e.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("state") == "pre"]
    for event in imminent_pre + live:
        comp = event.get("competitions", [{}])[0]
        date_str = comp.get("date", "")
        if date_str:
            try:
                game_time = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                if (game_time - now_utc).total_seconds() / 60 <= PREGAME_WINDOW_MINUTES:
                    eid = event.get("id", "")
                    competitors = comp.get("competitors", [])
                    home_c = next((c for c in competitors if c.get("homeAway") == "home"), {})
                    away_c = next((c for c in competitors if c.get("homeAway") == "away"), {})
                    home_name = home_c.get("team", {}).get("displayName", "")
                    away_name = away_c.get("team", {}).get("displayName", "")
                    if eid and home_name and away_name:
                        odds = fetch_game_odds(eid, home_name, away_name)
                        if odds:
                            usc_odds_map[eid] = odds
            except Exception:
                pass

    games_to_generate = completed + live
    print(f"Generating {len(games_to_generate)} USC game pages...")
    for event in games_to_generate:
        event_id = event.get("id", "")
        if event_id:
            try:
                game_html = generate_game_page(event_id, rankings, team_records,
                    odds=usc_odds_map.get(event_id))
                game_path = usc_games_dir / f"{event_id}.html"
                game_path.write_text(game_html)
            except Exception as e:
                print(f"  Error generating game {event_id}: {e}")
    print(f"Written USC game pages to {usc_games_dir}")

    # --- NU pages ---
    print("Generating NU pages...")

    print("Fetching NU player stats...")
    nu_roster = get_roster_with_stats(team_id=NU_TEAM_ID)

    nu_html = generate_game_html(nu_game, nu_schedule, rankings, nu_roster,
        team_id=NU_TEAM_ID, team_abbrev="NU", home_page="nu.html", schedule_page="nu-schedule.html",
        games_dir="nu-games",
        other_game_data=usc_game, other_schedule=schedule,
        other_team_id=USC_TEAM_ID, other_team_abbrev="USC", other_games_dir="games")
    nu_path = Path(__file__).parent.parent / "nu.html"
    nu_path.write_text(nu_html)
    print(f"Written to {nu_path}")

    nu_schedule_html = generate_schedule_html(nu_schedule, rankings,
        team_id=NU_TEAM_ID, team_abbrev="NU", home_page="nu.html", games_dir="nu-games")
    nu_schedule_path = Path(__file__).parent.parent / "nu-schedule.html"
    nu_schedule_path.write_text(nu_schedule_html)
    print(f"Written to {nu_schedule_path}")

    nu_games_dir = Path(__file__).parent.parent / "nu-games"
    nu_games_dir.mkdir(exist_ok=True)

    nu_events = nu_schedule.get("events", [])
    nu_completed = [e for e in nu_events if e.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("state") == "post"]
    nu_live = [e for e in nu_events if e.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("state") not in ("pre", "post", "")]

    nu_team_records = {}
    for event in nu_completed:
        for competitor in event.get("competitions", [{}])[0].get("competitors", []):
            abbrev = competitor.get("team", {}).get("abbreviation", "")
            records = competitor.get("records", [])
            for rec in records:
                if rec.get("type") == "total":
                    nu_team_records[abbrev] = rec.get("summary", "")
                    break

    # Fetch odds for imminent/live NU games
    nu_odds_map = {}
    nu_imminent_pre = [e for e in nu_events if e.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("state") == "pre"]
    for event in nu_imminent_pre + nu_live:
        comp = event.get("competitions", [{}])[0]
        date_str = comp.get("date", "")
        if date_str:
            try:
                game_time = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                if (game_time - now_utc).total_seconds() / 60 <= PREGAME_WINDOW_MINUTES:
                    eid = event.get("id", "")
                    competitors = comp.get("competitors", [])
                    home_c = next((c for c in competitors if c.get("homeAway") == "home"), {})
                    away_c = next((c for c in competitors if c.get("homeAway") == "away"), {})
                    home_name = home_c.get("team", {}).get("displayName", "")
                    away_name = away_c.get("team", {}).get("displayName", "")
                    if eid and home_name and away_name:
                        odds = fetch_game_odds(eid, home_name, away_name)
                        if odds:
                            nu_odds_map[eid] = odds
            except Exception:
                pass

    nu_games_to_generate = nu_completed + nu_live
    print(f"Generating {len(nu_games_to_generate)} NU game pages...")
    for event in nu_games_to_generate:
        event_id = event.get("id", "")
        if event_id:
            try:
                game_html = generate_game_page(event_id, rankings, nu_team_records,
                    team_id=NU_TEAM_ID, team_abbrev="NU", home_page="nu.html", schedule_page="nu-schedule.html",
                    odds=nu_odds_map.get(event_id))
                game_path = nu_games_dir / f"{event_id}.html"
                game_path.write_text(game_html)
            except Exception as e:
                print(f"  Error generating NU game {event_id}: {e}")
    print(f"Written NU game pages to {nu_games_dir}")

    # --- B1G standings page ---
    print("Generating B1G standings page...")
    standings = get_b1g_standings()

    # Build team ID -> abbreviation map from standings for leader lookups
    team_id_map = {}
    for entry in standings:
        team = entry.get("team", {})
        tid = str(team.get("id", ""))
        abbrev = team.get("abbreviation", "")
        if tid and abbrev:
            team_id_map[tid] = abbrev

    print("Fetching B1G conference leaders...")
    try:
        leaders = get_b1g_leaders(team_id_map)
    except Exception as e:
        print(f"  Error fetching leaders: {e}")
        leaders = {}

    standings_html = generate_standings_html(standings, rankings, leaders)
    standings_path = Path(__file__).parent.parent / "b1g.html"
    standings_path.write_text(standings_html)
    print(f"Written to {standings_path}")


if __name__ == "__main__":
    main()
