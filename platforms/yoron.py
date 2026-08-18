"""
tuber-review.com (Youtuber世論調査) platform adapter.

VERIFIED WORKING (2026-08-18) — login NOT required, posts as "匿名":
  1. GET  /entries/<entry_id>/comments/create   -> CSRF token + session cookie
  2. POST /comments/confirm   (fields: _token, commentator, sex, age,
                              rating_scores[1..10], content, entry_id, duplicationCount)
                             -> returns confirmation page (comment preview)
  3. POST /comments/store     (resend all values) -> comment is posted,
                             redirects back to the youtuber entry page.

Implemented with pure stdlib (urllib) so it runs in the slim container
(no curl binary). Optional cookie file: cookies_yoron.json (browser-exported).
"""

import json
import logging
import os
import re
import urllib.parse
import urllib.request
import http.cookiejar

from config import PLATFORM_COOKIES

log = logging.getLogger("daiko.platform.tuber")


class TuberPlatform:
    id = "yoron"
    label = "Youtuber世論調査"

    _UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    _BASE = "https://tuber-review.com"
    _BROWSER_HEADERS = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }

    def __init__(self):
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar))
        # Merge optional browser cookies into the jar
        path = PLATFORM_COOKIES.get("yoron")
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, list):
                    for c in raw:
                        if c.get("name") and c.get("value"):
                            self._jar.set_cookie(http.cookiejar.Cookie(
                                version=0, name=c["name"], value=c["value"],
                                port=None, port_specified=False,
                                domain=c.get("domain", ".tuber-review.com"),
                                domain_specified=True, domain_initial_dot=True,
                                path=c.get("path", "/"), path_specified=True,
                                secure=bool(c.get("secure")), expires=None,
                                discard=True, comment=None, comment_url=None,
                                rest={"HttpOnly": c.get("httpOnly", False)},
                                rfc2109=False))
                elif isinstance(raw, dict):
                    for k, v in raw.items():
                        self._jar.set_cookie(http.cookiejar.Cookie(
                            version=0, name=k, value=str(v), port=None,
                            port_specified=False, domain=".tuber-review.com",
                            domain_specified=True, domain_initial_dot=True,
                            path="/", path_specified=True, secure=False,
                            expires=None, discard=True, comment=None,
                            comment_url=None, rest={}, rfc2109=False))
            except Exception as e:
                log.warning("yoron cookie merge failed: %s", e)

    def _request(self, url, data=None, headers=None, timeout=30):
        """urllib GET/POST helper. data: dict -> form-encoded POST."""
        hdrs = dict(self._BROWSER_HEADERS)
        if headers:
            hdrs.update(headers)
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        req = urllib.request.Request(url, data=body, headers=hdrs)
        with self._opener.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "ignore")

    async def post(self, sub):
        entry_id = self._extract_entry_id(sub)
        if not entry_id:
            raise ValueError("tuber-review: 対象YouTuberのURL (/youtubers/<id>) か ID が必要です")
        content = (sub.content or "").strip()
        if not content:
            raise ValueError("tuber-review: コメント本文が必要です")

        base = f"{self._BASE}/entries/{entry_id}/comments/create"

        # 1) fetch create page -> csrf
        html = self._request(base, headers={"Referer": self._BASE})
        m = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
        token = m.group(1) if m else None
        if not token:
            raise RuntimeError("tuber-review: CSRFトークンを取得できません")

        # 2) Build fields from optional extra data (name/sex/age/ratings)
        extra = self._extra(sub)

        # 3) confirm POST (body returned so we can detect the store form)
        fields = [
            ("_token", token), ("commentator", extra.get("name", "")),
            ("sex", extra.get("sex", "")), ("age", str(extra.get("age", ""))),
            ("content", content), ("entry_id", entry_id), ("duplicationCount", ""),
        ]
        ratings = extra.get("ratings", {})
        for i in range(1, 11):
            fields.append((f"rating_scores[{i}]", str(ratings.get(str(i), 0))))
        conf_html = self._request(
            f"{self._BASE}/comments/confirm",
            data=dict(fields),
            headers={"Referer": base})
        if "/comments/store" not in conf_html:
            raise RuntimeError("tuber-review: 確認ページに遷移しませんでした")

        # 4) store POST (resend all)
        try:
            self._request(
                f"{self._BASE}/comments/store",
                data=dict(fields),
                headers={"Referer": f"{self._BASE}/comments/confirm"})
        except urllib.error.HTTPError as e:
            if e.code not in (301, 302):
                raise RuntimeError(f"tuber-review 投稿失敗 HTTP{e.code}")
        url = f"{self._BASE}/youtubers/{entry_id}"
        log.info("tuber-review store ok url=%s", url)
        return {"tweet_id": entry_id, "tweet_url": url}

    def _extra(self, sub):
        """Parse poll_choices JSON (name/sex/age/ratings) if present."""
        raw = (getattr(sub, "poll_choices", "") or "").strip()
        if raw.startswith("{"):
            try:
                d = json.loads(raw)
                if isinstance(d, dict):
                    return {
                        "name": str(d.get("name", "") or "")[:20],
                        "sex": str(d.get("sex", "") or ""),
                        "age": str(d.get("age", "") or ""),
                        "ratings": d.get("ratings", {}) if isinstance(d.get("ratings"), dict) else {},
                    }
            except Exception:
                pass
        # Fallback: legacy internal_note parsing
        note = (getattr(sub, "internal_note", "") or "")
        return {
            "name": "",
            "sex": "female" if "女性" in note else ("male" if "男性" in note else ""),
            "age": (re.search(r"(10|20|30|40|50|60|70)代", note) or [None, ""])[1],
            "ratings": {},
        }

    def _extract_entry_id(self, sub):
        tid = (sub.target_tweet_id or "").strip()
        if tid and tid.isdigit():
            return tid
        url = (sub.target_tweet_url or "").strip()
        m = re.search(r"/youtubers/(\d+)", url)
        if m:
            return m.group(1)
        return None

    def search(self, q, limit=10):
        """Search youtubers. POST /search with q + csrf token.
        Returns list of {name, url} candidates (real hits only)."""
        q = (q or "").strip()
        if not q:
            return []
        # 1) session + csrf
        html0 = self._request(self._BASE)
        m = re.search(r'<meta name="csrf-token" content="([^"]+)"', html0)
        token = m.group(1) if m else None
        if not token:
            return []
        # 2) POST search
        res = self._request(
            f"{self._BASE}/search",
            data={"q": q, "_token": token},
            headers={"Referer": self._BASE})
        out = []
        seen = set()
        for blk in re.finditer(r'<div class="entry-list-item".*?(?=<div class="entry-list-item"|$)', res, re.S):
            seg = blk.group(0)
            a = re.search(r'href="(https://tuber-review\.com/youtubers/\d+)"[^>]*>\s*([^<]{1,60})', seg)
            if not a:
                a = re.search(r'href="(https://tuber-review\.com/youtubers/\d+)[^"]*"[^>]*>', seg)
            if not a:
                continue
            url = a.group(1)
            name1 = a.group(2).strip() if len(a.groups()) > 1 and a.group(2) else ""
            if not name1 or name1.count(" ") > 4:
                b = re.search(r'font-weight:600">\s*([^<]{1,60})', seg)
                if b:
                    name1 = b.group(1).strip()
            if not name1:
                continue
            key = (url, name1)
            if key in seen:
                continue
            seen.add(key)
            out.append({"name": name1, "url": url})
            if len(out) >= limit:
                break
        return out
