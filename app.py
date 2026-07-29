"""
Flask web app — anonymous Twitter proxy bot with admin review queue.

Routes:
  GET  /              — submission form
  POST /submit        — submit a tweet
  GET  /status/<id>   — check submission status
  GET  /admin         — admin login
  POST /admin         — admin login
  GET  /admin/queue   — review queue (approved/rejected/pending all visible)
  POST /admin/approve/<id>
  POST /admin/reject/<id>
  POST /admin/reset/<id>   — move failed back to pending
"""
import logging
import os
import re
import uuid
from datetime import datetime, timezone

from flask import Flask, render_template, request, redirect, url_for, session, flash, make_response

from config import SECRET_KEY, ADMIN_PASSWORD, HOST, PORT, UPLOAD_DIR, ALLOWED_EXTENSIONS, MAX_UPLOAD_SIZE
from models import init_db, SessionLocal, Submission
from bot_worker import worker

# --- Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger("daiko.app")

# DISABLE werkzeug access log (prevents IP address logging)
logging.getLogger("werkzeug").disabled = True

init_db()

app = Flask(__name__)
app.secret_key = SECRET_KEY

# --- Onion hostname (read once at startup) ---
_onion_hostname = ""
try:
    _hp = os.path.join(os.path.dirname(__file__), "..", "var", "lib", "tor", "hidden_service", "hostname")
    # The file is at /var/lib/tor/hidden_service/hostname inside the container
    for _p in ("/var/lib/tor/hidden_service/hostname",):
        if os.path.exists(_p):
            with open(_p) as _f:
                _onion_hostname = _f.read().strip()
            break
except Exception:
    pass


# --- Jinja2 filter: linkify URLs in text ---
import re as _re
_URL_RE = _re.compile(r'(https?://[^\s<>"]+)')

_TWEET_WEIGHT_LIMIT = 280

def _tweet_weight(text: str) -> int:
    """Count tweet weight: CJK/fullwidth = 2, other = 1. Twitter's actual algo."""
    total = 0
    for ch in text:
        cp = ord(ch)
        # Full-width / CJK ranges (rough approximation matching Twitter's weight=2 chars)
        if (0x1100 <= cp <= 0x115F or   # Hangul Jamo
            0x2E80 <= cp <= 0x303F or   # CJK Radicals / Kana / Punctuation
            0x3040 <= cp <= 0x33BF or   # Hiragana, Katakana, Bopomofo, Hangul, CJK Compatibility
            0x3400 <= cp <= 0x4DBF or   # CJK Unified Ext-A
            0x4E00 <= cp <= 0x9FFF or   # CJK Unified Ideographs
            0xAC00 <= cp <= 0xD7AF or   # Hangul Syllables
            0xF900 <= cp <= 0xFAFF or   # CJK Compatibility Ideographs
            0xFF01 <= cp <= 0xFF60 or   # Fullwidth forms
            0xFFE0 <= cp <= 0xFFE6 or   # Fullwidth signs
            0x1F200 <= cp <= 0x1F2FF or # Enclosed Ideographic Supplement
            0x1F300 <= cp <= 0x1F5FF or # Misc Symbols & Pictographs (emoji)
            0x1F600 <= cp <= 0x1F9FF or # Emoticons
            0x20000 <= cp <= 0x2FFFF):  # CJK Ext B+
            total += 2
        else:
            total += 1
    return total

@app.template_filter("linkify")
def linkify(text: str) -> str:
    """Escape HTML, then convert URLs to clickable links."""
    from markupsafe import Markup, escape
    escaped = escape(text)  # escape HTML first
    def _replace(m):
        url = m.group(1)
        return f'<a href="{url}" rel="nofollow noopener">{url}</a>'
    return Markup(_URL_RE.sub(_replace, escaped))


# ============================================================
# Helpers
# ============================================================

def _parse_tweet_id(url: str) -> str:
    """Extract tweet ID from URL. Only accepts https://x.com/ or https://twitter.com/ URLs."""
    if not url:
        return ""
    # Block non-https and non-x.com/twitter.com URLs (prevents javascript:, data:, etc.)
    url_lower = url.lower()
    if not (url_lower.startswith("https://x.com/") or url_lower.startswith("https://twitter.com/")):
        return ""
    m = re.search(r"/status(?:es)?/(\d+)", url)
    return m.group(1) if m else ""

def _admin_required():
    """Check if logged in as admin; None = ok, else redirect."""
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    return None

def _is_onion():
    """Check if the request came via Tor hidden service (.onion host)."""
    host = request.host.lower()
    return host.endswith(".onion")

def _noindex(response):
    """Add noindex headers to prevent search engine indexing."""
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


# ============================================================
# robots.txt — block all crawlers
# ============================================================

@app.route("/robots.txt")
def robots():
    return "User-agent: *\nDisallow: /\n", 200, {"Content-Type": "text/plain"}


# ============================================================
# Public routes (onion-facing)
# ============================================================

@app.route("/")
def index():
    selected = request.args.get("type", "tweet")
    if selected not in ("tweet", "retweet", "reply", "quote"):
        selected = "tweet"
    resp = make_response(render_template("submit.html", is_onion=_is_onion(), selected_type=selected, onion_hostname=_onion_hostname))
    return _noindex(resp)


@app.route("/submit", methods=["POST"])
def submit():
    db = SessionLocal()
    try:
        submit_type = request.form.get("type", "tweet")
        content = request.form.get("content", "").strip()
        target_url = request.form.get("target_url", "").strip()
        like_original = 1 if request.form.get("like_original") == "1" else 0

        # --- Handle file upload ---
        media_filename = ""
        file = request.files.get("media")
        if file and file.filename:
            ext = os.path.splitext(file.filename)[1].lower().lstrip(".")
            if ext not in ALLOWED_EXTENSIONS:
                flash(f"未対応のファイル形式です: .{ext} (対応: {', '.join(sorted(ALLOWED_EXTENSIONS))})", "error")
                return redirect(url_for("index"))
            if file.content_length and file.content_length > MAX_UPLOAD_SIZE:
                flash(f"ファイルが大きすぎます (最大 {MAX_UPLOAD_SIZE // 1024 // 1024}MB)", "error")
                return redirect(url_for("index"))
            # Save with unique name to prevent collisions
            media_filename = f"{uuid.uuid4().hex}_{file.filename}"
            file.save(os.path.join(UPLOAD_DIR, media_filename))
            log.info("File uploaded: %s", media_filename)

        # Parse tweet ID from URL if provided
        target_tweet_id = _parse_tweet_id(target_url)

        # Validation — content can be empty if media is attached
        has_content = bool(content) or bool(media_filename)
        if submit_type in ("tweet", "reply", "quote") and not has_content:
            flash("投稿内容または画像/動画が必要です", "error")
            return redirect(url_for("index", type=submit_type))

        if submit_type in ("retweet", "reply", "quote") and not target_tweet_id:
            flash("有効なツイートURLを入力してください", "error")
            return redirect(url_for("index", type=submit_type))

        if submit_type in ("tweet", "quote") and _tweet_weight(content) > _TWEET_WEIGHT_LIMIT:
            weight = _tweet_weight(content)
            flash(f"文字数制限を超えています（現在: 重み{weight}/上限280）全角は2・半角は1でカウント", "error")
            return redirect(url_for("index", type=submit_type))

        # For tweet/quote type, clear target URL fields (quote embeds via content)
        if submit_type in ("tweet", "quote"):
            # For quote, append the tweet URL to content for auto-embed
            if submit_type == "quote" and target_url:
                content = f"{content}\n{target_url}"
            target_url = ""
            target_tweet_id = ""

        sub = Submission(
            submit_type=submit_type,
            content=content,
            target_tweet_url=target_url,
            target_tweet_id=target_tweet_id,
            like_original=like_original,
            media_file=media_filename,
            status="pending",
        )
        db.add(sub)
        db.commit()
        sub_id = sub.id

        log.info("New submission #%d type=%s", sub_id, submit_type)
        resp = make_response(render_template("submitted.html", submission_id=sub_id))
        return _noindex(resp)

    except Exception as e:
        db.rollback()
        log.exception("Submit error")
        flash("送信エラーが発生しました", "error")
        return redirect(url_for("index"))
    finally:
        db.close()


@app.route("/status/<int:sub_id>")
def check_status(sub_id: int):
    db = SessionLocal()
    try:
        sub = db.query(Submission).filter(Submission.id == sub_id).first()
        if not sub:
            resp = make_response(render_template("status.html", found=False))
        else:
            resp = make_response(render_template("status.html", found=True, submission=sub))
        return _noindex(resp)
    finally:
        db.close()


# ============================================================
# Admin routes
# ============================================================

@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("admin_queue"))
        flash("パスワードが違います", "error")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("admin_login"))


@app.route("/admin/queue")
def admin_queue():
    check = _admin_required()
    if check:
        return check

    db = SessionLocal()
    try:
        # Show all non-deleted, newest first
        subs = (
            db.query(Submission)
            .order_by(Submission.submitted_at.desc())
            .limit(200)
            .all()
        )
        return render_template("admin.html", submissions=subs)
    finally:
        db.close()


@app.route("/admin/approve/<int:sub_id>", methods=["POST"])
def admin_approve(sub_id: int):
    check = _admin_required()
    if check:
        return check

    db = SessionLocal()
    try:
        sub = db.query(Submission).filter(Submission.id == sub_id).first()
        if not sub:
            flash("見つかりません", "error")
            return redirect(url_for("admin_queue"))

        sub.status = "approved"
        sub.reviewed_at = datetime.now(timezone.utc)
        db.commit()
        log.info("Submission #%d APPROVED", sub_id)
        flash(f"#{sub_id} 承認しました", "success")
    finally:
        db.close()
    return redirect(url_for("admin_queue"))


@app.route("/admin/reject/<int:sub_id>", methods=["POST"])
def admin_reject(sub_id: int):
    check = _admin_required()
    if check:
        return check

    db = SessionLocal()
    try:
        sub = db.query(Submission).filter(Submission.id == sub_id).first()
        if not sub:
            flash("見つかりません", "error")
            return redirect(url_for("admin_queue"))

        sub.status = "rejected"
        sub.reviewed_at = datetime.now(timezone.utc)
        sub.admin_note = request.form.get("note", "")
        db.commit()
        log.info("Submission #%d REJECTED: %s", sub_id, sub.admin_note)
        flash(f"#{sub_id} 却下しました", "success")
    finally:
        db.close()
    return redirect(url_for("admin_queue"))


@app.route("/admin/reset/<int:sub_id>", methods=["POST"])
def admin_reset(sub_id: int):
    """Move a failed submission back to pending for retry."""
    check = _admin_required()
    if check:
        return check

    db = SessionLocal()
    try:
        sub = db.query(Submission).filter(Submission.id == sub_id).first()
        if not sub:
            flash("見つかりません", "error")
            return redirect(url_for("admin_queue"))

        sub.status = "pending"
        sub.reviewed_at = None
        sub.error_message = ""
        db.commit()
        log.info("Submission #%d RESET to pending", sub_id)
        flash(f"#{sub_id} 再審査待ちに戻しました", "success")
    finally:
        db.close()
    return redirect(url_for("admin_queue"))


# ============================================================
# Startup
# ============================================================

if __name__ == "__main__":
    # Start the bot worker in background
    worker.start()
    log.info("Starting daiko on %s:%s", HOST, PORT)
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
