"""
Kalshi World Cup odds scraper module.

Uses the public Kalshi Trading API v2 (no auth needed) to fetch World Cup
match odds and converts probabilities to decimal odds for comparison.
"""

import json
import os
import re
import requests
from datetime import datetime, timezone

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kalshi_data.json")


def scrape_kalshi_odds():
    """
    Fetch all World Cup match odds from Kalshi.
    Returns a dict with 'matches' list and 'last_updated' timestamp.
    """
    import sys as _sys
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching Kalshi odds...", file=_sys.stderr)

    markets = []
    cursor = None

    while True:
        params = {
            "series_ticker": "KXWCGAME",
            "limit": 200,
            "status": "open",
        }
        if cursor:
            params["cursor"] = cursor

        resp = requests.get(
            f"{KALSHI_API}/markets",
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        markets.extend(data.get("markets", []))
        cursor = data.get("cursor")
        if not cursor or not data.get("markets"):
            break

    # Group markets by event_ticker (each match has 3 markets: home, tie, away)
    event_groups = {}
    for m in markets:
        event_ticker = m.get("event_ticker", "")
        if event_ticker not in event_groups:
            event_groups[event_ticker] = []
        event_groups[event_ticker].append(m)

    matches = []
    for event_ticker, group in event_groups.items():
        parsed = _parse_match_group(event_ticker, group)
        if parsed:
            matches.append(parsed)

    result = {
        "matches": sorted(matches, key=lambda m: m.get("date", "")),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }

    with open(DATA_FILE, "w") as f:
        json.dump(result, f, indent=2)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Kalshi: {len(matches)} matches saved.", file=_sys.stderr)
    return result


def load_cached_kalshi():
    """Load the most recently scraped Kalshi odds from disk."""
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return None


def _parse_match_group(event_ticker, markets):
    """Parse a group of 3 Kalshi markets (home/tie/away) into standardized odds."""
    draw_prob = 0.0
    team_probs = {}  # team_name -> prob
    close_time = ""

    # Extract home/away order from the title: "X vs Y Winner?"
    sample_title = markets[0].get("title", "") if markets else ""
    vs_match = re.search(r"(.+?)\s+vs\.?\s+(.+?)(?:\s+Winner)?\??$", sample_title, re.IGNORECASE)

    for m in markets:
        ticker = m.get("ticker", "")
        subtitle = m.get("yes_sub_title", "")

        price_str = m.get("yes_ask_dollars") or m.get("yes_bid_dollars") or "0"
        try:
            price = float(price_str)
        except (ValueError, TypeError):
            price = 0.0

        if ticker.endswith("-TIE") or subtitle.lower() == "tie":
            draw_prob = price
        else:
            if subtitle:
                team_probs[subtitle] = price

        if not close_time:
            close_time = m.get("expected_expiration_time", "") or m.get("close_time", "")

    if len(team_probs) < 2 or not vs_match:
        return None

    title_home = vs_match.group(1).strip()
    title_away = vs_match.group(2).strip()

    # Match title teams to team_probs keys
    home_team, home_prob = _find_team(title_home, team_probs)
    away_team, away_prob = _find_team(title_away, team_probs)

    if not home_team or not away_team:
        # Fallback: just use the two teams in order found
        teams = list(team_probs.items())
        home_team, home_prob = teams[0]
        away_team, away_prob = teams[1]

    return {
        "event": f"{home_team} vs {away_team}",
        "date": close_time,
        "home_team": home_team,
        "away_team": away_team,
        "home_prob": home_prob,
        "draw_prob": draw_prob,
        "away_prob": away_prob,
        "home_odds": _prob_to_decimal(home_prob),
        "draw_odds": _prob_to_decimal(draw_prob),
        "away_odds": _prob_to_decimal(away_prob),
    }


def _find_team(title_name, team_probs):
    """Find a team in team_probs that matches the title name."""
    title_lower = title_name.lower()
    for name, prob in team_probs.items():
        if name.lower() == title_lower or name.lower() in title_lower or title_lower in name.lower():
            return name, prob
    return None, 0.0



def _prob_to_decimal(prob):
    """Convert a probability (0-1) to decimal odds string."""
    if prob > 0:
        return f"{1 / prob:.2f}"
    return ""


if __name__ == "__main__":
    result = scrape_kalshi_odds()
    print(json.dumps(result, indent=2))
