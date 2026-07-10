import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from functools import wraps
from typing import Optional

import yfinance as yf
from flask import Flask, jsonify, request, send_from_directory, session
from flask_cors import CORS
from flask_socketio import SocketIO, join_room
from werkzeug.security import check_password_hash, generate_password_hash


# ============================================================================
# Configuration
# ============================================================================

@dataclass
class Config:
    HOST: str = "0.0.0.0"
    PORT: int = 3001
    DATABASE: str = "/tmp/stonk_advisor.db"
    STARTING_BALANCE: float = 100_000.0


# ============================================================================
# Flask Setup
# ============================================================================

app = Flask(__name__)
config = Config()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("stonk_advisor")

app.secret_key = os.environ.get("SECRET_KEY")
if not app.secret_key:
    app.secret_key = secrets.token_hex(32)
    logger.warning(
        "SECRET_KEY not set; using a random key for this process only. "
        "Sessions will not survive a restart and won't work across multiple "
        "workers. Set the SECRET_KEY environment variable in production."
    )

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"

CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", f"http://localhost:{config.PORT}").split(",")]
CORS(app, supports_credentials=True, origins=CORS_ORIGINS)
socketio = SocketIO(app, cors_allowed_origins=CORS_ORIGINS)

SYMBOL_RE = re.compile(r"^[A-Z0-9.\-^]{1,10}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 8


def is_valid_symbol(symbol) -> bool:
    return isinstance(symbol, str) and bool(SYMBOL_RE.match(symbol.upper()))


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Authentication required"}), 401
        return f(*args, **kwargs)
    return wrapper


# ============================================================================
# Database
# ============================================================================

_local = threading.local()


def get_db() -> sqlite3.Connection:
    """Return a connection unique to the current thread.

    SQLite connections are not safe to share across threads, and this app has
    both Flask's request-handling threads and a background price-updater
    thread, so each thread gets and reuses its own connection.
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(config.DATABASE)
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn


def _ensure_column(conn, table, column, coltype):
    c = conn.cursor()
    c.execute(f"PRAGMA table_info({table})")
    existing = {row["name"] for row in c.fetchall()}
    if column not in existing:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_db():
    conn = get_db()
    c = conn.cursor()

    schemas = [
        """CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY, name TEXT DEFAULT '',
            risk_tolerance TEXT DEFAULT 'moderate',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS portfolios (
            id TEXT PRIMARY KEY, user_id TEXT, name TEXT,
            type TEXT DEFAULT 'paper', cash_balance REAL DEFAULT 100000,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS positions (
            id TEXT PRIMARY KEY, portfolio_id TEXT, symbol TEXT,
            shares REAL, avg_cost REAL, acquired_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS watchlists (
            id TEXT PRIMARY KEY, user_id TEXT UNIQUE, symbols TEXT DEFAULT '[]'
        )""",
        """CREATE TABLE IF NOT EXISTS alerts (
            id TEXT PRIMARY KEY, user_id TEXT, symbol TEXT, type TEXT,
            condition TEXT, threshold REAL, notify_inapp INTEGER DEFAULT 1,
            active INTEGER DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""",
    ]

    for s in schemas:
        c.execute(s)

    _ensure_column(conn, "users", "email", "TEXT")
    _ensure_column(conn, "users", "password_hash", "TEXT")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)")

    conn.commit()


def get_watchlist_symbols(user_id: str) -> list:
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT symbols FROM watchlists WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if not row:
        return []
    try:
        return json.loads(row["symbols"])
    except (TypeError, ValueError):
        logger.warning("Corrupt watchlist JSON for user %s", user_id)
        return []


def save_watchlist_symbols(user_id: str, symbols: list):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM watchlists WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    payload = json.dumps(symbols)
    if row:
        c.execute("UPDATE watchlists SET symbols = ? WHERE user_id = ?", (payload, user_id))
    else:
        c.execute("INSERT INTO watchlists (id, user_id, symbols) VALUES (?, ?, ?)",
                  (str(uuid.uuid4()), user_id, payload))
    conn.commit()


def get_all_user_ids_with_watchlists() -> list:
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT user_id FROM watchlists")
    return [row["user_id"] for row in c.fetchall()]


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class Quote:
    symbol: str; name: str; price: float; change: float
    change_percent: float; volume: int; high: float; low: float
    open: float; previous_close: float


# ============================================================================
# Services
# ============================================================================

class Market:
    @staticmethod
    def get_quote(symbol: str) -> Optional[Quote]:
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="1d")
            if hist.empty:
                return None
            latest = hist.iloc[-1]
            info = ticker.info or {}
            return Quote(
                symbol=symbol.upper(),
                name=info.get("shortName", info.get("longName", symbol)),
                price=float(latest["Close"]),
                change=float(latest["Close"] - latest["Open"]),
                change_percent=float(((latest["Close"] - latest["Open"]) / latest["Open"]) * 100),
                volume=int(latest["Volume"]),
                high=float(latest["High"]),
                low=float(latest["Low"]),
                open=float(latest["Open"]),
                previous_close=float(latest["Open"])
            )
        except Exception:
            logger.exception("Failed to fetch quote for %s", symbol)
            return None

    @staticmethod
    def get_quotes(symbols: list) -> dict:
        return {s: q for s in symbols if (q := Market.get_quote(s))}

    @staticmethod
    def search(query: str) -> list:
        try:
            tickers = yf.Tickers(query)
            return [{"symbol": s, "name": t.info.get("shortName", s)}
                    for s, t in tickers.tickers.items()
                    if t.info and t.info.get("shortName")][:10]
        except Exception:
            logger.exception("Symbol search failed for query %r", query)
            return []


class AI:
    @staticmethod
    def signal(quote: Quote) -> dict:
        prices = [quote.previous_close, quote.open, quote.price]

        # Simple RSI
        rsi = 50.0
        if len(prices) > 1:
            gains = sum(max(0, prices[i] - prices[i-1]) for i in range(1, len(prices)))
            losses = sum(max(0, prices[i-1] - prices[i]) for i in range(1, len(prices)))
            if losses > 0:
                rsi = 100 - 100 / (1 + gains/losses)

        # Moving averages
        ma5 = sum(prices) / min(5, len(prices))
        ma20 = sum(prices) / min(20, len(prices))

        score = 0
        reasons = []

        if rsi < 30: score += 25; reasons.append("RSI oversold")
        elif rsi > 70: score -= 25; reasons.append("RSI overbought")
        if quote.price > ma5: score += 15; reasons.append("Above MA5")
        if quote.price > ma20: score += 15; reasons.append("Above MA20")
        if quote.change_percent > 3: score += 10; reasons.append("Strong rally")
        elif quote.change_percent < -3: score -= 10; reasons.append("Weak decline")

        if score >= 30: sig = "strong_buy"
        elif score >= 15: sig = "buy"
        elif score <= -30: sig = "strong_sell"
        elif score <= -15: sig = "sell"
        else: sig = "hold"

        return {
            "id": str(uuid.uuid4()),
            "symbol": quote.symbol,
            "signal": sig,
            "confidence": min(95, 50 + abs(score)),
            "reasoning": f"{sig.replace('_', ' ').title()}: {', '.join(reasons[:2])}",
            "quote": asdict(quote)
        }


class Portfolio:
    @staticmethod
    def get_owned(portfolio_id: str, user_id: str) -> Optional[sqlite3.Row]:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM portfolios WHERE id = ? AND user_id = ?", (portfolio_id, user_id))
        return c.fetchone()

    @staticmethod
    def get_holdings(portfolio_id: str) -> list:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM positions WHERE portfolio_id = ?", (portfolio_id,))
        return [dict(r) for r in c.fetchall()]

    @staticmethod
    def add_position(portfolio_id: str, symbol: str, shares: float, avg_cost: float):
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM positions WHERE portfolio_id = ? AND symbol = ?", (portfolio_id, symbol))
        existing = c.fetchone()
        if existing:
            new_shares = existing["shares"] + shares
            new_cost = (existing["shares"] * existing["avg_cost"] + shares * avg_cost) / new_shares
            c.execute("UPDATE positions SET shares = ?, avg_cost = ? WHERE id = ?",
                     (new_shares, new_cost, existing["id"]))
        else:
            c.execute("INSERT INTO positions (id, portfolio_id, symbol, shares, avg_cost) VALUES (?, ?, ?, ?, ?)",
                     (str(uuid.uuid4()), portfolio_id, symbol, shares, avg_cost))
        conn.commit()


# ============================================================================
# Static Frontend
# ============================================================================

STATIC_FILES = {"app.js", "styles.css"}


@app.route("/")
def index():
    return send_from_directory(app.root_path, "index.html")


@app.route("/<path:filename>")
def static_files(filename):
    if filename not in STATIC_FILES:
        return jsonify({"error": "Not found"}), 404
    return send_from_directory(app.root_path, filename)


# ============================================================================
# Auth Routes
# ============================================================================

@app.route("/api/auth/register", methods=["POST"])
def register():
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        return jsonify({"error": "Request body must be JSON"}), 400

    email = (d.get("email") or "").strip().lower()
    password = d.get("password") or ""

    if not EMAIL_RE.match(email):
        return jsonify({"error": "Invalid email address"}), 400
    if len(password) < MIN_PASSWORD_LENGTH:
        return jsonify({"error": f"Password must be at least {MIN_PASSWORD_LENGTH} characters"}), 400

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE email = ?", (email,))
    if c.fetchone():
        return jsonify({"error": "An account with that email already exists"}), 409

    user_id = str(uuid.uuid4())
    c.execute("INSERT INTO users (id, name, email, password_hash) VALUES (?, ?, ?, ?)",
              (user_id, email.split("@")[0], email, generate_password_hash(password)))
    c.execute("INSERT INTO portfolios (id, user_id, name, cash_balance) VALUES (?, ?, ?, ?)",
              (str(uuid.uuid4()), user_id, "My Portfolio", config.STARTING_BALANCE))
    c.execute("INSERT INTO watchlists (id, user_id, symbols) VALUES (?, ?, ?)",
              (str(uuid.uuid4()), user_id, json.dumps(["AAPL", "GOOGL", "MSFT"])))
    conn.commit()

    session["user_id"] = user_id
    return jsonify({"user": {"id": user_id, "email": email}}), 201


@app.route("/api/auth/login", methods=["POST"])
def login():
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        return jsonify({"error": "Request body must be JSON"}), 400

    email = (d.get("email") or "").strip().lower()
    password = d.get("password") or ""

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, email, password_hash FROM users WHERE email = ?", (email,))
    user = c.fetchone()

    if not user or not user["password_hash"] or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Invalid email or password"}), 401

    session["user_id"] = user["id"]
    return jsonify({"user": {"id": user["id"], "email": user["email"]}})


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"success": True})


@app.route("/api/auth/me")
def me():
    if "user_id" not in session:
        return jsonify({"error": "Authentication required"}), 401
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, email FROM users WHERE id = ?", (session["user_id"],))
    user = c.fetchone()
    if not user:
        session.clear()
        return jsonify({"error": "Authentication required"}), 401
    return jsonify({"user": dict(user)})


# ============================================================================
# API Routes
# ============================================================================

@app.route("/api/health")
def health(): return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})

@app.route("/api/portfolio")
@login_required
def get_portfolios():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM portfolios WHERE user_id = ?", (session["user_id"],))
    return jsonify([dict(r) for r in c.fetchall()])

@app.route("/api/portfolio/<pid>/holdings")
@login_required
def get_holdings(pid):
    if not Portfolio.get_owned(pid, session["user_id"]):
        return jsonify({"error": "Not found"}), 404
    return jsonify(Portfolio.get_holdings(pid))

@app.route("/api/portfolio/<pid>/holdings", methods=["POST"])
@login_required
def add_holding(pid):
    if not Portfolio.get_owned(pid, session["user_id"]):
        return jsonify({"error": "Not found"}), 404

    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        return jsonify({"error": "Request body must be JSON"}), 400

    symbol = d.get("symbol")
    shares = d.get("shares")
    avg_cost = d.get("avg_cost")

    if not is_valid_symbol(symbol):
        return jsonify({"error": "Invalid or missing symbol"}), 400
    if not isinstance(shares, (int, float)) or shares == 0:
        return jsonify({"error": "shares must be a non-zero number"}), 400
    if not isinstance(avg_cost, (int, float)) or avg_cost < 0:
        return jsonify({"error": "avg_cost must be a non-negative number"}), 400

    Portfolio.add_position(pid, symbol.upper(), shares, avg_cost)
    return jsonify({"success": True})

@app.route("/api/market/quote/<symbol>")
def get_quote(symbol):
    if not is_valid_symbol(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    q = Market.get_quote(symbol.upper())
    if not q:
        return jsonify({"error": "Not found"}), 404
    return jsonify(asdict(q))

@app.route("/api/market/quotes")
def get_quotes():
    symbols = [s.strip().upper() for s in request.args.get("symbols", "").split(",") if s.strip()]
    symbols = [s for s in symbols if is_valid_symbol(s)]
    qs = Market.get_quotes(symbols)
    return jsonify({k: asdict(v) for k, v in qs.items()})

@app.route("/api/market/search")
def search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify([])
    return jsonify(Market.search(query))

@app.route("/api/watchlist")
@login_required
def get_watchlist():
    return jsonify({"symbols": get_watchlist_symbols(session["user_id"])})

@app.route("/api/watchlist", methods=["POST"])
@login_required
def update_watchlist():
    d = request.get_json(silent=True)
    if not isinstance(d, dict) or not isinstance(d.get("symbols"), list):
        return jsonify({"error": "symbols must be a list"}), 400

    incoming = [s.upper() for s in d["symbols"] if is_valid_symbol(s)]
    if not incoming:
        return jsonify({"error": "No valid symbols provided"}), 400

    existing = get_watchlist_symbols(session["user_id"])
    merged = sorted(set(existing) | set(incoming))
    save_watchlist_symbols(session["user_id"], merged)
    return jsonify({"success": True})

@app.route("/api/watchlist/<symbol>", methods=["DELETE"])
@login_required
def remove_watchlist(symbol):
    symbols = get_watchlist_symbols(session["user_id"])
    symbol = symbol.upper()
    if symbol in symbols:
        symbols.remove(symbol)
        save_watchlist_symbols(session["user_id"], symbols)
    return jsonify({"success": True})

@app.route("/api/alerts")
@login_required
def get_alerts():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM alerts WHERE user_id = ?", (session["user_id"],))
    return jsonify([dict(r) for r in c.fetchall()])

@app.route("/api/alerts", methods=["POST"])
@login_required
def create_alert():
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        return jsonify({"error": "Request body must be JSON"}), 400

    symbol = d.get("symbol")
    alert_type = d.get("type")
    condition = d.get("condition")
    threshold = d.get("threshold")

    if not is_valid_symbol(symbol):
        return jsonify({"error": "Invalid or missing symbol"}), 400
    if alert_type not in ("price", "change"):
        return jsonify({"error": "type must be 'price' or 'change'"}), 400
    if condition not in ("above", "below"):
        return jsonify({"error": "condition must be 'above' or 'below'"}), 400
    if not isinstance(threshold, (int, float)):
        return jsonify({"error": "threshold must be a number"}), 400

    conn = get_db()
    c = conn.cursor()
    c.execute("""INSERT INTO alerts (id, user_id, symbol, type, condition, threshold, notify_inapp, active)
               VALUES (?, ?, ?, ?, ?, ?, 1, 1)""",
             (str(uuid.uuid4()), session["user_id"], symbol.upper(), alert_type,
              condition, threshold))
    conn.commit()
    return jsonify({"success": True})

@app.route("/api/alerts/<aid>", methods=["DELETE"])
@login_required
def delete_alert(aid):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM alerts WHERE id = ? AND user_id = ?", (aid, session["user_id"]))
    conn.commit()
    return jsonify({"success": True})

@app.route("/api/ai")
@login_required
def get_ai_signals():
    signals = []
    for sym in get_watchlist_symbols(session["user_id"]):
        q = Market.get_quote(sym)
        if q:
            signals.append(AI.signal(q))
    return jsonify(signals)

@app.route("/api/ai/<symbol>")
def get_ai_signal(symbol):
    if not is_valid_symbol(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    q = Market.get_quote(symbol.upper())
    if not q:
        return jsonify({"error": "Not found"}), 404
    return jsonify(AI.signal(q))


# ============================================================================
# WebSocket & Scheduler
# ============================================================================

@socketio.on("connect")
def handle_connect():
    if "user_id" not in session:
        return False  # reject the connection
    join_room(session["user_id"])


def price_updater():
    while True:
        try:
            for user_id in get_all_user_ids_with_watchlists():
                symbols = get_watchlist_symbols(user_id)
                if symbols:
                    qs = Market.get_quotes(symbols)
                    socketio.emit("quotes", {k: asdict(v) for k, v in qs.items()}, room=user_id)
        except Exception:
            logger.exception("price_updater loop failed")
        time.sleep(15)


# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    init_db()
    threading.Thread(target=price_updater, daemon=True).start()
    socketio.run(app, host=config.HOST, port=config.PORT, debug=False)
