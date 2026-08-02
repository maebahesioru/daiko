from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, func
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from datetime import datetime, timezone
from config import DATABASE_FILE

engine = create_engine(f"sqlite:///{DATABASE_FILE}", connect_args={"check_same_thread": False})


class Base(DeclarativeBase):
    pass


class Submission(Base):
    __tablename__ = "submissions"

    id = Column(Integer, primary_key=True, autoincrement=True)
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
    error_message = Column(Text, default="")
    result_tweet_id = Column(String(64), default="")
    result_tweet_url = Column(Text, default="")


SessionLocal = sessionmaker(bind=engine)


def init_db():
    Base.metadata.create_all(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
