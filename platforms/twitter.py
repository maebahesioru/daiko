"""
Twitter platform adapter — wraps the existing TwitterClient.
Keeps all existing behavior (threads, polls, media, duplicate recovery).
"""

import asyncio
import logging
from twitter_client import twitter, _parse_media_list

log = logging.getLogger("daiko.platform.twitter")


class TwitterPlatform:
    id = "twitter"
    label = "Twitter"

    async def post(self, sub):
        media_json = sub.media_file or "[]"
        poll_choices = sub.poll_choices_as_list() if hasattr(sub, "poll_choices_as_list") else _parse_poll(sub.poll_choices)
        poll_duration = sub.poll_duration or 0
        thread_items = _parse_json(sub.thread_items)

        if sub.submit_type == "retweet":
            return await twitter.do_retweet(sub.target_tweet_id, bool(sub.like_original))

        if thread_items:
            reply_to = sub.target_tweet_id if sub.submit_type == "reply" else None
            return await twitter.post_thread(
                sub.content, thread_items, media_json,
                poll_choices, poll_duration, reply_to_id=reply_to,
            )

        if sub.submit_type == "tweet":
            return await twitter.post_tweet(sub.content, media_json, poll_choices, poll_duration)

        if sub.submit_type == "reply":
            return await twitter.post_reply(
                sub.content, sub.target_tweet_id, bool(sub.like_original),
                media_json, poll_choices, poll_duration,
            )
        raise ValueError(f"Unsupported twitter submit_type: {sub.submit_type}")

    def validate(self, form, files):
        return None, form


def _parse_json(raw):
    import json
    if not raw or raw == "[]":
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _parse_poll(raw):
    import json
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
