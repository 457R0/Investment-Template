import logging
import os
import re
import secrets
import threading
import time
from dataclasses import asdict
from datetime import datetime
from functools import wraps

from flask import Flask, jsonify, request, send_from_directory, session
from flask_cors import CORS
from flask_migrate import Migrate
from flask_socketio import SocketIO, join_room
from werkzeug.security import check_password_hash, generate_password_hash

from market import Market
from models import Alert, Portfolio, Position, User, Watchlist, db


# ============================================================================
# Configuration
# ============================================================================

HOST = "0.0.0.0"
PORT = 3001
STARTING_BALANCE = 100_000.0


# ============================================================================
# Flask Setup
# ============================================================================

app = Flask(__name__)

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

_default_sqlite_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), "stonk_advisor.db")
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{_default_sqlite_path}")
if DATABASE_URL.startswith("postgres://"):
    # Some hosts (e.g. Heroku-style providers) hand out the old "postgres://"
    # scheme, which SQLAlchemy 1.4+ no longer accepts.
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
migrate = Migrate(app, db)

CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", f"http://localhost:{PORT}").split(",")]
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
# AI Signal Service
# ============================================================================

class AI:
    @staticmethod
    def signal(quote) -> dict:
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
            "symbol": quote.symbol,
            "signal": sig,
            "confidence": min(95, 50 + abs(score)),
            "reasoning": f"{sig.replace('_', ' ').title()}: {', '.join(reasons[:2])}",
            "quote": asdict(quote)
        }


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

    if User.query.filter_by(email=email).first():
        return jsonify({"error": "An account with that email already exists"}), 409

    user = User(email=email, name=email.split("@")[0], password_hash=generate_password_hash(password))
    db.session.add(user)
    db.session.flush()  # populate user.id for the rows below

    db.session.add(Portfolio(user_id=user.id, name="My Portfolio", cash_balance=STARTING_BALANCE))
    db.session.add(Watchlist(user_id=user.id, symbols=["AAPL", "GOOGL", "MSFT"]))
    db.session.commit()

    session["user_id"] = user.id
    return jsonify({"user": {"id": user.id, "email": user.email}}), 201


@app.route("/api/auth/login", methods=["POST"])
def login():
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        return jsonify({"error": "Request body must be JSON"}), 400

    email = (d.get("email") or "").strip().lower()
    password = d.get("password") or ""

    user = User.query.filter_by(email=email).first()
    if not user or not check_password_hash(user.password_hash, password):
        return jsonify({"error": "Invalid email or password"}), 401

    session["user_id"] = user.id
    return jsonify({"user": {"id": user.id, "email": user.email}})


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"success": True})


@app.route("/api/auth/me")
def me():
    if "user_id" not in session:
        return jsonify({"error": "Authentication required"}), 401
    user = db.session.get(User, session["user_id"])
    if not user:
        session.clear()
        return jsonify({"error": "Authentication required"}), 401
    return jsonify({"user": {"id": user.id, "email": user.email}})


# ============================================================================
# API Routes
# ============================================================================

@app.route("/api/health")
def health(): return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})

@app.route("/api/portfolio")
@login_required
def get_portfolios():
    portfolios = Portfolio.query.filter_by(user_id=session["user_id"]).all()
    return jsonify([p.to_dict() for p in portfolios])

@app.route("/api/portfolio/<pid>/holdings")
@login_required
def get_holdings(pid):
    portfolio = Portfolio.query.filter_by(id=pid, user_id=session["user_id"]).first()
    if not portfolio:
        return jsonify({"error": "Not found"}), 404
    positions = Position.query.filter_by(portfolio_id=pid).all()
    return jsonify([p.to_dict() for p in positions])

@app.route("/api/portfolio/<pid>/holdings", methods=["POST"])
@login_required
def add_holding(pid):
    portfolio = Portfolio.query.filter_by(id=pid, user_id=session["user_id"]).first()
    if not portfolio:
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

    symbol = symbol.upper()
    existing = Position.query.filter_by(portfolio_id=pid, symbol=symbol).first()
    if existing:
        new_shares = existing.shares + shares
        existing.avg_cost = (existing.shares * existing.avg_cost + shares * avg_cost) / new_shares
        existing.shares = new_shares
    else:
        db.session.add(Position(portfolio_id=pid, symbol=symbol, shares=shares, avg_cost=avg_cost))
    db.session.commit()
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
    wl = Watchlist.query.filter_by(user_id=session["user_id"]).first()
    return jsonify({"symbols": wl.symbols if wl else []})

@app.route("/api/watchlist", methods=["POST"])
@login_required
def update_watchlist():
    d = request.get_json(silent=True)
    if not isinstance(d, dict) or not isinstance(d.get("symbols"), list):
        return jsonify({"error": "symbols must be a list"}), 400

    incoming = [s.upper() for s in d["symbols"] if is_valid_symbol(s)]
    if not incoming:
        return jsonify({"error": "No valid symbols provided"}), 400

    wl = Watchlist.query.filter_by(user_id=session["user_id"]).first()
    if wl:
        wl.symbols = sorted(set(wl.symbols) | set(incoming))
    else:
        wl = Watchlist(user_id=session["user_id"], symbols=sorted(set(incoming)))
        db.session.add(wl)
    db.session.commit()
    return jsonify({"success": True})

@app.route("/api/watchlist/<symbol>", methods=["DELETE"])
@login_required
def remove_watchlist(symbol):
    wl = Watchlist.query.filter_by(user_id=session["user_id"]).first()
    symbol = symbol.upper()
    if wl and symbol in wl.symbols:
        wl.symbols = [s for s in wl.symbols if s != symbol]
        db.session.commit()
    return jsonify({"success": True})

@app.route("/api/alerts")
@login_required
def get_alerts():
    alerts = Alert.query.filter_by(user_id=session["user_id"]).all()
    return jsonify([a.to_dict() for a in alerts])

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

    db.session.add(Alert(
        user_id=session["user_id"], symbol=symbol.upper(), type=alert_type,
        condition=condition, threshold=threshold,
    ))
    db.session.commit()
    return jsonify({"success": True})

@app.route("/api/alerts/<aid>", methods=["DELETE"])
@login_required
def delete_alert(aid):
    Alert.query.filter_by(id=aid, user_id=session["user_id"]).delete()
    db.session.commit()
    return jsonify({"success": True})

@app.route("/api/ai")
@login_required
def get_ai_signals():
    wl = Watchlist.query.filter_by(user_id=session["user_id"]).first()
    signals = []
    for sym in (wl.symbols if wl else []):
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
# TODO: app.js never loads socket.io-client or calls io(), so this per-user
# room broadcast has no consumer yet. Wire up a real-time feed in the
# frontend, or remove this machinery if it's not going to be used.

@socketio.on("connect")
def handle_connect():
    if "user_id" not in session:
        return False  # reject the connection
    join_room(session["user_id"])


def price_updater():
    while True:
        try:
            with app.app_context():
                for wl in Watchlist.query.all():
                    if wl.symbols:
                        qs = Market.get_quotes(wl.symbols)
                        socketio.emit("quotes", {k: asdict(v) for k, v in qs.items()}, room=wl.user_id)
        except Exception:
            logger.exception("price_updater loop failed")
        time.sleep(15)


# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    with app.app_context():
        if DATABASE_URL.startswith("sqlite:"):
            # Zero-friction local dev: create tables directly instead of
            # requiring `flask db upgrade` first.
            db.create_all()
        # Real deployments (Postgres, etc.) are expected to run
        # `flask db upgrade` as part of their deploy step.

    threading.Thread(target=price_updater, daemon=True).start()
    socketio.run(app, host=HOST, port=PORT, debug=False)
