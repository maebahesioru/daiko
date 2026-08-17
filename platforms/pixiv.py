import logging
import os

from config import PLATFORM_COOKIES

log = logging.getLogger("daiko.platform.pixiv")


class PixivPlatform:
    id = "pixiv"
    label = "Pixiv百科事典"

    def _cookies(self) -> str:
        path = PLATFORM_COOKIES.get("pixiv")
        if not path or not os.path.exists(path):
            raise RuntimeError("Pixiv百科事典 cookies not found (cookies_pixiv.json)")
        return path

    async def post(self, sub):
        # Pixiv百科事典 (dic.pixiv.net): コメント/トークページ投稿は pixiv アカウント必須。
        # 記事に「要望・編集依頼」を投稿する機能がある (POST dic.pixiv.net/... の ajax)。
        # TODO(endpoint): dic.pixiv.net の投稿エンドポイント (編集リクエスト等) を調査して配線。
        raise NotImplementedError(
            "pixiv endpoint not wired yet — need cookies + site probe"
        )
