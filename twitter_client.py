"""
Twitter client wrapper using twifork (twikit fork) with cookie-based auth.
Never calls login() — uses browser cookies to avoid account lockouts.
"""
import os
import json
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
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # backward compatibility: single filename as plain string
        return [raw] if raw else []


class TwitterClient:
    def __init__(self):
        self._client = None

    async def _get_client(self):
        if self._client is not None:
            return self._client
        from twikit import Client
        client = Client(language="ja")
        cookies = _load_cookies_as_dict()
        client.set_cookies(cookies)
        uid = await client.user_id()
        log.info("Twitter auth OK — user_id=%s", uid)
        self._client = client
        return client

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
        # Delete local file after successful upload
        try:
            os.remove(path)
            log.info("Deleted local file: %s", path)
        except OSError:
            pass
        return media_id

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
        tweet = await client.create_tweet(
            text=text, media_ids=media_ids or None, poll_uri=poll_uri
        )
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
        tweet = await client.create_tweet(
            text=text, reply_to=reply_to_tweet_id, media_ids=media_ids or None,
            poll_uri=poll_uri
        )
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


twitter = TwitterClient()
