"""
Background worker that processes the approved submission queue.
Posts to Twitter with rate limiting to avoid bans.
"""
import asyncio
import logging
import threading
import time
from datetime import datetime, timezone

from config import MIN_POST_INTERVAL_SECONDS
from models import SessionLocal, Submission
from twitter_client import twitter

log = logging.getLogger("daiko.worker")


class BotWorker:
    def __init__(self):
        self._running = False
        self._thread = None
        self._last_post_time = 0.0

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="bot-worker")
        self._thread.start()
        log.info("Bot worker started")

    def stop(self):
        self._running = False
        log.info("Bot worker stop requested")

    def _loop(self):
        while self._running:
            try:
                asyncio.run(self._process_queue())
            except Exception as e:
                log.exception("Worker loop error: %s", e)
            time.sleep(30)

    async def _process_queue(self):
        db = SessionLocal()
        try:
            sub = (
                db.query(Submission)
                .filter(Submission.status == "approved")
                .order_by(Submission.reviewed_at.asc())
                .first()
            )
            if sub is None:
                return

            # --- Rate limiting ---
            elapsed = time.monotonic() - self._last_post_time
            if elapsed < MIN_POST_INTERVAL_SECONDS:
                wait = MIN_POST_INTERVAL_SECONDS - elapsed
                log.info("Rate limit: waiting %.0fs before next post...", wait)
                time.sleep(wait)

            # --- Post ---
            log.info("Processing submission #%d type=%s media=%s", sub.id, sub.submit_type, sub.media_file or "none")
            try:
                result = await self._execute(sub)
                sub.status = "posted"
                sub.posted_at = datetime.now(timezone.utc)
                sub.result_tweet_id = result.get("tweet_id", "")
                sub.result_tweet_url = result.get("tweet_url", "")
                log.info("Submission #%d posted: %s", sub.id, sub.result_tweet_url)
                # Clean up media files if still present
                if sub.media_file:
                    import os, json as _json2
                    from config import UPLOAD_DIR
                    fnames = _json2.loads(sub.media_file) if sub.media_file.startswith("[") else [sub.media_file]
                    for fname in fnames:
                        path = os.path.join(UPLOAD_DIR, fname)
                        if os.path.exists(path):
                            os.remove(path)
                            log.info("Cleaned up media file: %s", fname)
            except Exception as e:
                sub.status = "failed"
                sub.error_message = str(e)[:500]
                log.exception("Submission #%d failed: %s", sub.id, e)

            db.commit()
            self._last_post_time = time.monotonic()

        finally:
            db.close()

    async def _execute(self, sub: Submission) -> dict:
        import json as _json
        media_json = sub.media_file or "[]"
        poll_choices = _json.loads(sub.poll_choices) if sub.poll_choices else None
        poll_duration = sub.poll_duration or 0
        if sub.submit_type in ("tweet", "quote"):
            # Quote: like original first if requested
            if sub.submit_type == "quote" and sub.like_original and sub.target_tweet_id:
                client = await twitter._get_client()
                await client.favorite_tweet(sub.target_tweet_id)
                log.info("Liked quoted tweet %s", sub.target_tweet_id)
            return await twitter.post_tweet(sub.content, media_json, poll_choices, poll_duration)
        elif sub.submit_type == "retweet":
            tid = sub.target_tweet_id
            if not tid:
                raise ValueError("No target tweet ID for retweet")
            return await twitter.do_retweet(tid, bool(sub.like_original))
        elif sub.submit_type == "reply":
            tid = sub.target_tweet_id
            if not tid:
                raise ValueError("No target tweet ID for reply")
            return await twitter.post_reply(sub.content, tid, bool(sub.like_original), media_json, poll_choices, poll_duration)
        else:
            raise ValueError(f"Unknown submit_type: {sub.submit_type}")


worker = BotWorker()
