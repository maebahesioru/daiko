"""
tuber-review.com (Youtuber世論調査) platform adapter.

VERIFIED WORKING (2026-08-18) — login NOT required, posts as "匿名":
  1. GET  /entries/<entry_id>/comments/create   -> CSRF token + session cookie
  2. POST /comments/confirm   (fields: _token, commentator, sex, age,
                              rating_scores[1..10], content, entry_id, duplicationCount)
                             -> returns confirmation page (comment preview)
  3. POST /comments/store     (resend all values) -> comment is posted,
                             redirects back to the youtuber entry page.

Optional cookie file: cookies_yoron.json (browser-exported). If absent,
posts fully anonymously (works, verified).
"""

import json
import logging
import os
import re
import subprocess

from config import PLATFORM_COOKIES

log = logging.getLogger("daiko.platform.tuber")


class TuberPlatform:
    id = "yoron"
    label = "Youtuber世論調査"

    _UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    _BASE = "https://tuber-review.com"

    def _cookie_args(self):
        jar = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tuber_jar.txt")
        args = ["-c", jar, "-b", jar]
        path = PLATFORM_COOKIES.get("yoron")
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                pairs = []
                if isinstance(raw, list):
                    for c in raw:
                        if c.get("name") and c.get("value"):
                            pairs.append(f"{c['name']}\t{c['value']}")
                elif isinstance(raw, dict):
                    for k, v in raw.items():
                        pairs.append(f"{k}\t{v}")
                merge = jar + ".auth"
                with open(merge, "w", encoding="utf-8") as f:
                    f.write("\n".join(pairs))
                args += ["-b", merge]
            except Exception as e:
                log.warning("yoron cookie merge failed: %s", e)
        return args

    def _run(self, args, timeout=45, referer=None):
        cmd = ["curl", "-s", "-m", "25", "-A", self._UA] + self._cookie_args()
        if referer:
            cmd += ["-e", referer]
        cmd += args
        r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           cwd=os.path.dirname(os.path.abspath(__file__)))
        return r.stdout

    async def post(self, sub):
        entry_id = self._extract_entry_id(sub)
        if not entry_id:
            raise ValueError("tuber-review: 対象YouTuberのURL (/youtubers/<id>) か ID が必要です")
        content = (sub.content or "").strip()
        if not content:
            raise ValueError("tuber-review: コメント本文が必要です")

        base = f"{self._BASE}/entries/{entry_id}/comments/create"

        # 1) fetch create page -> csrf
        html = self._run(["-L", "-w", "\n__S__%{http_code}", base]).decode("utf-8", "ignore")
        m = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
        token = m.group(1) if m else None
        if not token:
            raise RuntimeError("tuber-review: CSRFトークンを取得できません")

        # 2) confirm POST (body to stdout so we can detect the store form)
        fields = [
            ("_token", token), ("commentator", self._commentator(sub)),
            ("sex", self._sex_value(sub)), ("age", str(self._age_value(sub))),
            ("content", content), ("entry_id", entry_id), ("duplicationCount", ""),
        ]
        for i in range(1, 11):
            fields.append((f"rating_scores[{i}]", "0"))
        cmd = ["-L", "-w", "\n__S__%{http_code}"]
        for k, v in fields:
            cmd += ["--data-urlencode", f"{k}={v}"]
        cmd += [f"{self._BASE}/comments/confirm"]
        conf_html = self._run(cmd, referer=base).decode("utf-8", "ignore")
        if "/comments/store" not in conf_html:
            raise RuntimeError("tuber-review: 確認ページに遷移しませんでした")

        # 3) store POST (resend all)
        cmd2 = ["-L", "-o", os.devnull,
                "-w", "__S__%{http_code}__%{redirect_url}"]
        for k, v in fields:
            cmd2 += ["--data-urlencode", f"{k}={v}"]
        cmd2 += [f"{self._BASE}/comments/store"]
        res = self._run(cmd2, referer=f"{self._BASE}/comments/confirm").decode("utf-8", "ignore")
        ms = re.search(r"__S__(\d+)__(\S*)", res)
        http = ms.group(1) if ms else "?"
        url = f"{self._BASE}/youtubers/{entry_id}"
        log.info("tuber-review store HTTP%s url=%s", http, url)
        if http.startswith("2") or http == "302":
            return {"tweet_id": entry_id, "tweet_url": url}
        raise RuntimeError(f"tuber-review 投稿失敗 HTTP{http}")

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
        cwd = os.path.dirname(os.path.abspath(__file__))
        # 1) session + csrf
        html0 = self._run(["-L", self._BASE]).decode("utf-8", "ignore")
        m = re.search(r'<meta name="csrf-token" content="([^"]+)"', html0)
        token = m.group(1) if m else None
        if not token:
            return []
        # 2) POST search
        subprocess.run(
            ["curl", "-s", "-m", "25", "-L", "-A", self._UA,
             "-e", self._BASE, "-o", "search_res.html",
             "--data-urlencode", f"q={q}", "--data-urlencode", f"_token={token}",
             f"{self._BASE}/search"],
            capture_output=True, timeout=40, cwd=cwd)
        res = open(os.path.join(cwd, "search_res.html"),
                   encoding="utf-8", errors="ignore").read()
        out = []
        seen = set()
        # Real hits live inside "entry-list-item" blocks; each has a
        # font-weight:600 <a href="/youtubers/<id>">NAME</a>.
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
                # fall back to bold name inside block
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

    def _commentator(self, sub):
        return (getattr(sub, "poll_choices", "") or "").strip()[:20] or ""

    def _sex_value(self, sub):
        note = (getattr(sub, "internal_note", "") or "").lower()
        if "女性" in note:
            return "female"
        if "男性" in note:
            return "male"
        return ""

    def _age_value(self, sub):
        note = (getattr(sub, "internal_note", "") or "")
        m = re.search(r"(10|20|30|40|50|60|70)代", note)
        return m.group(1) if m else ""
