"""
suki-kira.com (好き嫌い.com) platform adapter.

VERIFIED WORKING (2026-08-18) — full flow:
  1. GET  /people/result/<name> (via Flaresolverr) -> if comment form present, skip vote
  2. If no comment form: POST /people/result/<name> a vote (好き=1 / 嫌い=0)
     with vote, ok, id, auth1, auth2, auth-r from the vote page.
  3. GET  /people/result/<name> again -> comment form with auth tokens
  4. POST /people/comment/<id>/ with body + name_id + type + url + all hiddens
     -> comment posted, redirects to /people/vote/<name>#comment

Cloudflare + FingerprintJS-issued auth tokens are produced by the in-house
Flaresolverr (http://10.0.1.42:8191/v1 on the same coolify network).
"""

import json
import logging
import os
import re
import urllib.parse
import urllib.request

from config import PLATFORM_COOKIES

log = logging.getLogger("daiko.platform.sukikirai")

FLARE_URL = os.environ.get("FLARE_URL", "http://10.0.1.42:8191/v1")


class SukikiraiPlatform:
    id = "sukikirai"
    label = "好き嫌い.com"

    _UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    _BASE = "https://suki-kira.com"

    def _flare(self, cmd, url, post_data=None, extra_headers=None, timeout=45):
        body = {"cmd": cmd, "url": url, "maxTimeout": 40000}
        headers = {"User-Agent": self._UA}
        if extra_headers:
            headers.update(extra_headers)
        body["headers"] = headers
        if post_data is not None:
            body["postData"] = post_data
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            FLARE_URL, data=payload,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "ignore"))
        except Exception as e:
            raise RuntimeError(f"Flaresolverr error: {e}")

    def _person_from_target(self, sub):
        url = (getattr(sub, "target_tweet_url", "") or "").strip()
        if not url:
            return None
        m = re.search(r"/people/(?:vote|result)/([^?#]+)", url)
        if m:
            return urllib.parse.unquote(m.group(1))
        return None

    def _type_value(self, sub):
        """Map submitted 好き/嫌い to the site's type value.
        The comment form's type select uses option text 好き派/嫌い派."""
        tv = (getattr(sub, "poll_choices", "") or "").strip()
        if "嫌い" in tv:
            return "嫌い派"
        if "好き" in tv:
            return "好き派"
        return ""

    def _hidden(self, html, field):
        m = re.search(r'name="%s" value="([^"]*)"' % re.escape(field), html)
        return m.group(1) if m else ""

    def search(self, q, limit=10):
        """Search people. Returns list of {name, url} candidates."""
        q = (q or "").strip()
        if not q:
            return []
        d = self._flare("request.get",
                        f"{self._BASE}/search?q={urllib.parse.quote(q)}")
        html = (d.get("solution", {}) or {}).get("response") or ""
        if not html:
            return []
        seen = set()
        out = []
        for m in re.finditer(r'href="(/people/vote/[^"]+)"[^>]*>\s*([^<]{1,60})', html):
            url = m.group(1)
            name1 = re.sub(r"\s+", " ", m.group(2)).strip()
            if not name1 or name1.startswith("'"):
                # fallback: decode from URL
                name1 = urllib.parse.unquote(url.split("/people/vote/")[1])
            if url in seen:
                continue
            seen.add(url)
            out.append({"name": name1, "url": self._BASE + url})
            if len(out) >= limit:
                break
        return out

    def _result_html(self, name):
        rurl = f"{self._BASE}/people/result/{urllib.parse.quote(name)}"
        d = self._flare("request.get", rurl)
        html = (d.get("solution", {}) or {}).get("response") or ""
        if not html:
            raise RuntimeError(f"resultページ取得失敗 ({d.get('message')})")
        return rurl, html

    def _cast_vote(self, name):
        """Vote (好き) on the person so the server unlocks the comment form."""
        vurl = f"{self._BASE}/people/vote/{urllib.parse.quote(name)}"
        d = self._flare("request.get", vurl)
        vin = (d.get("solution", {}) or {}).get("response") or ""
        if not vin:
            raise RuntimeError(f"投票ページ取得失敗 ({d.get('message')})")
        pid = self._hidden(vin, "id")
        auth1 = self._hidden(vin, "auth1")
        auth2 = self._hidden(vin, "auth2")
        authr = self._hidden(vin, "auth-r")
        if not (pid and auth1 and auth2):
            raise RuntimeError("suki-kira: 投票フォームのauthトークン取得失敗")
        post_data = f"vote=1&ok=ng&id={urllib.parse.quote(pid)}&auth1={urllib.parse.quote(auth1)}&auth2={urllib.parse.quote(auth2)}&auth-r={urllib.parse.quote(authr)}"
        rurl = f"{self._BASE}/people/result/{urllib.parse.quote(name)}"
        dv = self._flare("request.post", rurl, post_data=post_data,
                         extra_headers={"Content-Type": "application/x-www-form-urlencoded",
                                        "Referer": vurl})
        out = (dv.get("solution", {}) or {}).get("response") or ""
        log.info("suki-kira vote: status=%s bytes=%s", dv.get("status"), len(out))
        return pid, out

    async def post(self, sub):
        name = self._person_from_target(sub)
        if not name:
            raise ValueError("suki-kira: 対象人物ページのURL (/people/vote/<名前>) が必要です")
        content = (sub.content or "").strip()
        if not content:
            raise ValueError("suki-kira: コメント本文が必要です")

        # 1) Try to get comment form directly from result page
        rurl, html = self._result_html(name)
        if 'id="comment-submit-modal"' not in html:
            # Still no comment form -> vote first to unlock it
            log.info("suki-kira: コメントフォーム無し -> 投票してアンロック")
            pid, html = self._cast_vote(name)
            if 'id="comment-submit-modal"' not in html:
                raise RuntimeError("suki-kira: 投票後もコメントフォームが見つかりません（1日1回制限の可能性）")

        payload = {
            "id": self._hidden(html, "id"),
            "name_id": (getattr(sub, "poll_choices", "") or "").strip()[:50],
            "type": self._type_value(sub),
            "url": self._hidden(html, "url"),
            "body": content,
            "sum": self._hidden(html, "sum"),
            "auth1": self._hidden(html, "auth1"),
            "auth2": self._hidden(html, "auth2"),
            "auth-r": self._hidden(html, "auth-r"),
            "ok": self._hidden(html, "ok"),
            "tag_id": self._hidden(html, "tag_id"),
            "form_ts": self._hidden(html, "form_ts"),
            "form_sig": self._hidden(html, "form_sig"),
        }
        if not payload["auth1"] or not payload["form_sig"]:
            raise RuntimeError("suki-kira: 認証トークン(form_sig)が取得できません")

        post_data = "&".join(
            f"{urllib.parse.quote(k)}={urllib.parse.quote(v)}" for k, v in payload.items())
        person_id = payload["id"] or self._hidden(html, "id")

        d2 = self._flare(
            "request.post", f"{self._BASE}/people/comment/{person_id}/",
            post_data=post_data,
            extra_headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": rurl,
            })
        sol2 = d2.get("solution", {}) or {}
        out = sol2.get("response") or ""
        redirect = sol2.get("url") or ""
        log.info("suki-kira comment: status=%s bytes=%s redirect=%s",
                 d2.get("status"), len(out), redirect)
        if "#comment" in redirect or "people/vote" in redirect:
            return {"tweet_id": person_id or name, "tweet_url": f"{self._BASE}/people/result/{urllib.parse.quote(name)}"}
        raise RuntimeError(f"suki-kira: コメント投稿失敗 ({d2.get('message')})")
