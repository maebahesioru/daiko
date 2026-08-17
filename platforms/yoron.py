import logging
import os

from config import PLATFORM_COOKIES

log = logging.getLogger("daiko.platform.yoron")


class YoronPlatform:
    id = "yoron"
    label = "Youtuber世論調査"

    def _cookies(self) -> str:
        path = PLATFORM_COOKIES.get("yoron")
        if not path or not os.path.exists(path):
            raise RuntimeError("Youtuber世論調査 cookies not found (cookies_yoron.json)")
        return path

    async def post(self, sub):
        # TODO(endpoint): Youtuber世論調査 posting model is uncertain — likely
        # a survey/comment form, possibly with moderation. Probe once cookies provided.
        raise NotImplementedError(
            "yoron endpoint not wired yet — need cookies + site probe"
        )
