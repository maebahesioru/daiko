from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, func, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from datetime import datetime, timezone
from config import DATABASE_FILE

engine = create_engine(f"sqlite:///{DATABASE_FILE}", connect_args={"check_same_thread": False})


class Base(DeclarativeBase):
    pass


class Submission(Base):
    __tablename__ = "submissions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(20), default="twitter")  # twitter, sukikirai, nico, pixiv, yoron
    submit_type = Column(String(10), nullable=False)   # 'tweet', 'retweet', 'reply'
    content = Column(Text, default="")                  # tweet text
    target_tweet_url = Column(Text, default="")         # original tweet URL for RT/reply
    target_tweet_id = Column(String(64), default="")    # parsed tweet ID
    like_original = Column(Integer, default=0)          # 0 or 1
    media_file = Column(Text, default="[]")              # JSON array of uploaded filenames
    poll_choices = Column(Text, default="")              # JSON: ["choice1","choice2",...]
    poll_duration = Column(Integer, default=0)           # minutes, 0=no poll
    thread_items = Column(Text, default="")               # JSON array of sub-tweets
    status = Column(String(20), default="pending")      # pending, approved, rejected, posted, failed
    submitted_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    reviewed_at = Column(DateTime, nullable=True)
    posted_at = Column(DateTime, nullable=True)
    admin_note = Column(Text, default="")
    internal_note = Column(Text, default="")   # submitter→admin internal message (never posted)
    edit_pin = Column(String(4), default="")   # 4-digit PIN for editing after submit
    error_message = Column(Text, default="")
    result_tweet_id = Column(String(64), default="")
    result_tweet_url = Column(Text, default="")

    def poll_choices_parsed(self):
        """Parse poll_choices: JSON dict (yoron/sukikirai extra) or raw str."""
        import json as _j
        raw = self.poll_choices or ""
        if raw.startswith("{"):
            try:
                d = _j.loads(raw)
                if isinstance(d, dict):
                    name = str(d.get("name", "") or "")
                    return type("EX", (), {
                        "name": name,
                        "sex": str(d.get("sex", "") or ""),
                        "age": str(d.get("age", "") or ""),
                        "type": str(d.get("type", "") or ""),
                        "ratings": d.get("ratings", {}) if isinstance(d.get("ratings"), dict) else {},
                    })()
            except Exception:
                pass
        return type("EX", (), {"name": "", "sex": "", "age": "", "type": raw, "ratings": {}})()


SessionLocal = sessionmaker(bind=engine)


def init_db():
    Base.metadata.create_all(engine)
    _migrate()


def _migrate():
    """Add columns missing from older databases (create_all doesn't alter tables)."""
    with engine.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(submissions)"))}
        additions = {
            "internal_note": "ALTER TABLE submissions ADD COLUMN internal_note TEXT DEFAULT ''",
            "edit_pin": "ALTER TABLE submissions ADD COLUMN edit_pin VARCHAR(4) DEFAULT ''",
            "platform": "ALTER TABLE submissions ADD COLUMN platform VARCHAR(20) DEFAULT 'twitter'",
        }
        for name, sql in additions.items():
            if name not in cols:
                conn.execute(text(sql))
        conn.commit()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
