import json
import logging
import os
import subprocess

from config import PLATFORM_COOKIES

log = logging.getLogger("daiko.platform.sukikirai")


class SukikiraiPlatform:
    id = "sukikirai"
    label = "好き嫌い.com"

    def _cookies(self) -> str:
        path = PLATFORM_COOKIES.get("sukikirai")
        if not path or not os.path.exists(path):
            raise RuntimeError("好き嫌い.com cookies not found (cookies_sukikirai.json)")
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, list):
            pairs = [f"{c.get('name','')}={c.get('value','')}" for c in raw if c.get("name")]
            return "; ".join(p for p in pairs if "=" in p and p.split("=")[1])
        if isinstance(raw, dict):
            return "; ".join(f"{k}={v}" for k, v in raw.items())
        return str(raw)

    async def post(self, sub):
        cookie = self._cookies()
        # TODO(endpoint): real POST target + payload once cookies supplied.
        raise NotImplementedError(
            "sukikirai endpoint not wired yet — supply cookies + probe site"
        )
        url = "https://<sukikirai-post-endpoint>"
        payload = {
            "bbs_id": sub.target_tweet_id,
            "message": sub.content,
        }
        cmd = ["curl", "-s", "-m", "20", "-L",
               "-H", f"Cookie: {cookie}",
               "-H", "User-Agent: Mozilla/5.0"]
        for k, v in payload.items():
            cmd += ["--data-urlencode", f"{k}={v}"]
        cmd.append(url)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        log.info("sukikirai post rc=%s out=%s err=%s", r.returncode, r.stdout[:200], r.stderr[:200])
        return {"tweet_id": "", "tweet_url": ""}
