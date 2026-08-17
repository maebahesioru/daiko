"""
Platform registry — multi-site proxy posting.

Each platform adapter implements a common interface so bot_worker and app.py
treat every destination uniformly. Twitter is the reference implementation.

Adapter interface (async):
    post(self, sub: Submission) -> dict
        returns {"tweet_id": ..., "tweet_url": ...}
    validate(self, form: dict, files: list) -> tuple[error: str|None, data: dict]
        called at submit time; return (error_message, cleaned_data)
"""

import logging

log = logging.getLogger("daiko.platform")

# platform_id -> adapter instance (lazy import to keep deps optional)
_REGISTRY = {}


def register(platform_id: str, adapter, *, load_error: str | None = None):
    _REGISTRY[platform_id] = {
        "adapter": adapter,
        "load_error": load_error,
    }


def get_adapter(platform_id: str):
    entry = _REGISTRY.get(platform_id)
    if entry is None:
        return None
    if entry["load_error"]:
        raise RuntimeError(f"Platform {platform_id} unavailable: {entry['load_error']}")
    return entry["adapter"]


def available_platforms():
    return sorted(_REGISTRY.keys())


# --- Register built-in platforms ---
def _load_platforms():
    # Twitter — required
    from platforms.twitter import TwitterPlatform
    register("twitter", TwitterPlatform())

    # Optional platforms — load errors are captured so app still starts w/o extras
    def _try(pid, module, cls):
        try:
            mod = __import__(f"platforms.{module}", fromlist=[cls])
            adapter = getattr(mod, cls)()
            register(pid, adapter)
            log.info("Platform %s loaded", pid)
        except Exception as e:
            register(pid, None, load_error=str(e))
            log.warning("Platform %s failed to load: %s", pid, e)

    for pid, module, cls in (
        ("sukikirai", "sukikirai", "SukikiraiPlatform"),
        ("yoron", "yoron", "TuberPlatform"),
        ("nico", "nico", "NicoPlatform"),
        ("pixiv", "pixiv", "PixivPlatform"),
    ):
        _try(pid, module, cls)


_load_platforms()
