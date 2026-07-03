"""
Flask web server for World Cup odds comparison.

- Serves the frontend at /
- Exposes /api/odds for combined SG Pools + Polymarket data
- Runs both scrapers every 5 minutes in the background
"""

import os
import signal
import subprocess
import sys
import threading
import time
from flask import Flask, jsonify, send_from_directory
from apscheduler.schedulers.background import BackgroundScheduler
from odds_scraper import load_cached_odds
from polymarket_scraper import scrape_polymarket_odds, load_cached_polymarket
from kalshi_scraper import scrape_kalshi_odds, load_cached_kalshi

app = Flask(__name__, static_folder="static")
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

# Guards against overlapping scrapes. Instead of a plain lock (which would stay
# held forever if a scrape ever hung), we track when the current scrape started.
# If it has been running far longer than any scrape should take, we assume that
# thread is stuck and let a fresh scrape proceed anyway — so SG Pools keeps
# updating instead of freezing permanently.
_scrape_guard = threading.Lock()
_scrape_in_progress = False
_scrape_started_at = 0.0
STALE_SCRAPE_SECONDS = 600  # 10 min; a healthy scrape finishes in well under a minute

# The SG Pools scrape drives a headless Chromium via Playwright, which can hang
# in places the page-level timeouts don't cover (browser launch, teardown). If
# that happens in-process, the scrape_all thread never returns and APScheduler's
# max_instances=1 permanently skips every future run ("maximum number of running
# instances reached"). Running it in a killable subprocess with a hard
# wall-clock cap guarantees scrape_all always returns and reaps a stuck Chromium.
SCRAPE_ODDS_TIMEOUT = 180  # seconds; a healthy SG Pools scrape finishes in ~30-60s
_APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _run_sg_scrape():
    """Run the SG Pools scrape in a subprocess bounded by a hard timeout."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "from odds_scraper import scrape_odds; scrape_odds()"],
        cwd=_APP_DIR,
        start_new_session=True,  # own process group so we can kill Chromium children
    )
    try:
        proc.wait(timeout=SCRAPE_ODDS_TIMEOUT)
    except subprocess.TimeoutExpired:
        print(f"SG Pools scrape exceeded {SCRAPE_ODDS_TIMEOUT}s; killing subprocess tree")
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()

# Team name normalization for matching between sources
TEAM_ALIASES = {
    "ivory coast": "côte d'ivoire",
    "cote d'ivoire": "côte d'ivoire",
    "côte d'ivoire": "côte d'ivoire",
    "holland": "netherlands",
    "netherlands": "netherlands",
    "congo dr": "dr congo",
    "dr congo": "dr congo",
    "democratic republic of congo": "dr congo",
    "cape verde": "cabo verde",
    "cabo verde": "cabo verde",
    "korea republic": "korea republic",
    "south korea": "korea republic",
    "turkey": "türkiye",
    "türkiye": "türkiye",
    "turkiye": "türkiye",
    "bosnia": "bosnia-herzegovina",
    "bosnia-herzegovina": "bosnia-herzegovina",
    "bosnia and herzegovina": "bosnia-herzegovina",
    "curacao": "curaçao",
    "curaçao": "curaçao",
    "czech republic": "czechia",
    "czechia": "czechia",
    "iran": "ir iran",
    "ir iran": "ir iran",
    "usa": "united states",
    "united states": "united states",
}


def normalize_team(name):
    return TEAM_ALIASES.get(name.lower(), name.lower())


def scrape_all():
    """Run all scrapers. Skips if another scrape is already running, unless the
    running one appears stuck (see STALE_SCRAPE_SECONDS)."""
    global _scrape_in_progress, _scrape_started_at
    with _scrape_guard:
        if _scrape_in_progress and (time.time() - _scrape_started_at) < STALE_SCRAPE_SECONDS:
            print("Scrape already in progress, skipping")
            return
        if _scrape_in_progress:
            print("Previous scrape appears stuck; starting a new one anyway")
        _scrape_in_progress = True
        _scrape_started_at = time.time()

    try:
        try:
            _run_sg_scrape()
        except Exception as e:
            print(f"SG Pools scrape error: {e}")
        try:
            scrape_polymarket_odds()
        except Exception as e:
            print(f"Polymarket scrape error: {e}")
        try:
            scrape_kalshi_odds()
        except Exception as e:
            print(f"Kalshi scrape error: {e}")
    finally:
        with _scrape_guard:
            _scrape_in_progress = False


def scrape_fast():
    """Run Polymarket + Kalshi scrapers only (lightweight API calls)."""
    try:
        scrape_polymarket_odds()
    except Exception as e:
        print(f"Polymarket scrape error: {e}")
    try:
        scrape_kalshi_odds()
    except Exception as e:
        print(f"Kalshi scrape error: {e}")


def combine_odds():
    """Combine SG Pools, Polymarket, and Kalshi odds into a single dataset."""
    sg_data = load_cached_odds()
    pm_data = load_cached_polymarket()
    ka_data = load_cached_kalshi()

    if not sg_data:
        return None

    # Build lookup from Polymarket data keyed by normalized team pair
    pm_lookup = {}
    if pm_data:
        for match in pm_data.get("matches", []):
            home_norm = normalize_team(match["home_team"])
            away_norm = normalize_team(match["away_team"])
            key = f"{home_norm}|{away_norm}"
            pm_lookup[key] = match

    # Build lookup from Kalshi data keyed by normalized team pair
    ka_lookup = {}
    if ka_data:
        for match in ka_data.get("matches", []):
            home_norm = normalize_team(match["home_team"])
            away_norm = normalize_team(match["away_team"])
            key = f"{home_norm}|{away_norm}"
            ka_lookup[key] = match

    combined = []
    for match in sg_data.get("matches", []):
        home_norm = normalize_team(match["home_team"])
        away_norm = normalize_team(match["away_team"])
        key = f"{home_norm}|{away_norm}"

        reversed_key = f"{away_norm}|{home_norm}"
        pm_match = pm_lookup.get(key) or pm_lookup.get(reversed_key)
        ka_match = ka_lookup.get(key) or ka_lookup.get(reversed_key)

        entry = {
            "event": match["event"],
            "date": match["date"],
            "home_team": match["home_team"],
            "away_team": match["away_team"],
            # SG Pools odds
            "sg_home": match["home_odds"],
            "sg_draw": match["draw_odds"],
            "sg_away": match["away_odds"],
            "sg_home_opening": match.get("home_opening", ""),
            "sg_draw_opening": match.get("draw_opening", ""),
            "sg_away_opening": match.get("away_opening", ""),
            # Polymarket odds
            "pm_home": pm_match["home_odds"] if pm_match else "",
            "pm_draw": pm_match["draw_odds"] if pm_match else "",
            "pm_away": pm_match["away_odds"] if pm_match else "",
            "pm_home_prob": pm_match["home_prob"] if pm_match else "",
            "pm_draw_prob": pm_match["draw_prob"] if pm_match else "",
            "pm_away_prob": pm_match["away_prob"] if pm_match else "",
            # Kalshi odds
            "ka_home": ka_match["home_odds"] if ka_match else "",
            "ka_draw": ka_match["draw_odds"] if ka_match else "",
            "ka_away": ka_match["away_odds"] if ka_match else "",
            "ka_home_prob": ka_match["home_prob"] if ka_match else "",
            "ka_draw_prob": ka_match["draw_prob"] if ka_match else "",
            "ka_away_prob": ka_match["away_prob"] if ka_match else "",
        }
        # Only include matches listed on Polymarket or Kalshi (filters out non-WC cup matches)
        if pm_match or ka_match:
            combined.append(entry)

    return {
        "matches": combined,
        "sg_updated": sg_data.get("last_updated", ""),
        "pm_updated": pm_data.get("last_updated", "") if pm_data else "",
        "ka_updated": ka_data.get("last_updated", "") if ka_data else "",
    }


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/odds")
def api_odds():
    data = combine_odds()
    if data is None:
        return jsonify({"error": "No data yet. Scraping in progress..."}), 503
    return jsonify(data)


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Trigger a manual refresh (runs in background thread)."""
    thread = threading.Thread(target=scrape_all, daemon=True)
    thread.start()
    return jsonify({"status": "Scrape started"})


def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(scrape_all, "interval", minutes=5, id="sg_scraper")
    scheduler.add_job(scrape_fast, "interval", seconds=30, id="fast_scraper")
    scheduler.start()


if __name__ == "__main__":
    print("Running initial scrape...")
    scrape_all()

    start_scheduler()

    port = int(os.environ.get("PORT", 5050))
    print(f"\nStarting web server on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
