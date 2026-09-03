"""
Central configuration for Urban Spork.

Everything configurable lives here so no module reads os.getenv() at random,
and so nothing sensitive is ever hard-coded: broker credentials, database
URLs and API keys are read from environment variables (or a .env file) only.
See .env.example for the full list.
"""
import os

try:  # optional - the app runs fine without python-dotenv installed
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # pragma: no cover - trivial optional import
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# --- Persistent stores -----------------------------------------------------
# Market data, predictions and paper trades live in SQLite by default. This
# file is deliberately NOT cleared at market close or on restart - a growing
# local history is what makes later ML training possible at all.
MARKET_DB_PATH = os.getenv("MARKET_DB_PATH", os.path.join(DATA_DIR, "urban_spork.db"))
MODEL_DIR = os.getenv("MODEL_DIR", os.path.join(DATA_DIR, "models"))
os.makedirs(MODEL_DIR, exist_ok=True)

# --- Data providers --------------------------------------------------------
# Which provider serves quotes/history. "yahoo" is the keyless default.
MARKET_DATA_PROVIDER = os.getenv("MARKET_DATA_PROVIDER", "yahoo").strip().lower()

# Kotak Neo (optional, not enabled unless every value below is present).
# NEVER hard-code these - they belong in the environment / a .env file that
# is git-ignored.
KOTAK_NEO_CONSUMER_KEY = os.getenv("KOTAK_NEO_CONSUMER_KEY", "")
KOTAK_NEO_CONSUMER_SECRET = os.getenv("KOTAK_NEO_CONSUMER_SECRET", "")
KOTAK_NEO_ACCESS_TOKEN = os.getenv("KOTAK_NEO_ACCESS_TOKEN", "")
KOTAK_NEO_MOBILE = os.getenv("KOTAK_NEO_MOBILE", "")
KOTAK_NEO_PASSWORD = os.getenv("KOTAK_NEO_PASSWORD", "")
KOTAK_NEO_MPIN = os.getenv("KOTAK_NEO_MPIN", "")
KOTAK_NEO_ENVIRONMENT = os.getenv("KOTAK_NEO_ENVIRONMENT", "prod")

NEWS_API_KEY = os.getenv("NEWS_API_KEY", "")

# --- Universe refresh ------------------------------------------------------
UNIVERSE_REFRESH_HOURS = float(os.getenv("UNIVERSE_REFRESH_HOURS", "24"))

# --- Risk defaults (per-user overridable at runtime, see risk.py) ----------
DEFAULT_CAPITAL = float(os.getenv("DEFAULT_CAPITAL", "100000"))
DEFAULT_RISK_PER_TRADE_PCT = float(os.getenv("DEFAULT_RISK_PER_TRADE_PCT", "1.0"))
DEFAULT_MAX_DAILY_LOSS_PCT = float(os.getenv("DEFAULT_MAX_DAILY_LOSS_PCT", "3.0"))
DEFAULT_MIN_RISK_REWARD = float(os.getenv("DEFAULT_MIN_RISK_REWARD", "1.5"))

# Below this confidence the engine refuses to emit a trade at all. Saying
# "NO TRADE" is a feature, not a failure - see README.
MIN_TRADE_CONFIDENCE = float(os.getenv("MIN_TRADE_CONFIDENCE", "0.55"))

# --- Scheduler -------------------------------------------------------------
TICK_MINUTES = int(os.getenv("TICK_MINUTES", "5"))
COLLECTOR_MINUTES = int(os.getenv("COLLECTOR_MINUTES", "5"))
