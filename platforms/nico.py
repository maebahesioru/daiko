import logging
import os

from config import PLATFORM_COOKIES

log = logging.getLogger("daiko.platform.nico")


class NicoPlatform:
    id = "nico"
    label = "ニコニコ大百科"

    def _cookies(self) -> str:
        path = PLATFORM_COOKIES.get("nico")
        if not path or not os.path.exists(path):
            raise RuntimeError("ニコニコ大百科 cookies not found (cookies_nico.json)")
        return path

    async def post(self, sub):
        # ニコニコ大百科: 掲示板投稿はニコニコアカウント必須。
        # nicodic の掲示板は「あんWiki」形式 or 公式掲示板 API 経由。
        # TODO(endpoint): 実際の投稿 API (animewiki / bulle / newtopic) を調査して配線。
        # 参考: dic.nicovideo.jp の「あんWiki」は Cookie + POST で投稿可。
        raise NotImplementedError(
            "nico endpoint not wired yet — need cookies + site probe"
        )
