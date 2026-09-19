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

# --- Live trading (REAL MONEY) ---------------------------------------------
# Nothing below can send an order unless LIVE_TRADING_ENABLED is true AND the
# session has been explicitly armed at runtime. Two separate switches on
# purpose: the env var is a deployment-level decision, arming is a
# session-level one that expires on its own.
LIVE_TRADING_ENABLED = os.getenv("LIVE_TRADING_ENABLED", "false").strip().lower() in ("1", "true", "yes")

# Which broker executes orders. "paper" routes to the simulator and is the
# default precisely so a misconfiguration cannot spend money.
BROKER = os.getenv("BROKER", "paper").strip().lower()

# Groww credentials. Tokens are short-lived; either paste a daily access
# token, or supply an API key plus TOTP secret and let the app mint one.
GROWW_ACCESS_TOKEN = os.getenv("GROWW_ACCESS_TOKEN", "")
GROWW_API_KEY = os.getenv("GROWW_API_KEY", "")
GROWW_API_SECRET = os.getenv("GROWW_API_SECRET", "")
GROWW_TOTP_SECRET = os.getenv("GROWW_TOTP_SECRET", "")

# Dry run builds and logs the exact broker payload but never transmits it.
# On by default: arming a live session should be a deliberate second step.
LIVE_DRY_RUN = os.getenv("LIVE_DRY_RUN", "true").strip().lower() in ("1", "true", "yes")

# Hard ceilings. These are enforced in code before every order and are not
# editable from the UI - the UI settings can only ever be more conservative.
LIVE_MAX_ORDER_VALUE = float(os.getenv("LIVE_MAX_ORDER_VALUE", "25000"))
LIVE_MAX_ORDERS_PER_DAY = int(os.getenv("LIVE_MAX_ORDERS_PER_DAY", "10"))
LIVE_MAX_DEPLOYED_CAPITAL = float(os.getenv("LIVE_MAX_DEPLOYED_CAPITAL", "100000"))
LIVE_MAX_OPEN_POSITIONS = int(os.getenv("LIVE_MAX_OPEN_POSITIONS", "3"))

# Auto-execution needs a higher bar than a trade you approve by hand.
LIVE_AUTO_MIN_CONFIDENCE = float(os.getenv("LIVE_AUTO_MIN_CONFIDENCE", "0.75"))

# An armed session expires by itself, so a forgotten switch cannot trade for
# days. Re-arming is one tap.
LIVE_ARM_MINUTES = int(os.getenv("LIVE_ARM_MINUTES", "360"))

# Stop opening new positions this many minutes before the close, and square
# off whatever is still open this many minutes before it.
LIVE_NO_NEW_TRADES_BEFORE_CLOSE_MIN = int(os.getenv("LIVE_NO_NEW_TRADES_BEFORE_CLOSE_MIN", "30"))
LIVE_SQUARE_OFF_BEFORE_CLOSE_MIN = int(os.getenv("LIVE_SQUARE_OFF_BEFORE_CLOSE_MIN", "15"))

# Reject an order if the quote moved more than this since the analysis, or
# if the quote itself is older than this.
LIVE_MAX_PRICE_DRIFT_PCT = float(os.getenv("LIVE_MAX_PRICE_DRIFT_PCT", "0.5"))
LIVE_MAX_QUOTE_AGE_SECONDS = int(os.getenv("LIVE_MAX_QUOTE_AGE_SECONDS", "120"))

# Minutes before the same symbol may be traded again.
LIVE_SYMBOL_COOLDOWN_MINUTES = int(os.getenv("LIVE_SYMBOL_COOLDOWN_MINUTES", "30"))

# Only these symbols may ever be traded with real money. Empty means none -
# an explicit allow-list, never an implicit "everything".
LIVE_SYMBOL_WHITELIST = [
    s.strip().upper() for s in os.getenv("LIVE_SYMBOL_WHITELIST", "").split(",") if s.strip()
]

# LIMIT orders by default. A market order in a thin stock is how you discover
# what a bad fill feels like.
LIVE_ORDER_TYPE = os.getenv("LIVE_ORDER_TYPE", "LIMIT").strip().upper()
# How far through the spread a LIMIT entry is placed, as a % of price.
LIVE_LIMIT_SLIPPAGE_PCT = float(os.getenv("LIVE_LIMIT_SLIPPAGE_PCT", "0.15"))

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
