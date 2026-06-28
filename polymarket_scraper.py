"""
Polymarket World Cup odds scraper module.

Uses the public Gamma API (no auth needed) to fetch World Cup match
odds and converts probabilities to decimal odds for comparison with
Singapore Pools.
"""

import json
import os
import requests
from datetime import datetime, timezone

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "polymarket_data.json")


def scrape_polymarket_odds():
    """
    Fetch all World Cup match odds from Polymarket.
    Returns a dict with 'matches' list and 'last_updated' timestamp.
    """
    import sys as _sys
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching Polymarket odds...", file=_sys.stderr)

    # Fetch all WC events (active=True covers both open and recently closed match markets)
    all_events = []
    offset = 0
    while True:
        resp = requests.get(
            f"{GAMMA_API}/events",
            params={"tag_slug": "fifa-world-cup", "limit": 100, "active": True, "offset": offset},
            timeout=15,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        all_events.extend(batch)
        if len(batch) < 100:
            break
        offset += 100

    now = datetime.now(timezone.utc)

    matches = []
    for event in all_events:
        title = event.get("title", "")
        if " vs" not in title and "–" not in title:
            continue
        # Skip player props and specials
        title_lower = title.lower()
        if any(kw in title_lower for kw in ["player props", "halftime", "half time", "exact score", "total goals"]):
            continue
        # Skip already-resolved matches (end date in the past by more than 3 hours)
        end_date_str = event.get("endDate", "")
        if end_date_str:
            try:
                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                if end_date < now - __import__('datetime').timedelta(hours=3):
                    continue
            except Exception:
                pass

        parsed = _parse_match_event(event)
        if parsed:
            matches.append(parsed)

    result = {
        "matches": sorted(matches, key=lambda m: m.get("date", "")),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }

    with open(DATA_FILE, "w") as f:
        json.dump(result, f, indent=2)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Polymarket: {len(matches)} matches saved.", file=_sys.stderr)
    return result


def load_cached_polymarket():
    """Load the most recently scraped Polymarket odds from disk."""
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return None


def _parse_match_event(event):
    """Parse a Polymarket match event into standardized odds format."""
    title = event.get("title", "")
    end_date = event.get("endDate", "")
    markets = event.get("markets", [])

    # Split title into home/away teams (handle "vs.", "vs", and en-dash "–")
    parts = title.split(" vs. ")
    if len(parts) != 2:
        parts = title.split(" vs ")
    if len(parts) != 2:
        parts = title.split(" – ")
    if len(parts) != 2:
        return None

    home_team = parts[0].strip()
    away_team = parts[1].strip()

    home_prob = draw_prob = away_prob = 0.0

    for market in markets:
        question = market.get("question", "").lower()
        prices_str = market.get("outcomePrices", "[]")
        try:
            prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
        except (json.JSONDecodeError, TypeError):
            continue

        yes_price = float(prices[0]) if prices else 0.0

        if "draw" in question:
            draw_prob = yes_price
        elif "win" in question:
            # Match team name to determine home vs away
            if _team_matches(home_team, question):
                home_prob = yes_price
            elif _team_matches(away_team, question):
                away_prob = yes_price

    return {
        "event": title,
        "date": end_date,
        "home_team": home_team,
        "away_team": away_team,
        "home_prob": home_prob,
        "draw_prob": draw_prob,
        "away_prob": away_prob,
        "home_odds": _prob_to_decimal(home_prob),
        "draw_odds": _prob_to_decimal(draw_prob),
        "away_odds": _prob_to_decimal(away_prob),
    }


def _team_matches(team_name, question):
    """Check if a team name appears in a question string."""
    # Normalize for comparison
    team_lower = team_name.lower()
    # Handle common variations
    variations = [team_lower]
    # Add specific mappings for name mismatches
    name_map = {
        "côte d'ivoire": ["cote d'ivoire", "côte d'ivoire", "ivory coast"],
        "ir iran": ["iran"],
        "korea republic": ["korea", "south korea"],
        "türkiye": ["turkiye", "turkey", "türkiye"],
        "dr congo": ["congo", "dr congo", "democratic republic of congo"],
        "cabo verde": ["cape verde", "cabo verde"],
        "bosnia-herzegovina": ["bosnia", "bosnia-herzegovina", "bosnia and herzegovina"],
        "curaçao": ["curacao", "curaçao"],
    }
    for canonical, alts in name_map.items():
        if team_lower == canonical or team_lower in alts:
            variations.extend(alts)
            variations.append(canonical)

    return any(v in question for v in variations)


def _prob_to_decimal(prob):
    """Convert a probability (0-1) to decimal odds string."""
    if prob > 0:
        return f"{1 / prob:.2f}"
    return ""


if __name__ == "__main__":
    result = scrape_polymarket_odds()
    print(json.dumps(result, indent=2))
