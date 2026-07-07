"""
Singapore Pools World Cup odds scraper module.

Handles authentication with the IBM MobileFirst flow and fetches
structured odds data from the internal API.
"""

import json
import os
import time
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright

BASE_URL = "https://online.singaporepools.com"
HIERARCHY_URL = f"{BASE_URL}/mfp/api/adapters/spplMfpApi/event/hierarchy?lang=en"
ODDS_URL_TEMPLATE = f"{BASE_URL}/mfp/api/adapters/spplMfpApi/event/opening-odds/football/{{event_id}}?lang=en"
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "odds_data.json")


def _launch_browser(p, attempts=3):
    """
    Launch headless Chromium, retrying on failure.

    In the Railway container Chromium intermittently dies at launch (SIGTRAP,
    typically transient memory pressure), which surfaces as a TargetClosedError.
    Because last_updated is only written on full success, a single unretried
    launch crash freezes the SG feed until the next redeploy. Retrying the launch
    a few times with backoff turns that multi-day outage into a blip.

    Memory-lean flags cut shared-memory and GPU overhead without destabilising
    navigation. (--single-process saves the most RAM but hangs Page.goto in this
    containerised headless Chromium, so it's out.)
    """
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            return p.chromium.launch(headless=True, args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ])
        except Exception as e:
            last_err = e
            print(f"  Chromium launch failed (attempt {attempt}/{attempts}): {e}")
            if attempt < attempts:
                time.sleep(2 * attempt)
    raise RuntimeError(
        f"Chromium failed to launch after {attempts} attempts: {last_err}"
    )


def scrape_odds():
    """
    Scrape all World Cup 1X2 odds from Singapore Pools.
    Returns a dict with 'matches' list and 'last_updated' timestamp.
    """
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Starting odds scrape...")

    with sync_playwright() as p:
        browser = _launch_browser(p)
        try:
            page = browser.new_page()
            # Cap every Playwright action (goto, evaluate, etc.) so a single
            # unresponsive step can't stall the scrape forever.
            page.set_default_timeout(30000)

            # We only need the auth token (minted by the page's JS) and JSON from
            # the API, never rendered assets. Aborting images/fonts/media/CSS cuts
            # memory and bandwidth on load. Scripts/documents are left alone so the
            # MobileFirst SDK can still run and produce the token.
            _BLOCKED_RESOURCES = {"image", "font", "media", "stylesheet"}
            page.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in _BLOCKED_RESOURCES
                else route.continue_(),
            )

            # Authenticate
            token = _get_auth_token(page)

            # Get event hierarchy
            hierarchy = _fetch_api(page, HIERARCHY_URL, token)

            # Extract World Cup match events (not outrights like "Group Winner")
            wc_matches = []
            for country in hierarchy.get("football", []):
                for league in country.get("leagues", []):
                    if "cup" in league.get("name", "").lower():
                        for event in league.get("eventIdSet", []):
                            name = event["name"]
                            # Filter to actual matches (contains "vs")
                            if " vs " in name:
                                wc_matches.append(event)

            print(f"  Found {len(wc_matches)} World Cup matches")

            # Fetch odds for each match
            matches = []
            for i, event in enumerate(wc_matches):
                try:
                    odds_data = _fetch_api(
                        page,
                        ODDS_URL_TEMPLATE.format(event_id=event["id"]),
                        token,
                    )
                    parsed = _parse_match_odds(odds_data)
                    if parsed:
                        matches.append(parsed)
                except Exception as e:
                    print(f"  Error fetching {event['name']}: {e}")
        finally:
            browser.close()

    result = {
        "matches": sorted(matches, key=lambda m: m.get("date", "")),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }

    # Write to file
    with open(DATA_FILE, "w") as f:
        json.dump(result, f, indent=2)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Scrape complete. {len(matches)} matches saved.")
    return result


def load_cached_odds():
    """Load the most recently scraped odds from disk."""
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return None


def _get_auth_token(page):
    """Navigate to the site and capture the JWT from the MobileFirst auth flow."""
    token = {"value": None}

    def on_response(response):
        if "/mfp/api/az/v1/token" in response.url and response.status == 200:
            try:
                body = response.json()
                token["value"] = body.get("access_token")
            except Exception:
                pass

    page.on("response", on_response)
    # domcontentloaded is bounded and reliable; "networkidle" can stall on
    # sites that keep long-lived connections open (never going idle).
    page.goto(f"{BASE_URL}/en/sports/opening-odds", wait_until="domcontentloaded", timeout=30000)

    # Poll for the token instead of a blind fixed wait.
    for _ in range(30):
        if token["value"]:
            break
        page.wait_for_timeout(500)

    if not token["value"]:
        raise RuntimeError("Failed to capture auth token")

    return token["value"]


def _fetch_api(page, url, token, timeout_ms=15000):
    """Call an API endpoint using the browser context with the JWT token.

    Uses an AbortController so a stalled request fails fast instead of
    hanging page.evaluate (and the whole scrape) indefinitely.
    """
    return page.evaluate("""
        async ([url, token, timeoutMs]) => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), timeoutMs);
            try {
                const resp = await fetch(url, {
                    headers: { 'Authorization': 'Bearer ' + token },
                    signal: controller.signal
                });
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                return await resp.json();
            } finally {
                clearTimeout(timer);
            }
        }
    """, [url, token, timeout_ms])


def _parse_match_odds(odds_data):
    """Extract 1X2 odds, date, and teams from a match odds response."""
    for event in odds_data.get("events", []):
        event_name = event.get("name", "")
        event_date = event.get("startTime", "")

        for market in event.get("markets", []):
            if market.get("minorCode") != "MR":
                continue

            home = {"name": "", "odds": "", "opening": ""}
            draw = {"odds": "", "opening": ""}
            away = {"name": "", "odds": "", "opening": ""}

            for outcome in market.get("outcomes", []):
                minor = outcome.get("minorCode", "")
                prices = outcome.get("prices", [])
                current = prices[0].get("decimal", "") if prices else ""

                opening = ""
                for hp in outcome.get("historicPrices", []):
                    if hp.get("isOpening") == "true":
                        opening = str(hp.get("livePriceDec", ""))

                if minor == "H":
                    home = {"name": outcome.get("name", ""), "odds": current, "opening": opening}
                elif minor == "D":
                    draw = {"odds": current, "opening": opening}
                elif minor == "A":
                    away = {"name": outcome.get("name", ""), "odds": current, "opening": opening}

            return {
                "event": event_name,
                "date": event_date,
                "home_team": home["name"],
                "away_team": away["name"],
                "home_odds": home["odds"],
                "draw_odds": draw["odds"],
                "away_odds": away["odds"],
                "home_opening": home["opening"],
                "draw_opening": draw["opening"],
                "away_opening": away["opening"],
            }

    return None


if __name__ == "__main__":
    result = scrape_odds()
    print(json.dumps(result, indent=2))
