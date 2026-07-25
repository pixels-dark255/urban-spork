"""
Database layer. Uses SQLite by default (file: stockapp.db).
On Render's free tier the disk is ephemeral across redeploys, so if you want
predictions/watchlist history to survive redeploys long-term, point DATABASE_URL
at a free Postgres (e.g. Supabase / Neon) instead - SQLAlchemy will work with
either without code changes.
"""
import os
import datetime as dt
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, DateTime, ForeignKey
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./stockapp.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class WatchlistItem(Base):
    __tablename__ = "watchlist"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, index=True, nullable=False)       # e.g. RELIANCE.NS
    display_name = Column(String, nullable=True)
    horizon_minutes = Column(Integer, nullable=False)          # how far ahead to predict
    created_at = Column(DateTime, default=dt.datetime.utcnow)

    predictions = relationship(
        "PredictionRecord", back_populates="watchlist_item", cascade="all, delete-orphan"
    )


class PredictionRecord(Base):
    __tablename__ = "predictions"

    id = Column(Integer, primary_key=True, index=True)
    watchlist_id = Column(Integer, ForeignKey("watchlist.id"))
    symbol = Column(String, index=True, nullable=False)

    made_at = Column(DateTime, default=dt.datetime.utcnow)      # when prediction was made
    target_at = Column(DateTime, nullable=False)                # when it's supposed to resolve
    price_at_prediction = Column(Float, nullable=False)
    predicted_price = Column(Float, nullable=False)
    predicted_low = Column(Float, nullable=True)
    predicted_high = Column(Float, nullable=True)
    confidence = Column(Float, nullable=True)

    actual_price = Column(Float, nullable=True)   # filled in once target_at has passed
    resolved = Column(Integer, default=0)          # 0/1 boolean
    error_pct = Column(Float, nullable=True)

    watchlist_item = relationship("WatchlistItem", back_populates="predictions")


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
