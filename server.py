import json
import logging
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Optional
import sqlite3

import yfinance as yf
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO


# ============================================================================
# Configuration
# ============================================================================

@dataclass
class Config:
    HOST: str = "0.0.0.0"
    PORT: int = 3001
    DATABASE: str = "/tmp/stonk_advisor.db"
    DEFAULT_USER: str = "default_user"
    DEFAULT_PORTFOLIO: str = "default_portfolio"
    STARTING_BALANCE: float = 100_000.0


# ============================================================================
# Flask Setup
# ============================================================================

app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")
config = Config()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("stonk_advisor")

SYMBOL_RE = re.compile(r"^[A-Z0-9.\-^]{1,10}$")


def is_valid_symbol(symbol) -> bool:
    return isinstance(symbol, str) and bool(SYMBOL_RE.match(symbol.upper()))


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

    c.execute("SELECT id FROM users WHERE id = ?", (config.DEFAULT_USER,))
    if not c.fetchone():
        c.execute("INSERT INTO users (id, name) VALUES (?, ?)",
                  (config.DEFAULT_USER, "Trader"))
        c.execute("INSERT INTO portfolios (id, user_id, name, cash_balance) VALUES (?, ?, ?, ?)",
                  (config.DEFAULT_PORTFOLIO, config.DEFAULT_USER, "Paper Trading", config.STARTING_BALANCE))
        c.execute("INSERT INTO watchlists (id, user_id, symbols) VALUES (?, ?, ?)",
                  (str(uuid.uuid4()), config.DEFAULT_USER,
                   json.dumps(["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "NFLX"])))

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
# API Routes
# ============================================================================

@app.route("/api/health")
def health(): return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})

@app.route("/api/portfolio")
def get_portfolios():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM portfolios WHERE user_id = ?", (config.DEFAULT_USER,))
    return jsonify([dict(r) for r in c.fetchall()])

@app.route("/api/portfolio/<pid>/holdings")
def get_holdings(pid): return jsonify(Portfolio.get_holdings(pid))

@app.route("/api/portfolio/<pid>/holdings", methods=["POST"])
def add_holding(pid):
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
def get_watchlist():
    return jsonify({"symbols": get_watchlist_symbols(config.DEFAULT_USER)})

@app.route("/api/watchlist", methods=["POST"])
def update_watchlist():
    d = request.get_json(silent=True)
    if not isinstance(d, dict) or not isinstance(d.get("symbols"), list):
        return jsonify({"error": "symbols must be a list"}), 400

    incoming = [s.upper() for s in d["symbols"] if is_valid_symbol(s)]
    if not incoming:
        return jsonify({"error": "No valid symbols provided"}), 400

    existing = get_watchlist_symbols(config.DEFAULT_USER)
    merged = sorted(set(existing) | set(incoming))
    save_watchlist_symbols(config.DEFAULT_USER, merged)
    return jsonify({"success": True})

@app.route("/api/watchlist/<symbol>", methods=["DELETE"])
def remove_watchlist(symbol):
    symbols = get_watchlist_symbols(config.DEFAULT_USER)
    symbol = symbol.upper()
    if symbol in symbols:
        symbols.remove(symbol)
        save_watchlist_symbols(config.DEFAULT_USER, symbols)
    return jsonify({"success": True})

@app.route("/api/alerts")
def get_alerts():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM alerts WHERE user_id = ?", (config.DEFAULT_USER,))
    return jsonify([dict(r) for r in c.fetchall()])

@app.route("/api/alerts", methods=["POST"])
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
             (str(uuid.uuid4()), config.DEFAULT_USER, symbol.upper(), alert_type,
              condition, threshold))
    conn.commit()
    return jsonify({"success": True})

@app.route("/api/alerts/<aid>", methods=["DELETE"])
def delete_alert(aid):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM alerts WHERE id = ? AND user_id = ?", (aid, config.DEFAULT_USER))
    conn.commit()
    return jsonify({"success": True})

@app.route("/api/ai")
def get_ai_signals():
    signals = []
    for sym in get_watchlist_symbols(config.DEFAULT_USER):
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

def price_updater():
    while True:
        try:
            symbols = get_watchlist_symbols(config.DEFAULT_USER)
            if symbols:
                qs = Market.get_quotes(symbols)
                socketio.emit("quotes", {k: asdict(v) for k, v in qs.items()})
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
