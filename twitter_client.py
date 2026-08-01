"""
Twitter client wrapper using twifork (twikit fork) with cookie-based auth.
Never calls login() — uses browser cookies to avoid account lockouts.
"""
import os
import json
import asyncio
import logging
from config import COOKIES_FILE, UPLOAD_DIR

log = logging.getLogger("daiko.twitter")


# --- twifork ClientTransaction bug fix (required for twifork 2.3.5) ---
def _apply_twifork_patch():
    try:
        from twikit.x_client_transaction import transaction as _txn
        _orig_init = _txn.ClientTransaction.__init__
        def _patched_init(self):
            _orig_init(self)
            self.key = None
            self.animation_key = None
        _txn.ClientTransaction.__init__ = _patched_init
        log.info("twifork ClientTransaction patch applied")
    except Exception as e:
        log.warning("Could not apply twifork patch: %s", e)

_apply_twifork_patch()


def _load_cookies_as_dict():
    with open(COOKIES_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    cookies = {}
    for c in raw:
        name = c.get("name", "")
        if name in ("auth_token", "ct0"):
            cookies[name] = c["value"]
    return cookies


def _parse_media_list(raw: str) -> list[str]:
    """Parse JSON array of media filenames."""
    if not raw or raw in ("", "[]"):
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return [raw] if raw else []
    # Handle both ["file.jpg"] and [{"file":"x.jpg","alt":"desc"}] formats
    result = []
    for item in parsed:
        if isinstance(item, dict):
            result.append(item.get("file", ""))
        elif isinstance(item, str):
            result.append(item)
    return result


class TwitterClient:
    def __init__(self):
        self._client = None

    async def _get_client(self):
        if self._client is not None:
            return self._client
        from twikit import Client
        # Retry client creation — x.com sometimes returns 302/consent pages
        # which make twikit's client_transaction fail with KEY_BYTE indices error.
        last_err = None
        for attempt in range(3):
            try:
                client = Client(language="ja")
                cookies = _load_cookies_as_dict()
                client.set_cookies(cookies)
                uid = await client.user_id()
                log.info("Twitter auth OK — user_id=%s", uid)
                self._client = client
                return client
            except Exception as e:
                last_err = e
                log.warning("Client auth attempt %d failed: %s", attempt + 1, e)
                await asyncio.sleep(5 * (attempt + 1))
        raise last_err or RuntimeError("Failed to create Twitter client")

    async def invalidate_client(self):
        """Drop cached client so next call re-auths (for transient twikit failures)."""
        if self._client is not None:
            self._client = None
            log.info("Twitter client invalidated")

    async def _upload_media(self, filename: str) -> str | None:
        """Upload a media file. Returns media_id or None if no file.
        Auto-detects video (mp4/mov) vs image."""
        if not filename:
            return None
        path = os.path.join(UPLOAD_DIR, filename)
        if not os.path.exists(path):
            log.warning("Media file not found: %s", path)
            return None
        client = await self._get_client()
        ext = os.path.splitext(filename)[1].lower()
        if ext in (".mp4", ".mov"):
            log.info("Uploading video: %s", filename)
            media_id = await client.upload_media(
                path, wait_for_completion=True, media_category="tweet_video"
            )
        else:
            log.info("Uploading image: %s", filename)
            media_id = await client.upload_media(path)
        log.info("Media uploaded: %s -> %s", filename, media_id)
        return media_id

    async def _recover_duplicate(self, client, text: str) -> dict | None:
        """Find an already-posted tweet with identical text (duplicate 187 recovery).
        Returns {tweet_id, tweet_url} if found, None otherwise."""
        try:
            uid = await client.user_id()
            me = await client.get_user_by_id(uid)
            # Fetch our recent tweets and look for exact text match
            tweets = await client.get_user_tweets(uid, "Tweets", count=20)
            for tw in tweets:
                if tw.text and tw.text.strip() == text.strip():
                    author = getattr(tw, "user", None)
                    sn = getattr(author, "screen_name", me.screen_name)
                    url = f"https://x.com/{sn}/status/{tw.id}"
                    log.info("Duplicate recovered: %s", url)
                    return {"tweet_id": tw.id, "tweet_url": url}
            log.warning("Duplicate error but no matching tweet found in last 20")
        except Exception as e:
            log.warning("Duplicate recovery search failed: %s", e)
        return None

    async def post_tweet(self, text: str, media_filenames: str = "[]",
                          poll_choices: list[str] | None = None,
                          poll_duration: int = 0) -> dict:
        client = await self._get_client()
        poll_uri = None
        if poll_choices and len(poll_choices) >= 2:
            poll_uri = await client.create_poll(choices=poll_choices, duration_minutes=poll_duration or 1440)
            log.info("Poll created with %d choices (%d min)", len(poll_choices), poll_duration or 1440)
        media_ids = []
        if not poll_uri:
            for fname in _parse_media_list(media_filenames):
                mid = await self._upload_media(fname)
                if mid:
                    media_ids.append(mid)
        try:
            tweet = await client.create_tweet(
                text=text, media_ids=media_ids or None, poll_uri=poll_uri
            )
        except Exception as e:
            if "duplicate" in str(e).lower() or "187" in str(e):
                recovered = await self._recover_duplicate(client, text)
                if recovered:
                    return recovered
            raise
        tid = tweet.id
        author = tweet.user.screen_name
        url = f"https://x.com/{author}/status/{tid}"
        log.info("Tweet posted: %s -> %s", tid, url)
        return {"tweet_id": tid, "tweet_url": url}

    async def post_reply(self, text: str, reply_to_tweet_id: str,
                         like_original: bool = False, media_filenames: str = "[]",
                         poll_choices: list[str] | None = None,
                         poll_duration: int = 0) -> dict:
        client = await self._get_client()
        if like_original:
            await client.favorite_tweet(reply_to_tweet_id)
            log.info("Liked original tweet %s", reply_to_tweet_id)
        poll_uri = None
        if poll_choices and len(poll_choices) >= 2:
            poll_uri = await client.create_poll(choices=poll_choices, duration_minutes=poll_duration or 1440)
            log.info("Poll created with %d choices (%d min)", len(poll_choices), poll_duration or 1440)
        media_ids = []
        if not poll_uri:
            for fname in _parse_media_list(media_filenames):
                mid = await self._upload_media(fname)
                if mid:
                    media_ids.append(mid)
        try:
            tweet = await client.create_tweet(
                text=text, reply_to=reply_to_tweet_id, media_ids=media_ids or None,
                poll_uri=poll_uri
            )
        except Exception as e:
            if "duplicate" in str(e).lower() or "187" in str(e):
                recovered = await self._recover_duplicate(client, text)
                if recovered:
                    return recovered
            raise
        tid = tweet.id
        author = tweet.user.screen_name
        url = f"https://x.com/{author}/status/{tid}"
        log.info("Reply posted: %s -> %s", tid, url)
        return {"tweet_id": tid, "tweet_url": url}

    async def do_retweet(self, target_tweet_id: str, like_original: bool = False) -> dict:
        client = await self._get_client()
        if like_original:
            await client.favorite_tweet(target_tweet_id)
            log.info("Liked original tweet %s", target_tweet_id)
        await client.retweet(target_tweet_id)
        url = f"https://x.com/i/status/{target_tweet_id}"
        log.info("Retweeted: %s", target_tweet_id)
        return {"tweet_id": target_tweet_id, "tweet_url": url}

    async def post_thread(self, main_text: str, thread_items: list[dict],
                          media_filenames: str = "[]",
                          poll_choices: list[str] | None = None,
                          poll_duration: int = 0,
                          reply_to_id: str | None = None) -> dict:
        """Post a thread: first tweet + N sub-tweets."""
        client = await self._get_client()

        # Post first tweet
        result = await self.post_tweet(main_text, media_filenames, poll_choices, poll_duration)
        # Override with reply_to if needed
        if reply_to_id:
            result = await self.post_reply(main_text, reply_to_id, False, media_filenames, poll_choices, poll_duration)
        prev_id = result["tweet_id"]

        # Post subsequent tweets in thread
        for item in (thread_items or []):
            text = item.get("content", "") or ""
            medias = item.get("media", "[]") or "[]"
            pc = item.get("poll_choices")
            pd = item.get("poll_duration", 0) or 0
            if not text and medias == "[]" and not pc:
                continue
            r = await self.post_reply(text, prev_id, False, medias, pc, pd)
            prev_id = r["tweet_id"]
            log.info("Thread tweet: %s", r["tweet_url"])

        return result


twitter = TwitterClient()
