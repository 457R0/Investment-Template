import uuid
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(255), default="")
    created_at = db.Column(db.DateTime(timezone=True), default=_now)

    portfolios = db.relationship("Portfolio", backref="user", cascade="all, delete-orphan")
    watchlist = db.relationship("Watchlist", backref="user", uselist=False, cascade="all, delete-orphan")
    alerts = db.relationship("Alert", backref="user", cascade="all, delete-orphan")


class Portfolio(db.Model):
    __tablename__ = "portfolios"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    name = db.Column(db.String(255))
    type = db.Column(db.String(50), default="paper")
    cash_balance = db.Column(db.Float, default=100_000.0)
    created_at = db.Column(db.DateTime(timezone=True), default=_now)

    positions = db.relationship("Position", backref="portfolio", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "name": self.name,
            "type": self.type,
            "cash_balance": self.cash_balance,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Position(db.Model):
    __tablename__ = "positions"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    portfolio_id = db.Column(db.String(36), db.ForeignKey("portfolios.id"), nullable=False, index=True)
    symbol = db.Column(db.String(10), nullable=False)
    shares = db.Column(db.Float, nullable=False)
    avg_cost = db.Column(db.Float, nullable=False)
    acquired_at = db.Column(db.DateTime(timezone=True), default=_now)

    def to_dict(self):
        return {
            "id": self.id,
            "portfolio_id": self.portfolio_id,
            "symbol": self.symbol,
            "shares": self.shares,
            "avg_cost": self.avg_cost,
            "acquired_at": self.acquired_at.isoformat() if self.acquired_at else None,
        }


class Watchlist(db.Model):
    __tablename__ = "watchlists"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), unique=True, nullable=False)
    symbols = db.Column(db.JSON, default=list, nullable=False)


class Alert(db.Model):
    __tablename__ = "alerts"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    symbol = db.Column(db.String(10), nullable=False)
    type = db.Column(db.String(20), nullable=False)
    condition = db.Column(db.String(20), nullable=False)
    threshold = db.Column(db.Float, nullable=False)
    notify_inapp = db.Column(db.Boolean, default=True)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime(timezone=True), default=_now)

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "symbol": self.symbol,
            "type": self.type,
            "condition": self.condition,
            "threshold": self.threshold,
            "notify_inapp": self.notify_inapp,
            "active": self.active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
