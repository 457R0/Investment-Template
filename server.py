from flask import Flask, jsonify, request
from flask_socketio import SocketIO
from flask_cors import CORS
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional
import sqlite3
import yfinance as yf
import uuid
import threading
import time


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


# ============================================================================
# Database
# ============================================================================

class DB:
    """SQLite connection singleton."""
    _conn = None
    
    @property
    def conn(self) -> sqlite3.Connection:
        if not self._conn:
            self._conn = sqlite3.connect(config.DATABASE, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn
    
    def close(self):
        if self._conn:
            self._conn.close()


def init_db():
    db = DB()
    c = db.conn.cursor()
    
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
        c.execute("INSERT INTO watchlists (user_id, symbols) VALUES (?, ?)",
                  (config.DEFAULT_USER, '["AAPL","GOOGL","MSFT","AMZN","TSLA","NVDA","META","NFLX"]'))
    
    db.conn.commit()
    db.close()


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
        except:
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
        except:
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
        db = DB()
        c = db.conn.cursor()
        c.execute("SELECT * FROM positions WHERE portfolio_id = ?", (portfolio_id,))
        return [dict(r) for r in c.fetchall()]
    
    @staticmethod
    def add_position(portfolio_id: str, symbol: str, shares: float, avg_cost: float):
        db = DB()
        c = db.conn.cursor()
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
        db.conn.commit()
        db.close()


# ============================================================================
# API Routes
# ============================================================================

@app.route("/api/health")
def health(): return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})

@app.route("/api/portfolio")
def get_portfolios():
    db = DB()
    c = db.conn.cursor()
    c.execute("SELECT * FROM portfolios WHERE user_id = ?", (config.DEFAULT_USER,))
    return jsonify([dict(r) for r in c.fetchall()])

@app.route("/api/portfolio/<pid>/holdings")
def get_holdings(pid): return jsonify(Portfolio.get_holdings(pid))

@app.route("/api/portfolio/<pid>/holdings", methods=["POST"])
def add_holding(pid):
    d = request.json
    Portfolio.add_position(pid, d["symbol"].upper(), d["shares"], d["avg_cost"])
    return jsonify({"success": True})

@app.route("/api/market/quote/<symbol>")
def get_quote(symbol):
    q = Market.get_quote(symbol.upper())
    return jsonify(asdict(q)) if q else jsonify({"error": "Not found"}), 404

@app.route("/api/market/quotes")
def get_quotes():
    symbols = [s.strip().upper() for s in request.args.get("symbols", "").split(",") if s.strip()]
    qs = Market.get_quotes(symbols)
    return jsonify({k: asdict(v) for k, v in qs.items()})

@app.route("/api/market/search")
def search():
    return jsonify(Market.search(request.args.get("q", "")))

@app.route("/api/watchlist")
def get_watchlist():
    db = DB()
    c = db.conn.cursor()
    c.execute("SELECT symbols FROM watchlists WHERE user_id = ?", (config.DEFAULT_USER,))
    r = c.fetchone()
    return jsonify({"symbols": eval(r[0]) if r else []})

@app.route("/api/watchlist", methods=["POST"])
def update_watchlist():
    symbols = request.json.get("symbols", [])
    db = DB()
    c = db.conn.cursor()
    c.execute("SELECT symbols FROM watchlists WHERE user_id = ?", (config.DEFAULT_USER,))
    r = c.fetchone()
    if r:
        new = list(set(eval(r[0]) + symbols))
        c.execute("UPDATE watchlists SET symbols = ? WHERE user_id = ?", (str(new), config.DEFAULT_USER))
    else:
        c.execute("INSERT INTO watchlists (id, user_id, symbols) VALUES (?, ?, ?)",
                 (str(uuid.uuid4()), config.DEFAULT_USER, str(symbols)))
    db.conn.commit()
    db.close()
    return jsonify({"success": True})

@app.route("/api/watchlist/<symbol>", methods=["DELETE"])
def remove_watchlist(symbol):
    db = DB()
    c = db.conn.cursor()
    c.execute("SELECT symbols FROM watchlists WHERE user_id = ?", (config.DEFAULT_USER,))
    r = c.fetchone()
    if r:
        s = eval(r[0])
        if symbol.upper() in s:
            s.remove(symbol.upper())
            c.execute("UPDATE watchlists SET symbols = ? WHERE user_id = ?", (str(s), config.DEFAULT_USER))
            db.conn.commit()
    db.close()
    return jsonify({"success": True})

@app.route("/api/alerts")
def get_alerts():
    db = DB()
    c = db.conn.cursor()
    c.execute("SELECT * FROM alerts WHERE user_id = ?", (config.DEFAULT_USER,))
    return jsonify([dict(r) for r in c.fetchall()])

@app.route("/api/alerts", methods=["POST"])
def create_alert():
    d = request.json
    db = DB()
    c = db.conn.cursor()
    c.execute("""INSERT INTO alerts (id, user_id, symbol, type, condition, threshold, notify_inapp, active)
               VALUES (?, ?, ?, ?, ?, ?, 1, 1)""",
             (str(uuid.uuid4()), config.DEFAULT_USER, d["symbol"].upper(), d["type"],
              d["condition"], d["threshold"]))
    db.conn.commit()
    db.close()
    return jsonify({"success": True})

@app.route("/api/alerts/<aid>", methods=["DELETE"])
def delete_alert(aid):
    db = DB()
    c = db.conn.cursor()
    c.execute("DELETE FROM alerts WHERE id = ? AND user_id = ?", (aid, config.DEFAULT_USER))
    db.conn.commit()
    db.close()
    return jsonify({"success": True})

@app.route("/api/ai")
def get_ai_signals():
    db = DB()
    c = db.conn.cursor()
    c.execute("SELECT symbols FROM watchlists WHERE user_id = ?", (config.DEFAULT_USER,))
    r = c.fetchone()
    if not r:
        return jsonify([])
    
    signals = []
    for sym in eval(r[0]):
        q = Market.get_quote(sym)
        if q:
            signals.append(AI.signal(q))
    return jsonify(signals)

@app.route("/api/ai/<symbol>")
def get_ai_signal(symbol):
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
            db = DB()
            c = db.conn.cursor()
            c.execute("SELECT symbols FROM watchlists WHERE user_id = ?", (config.DEFAULT_USER,))
            r = c.fetchone()
            if r:
                qs = Market.get_quotes(eval(r[0]))
                socketio.emit("quotes", {k: asdict(v) for k, v in qs.items()})
            db.close()
        except Exception as e:
            print(f"Error: {e}")
        time.sleep(15)


# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    init_db()
    threading.Thread(target=price_updater, daemon=True).start()
    socketio.run(app, host=config.HOST, port=config.PORT, debug=False)
