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
import json
import os
import re
import uuid
from datetime import datetime, timezone

from flask import Flask, render_template, request, redirect, url_for, session, flash, make_response, send_from_directory, abort

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

@app.template_filter("parse_json")
def parse_json_filter(s: str):
    """Jinja2 filter: parse JSON string, return list/dict or original."""
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return s


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
    show_poll = request.args.get("poll") == "1" and selected in ("tweet", "reply")
    thread_count = max(0, min(4, int(request.args.get("thread", "0") or 0)))
    resp = make_response(render_template("submit.html",
        is_onion=_is_onion(), selected_type=selected,
        onion_hostname=_onion_hostname, show_poll=show_poll,
        thread_count=thread_count))
    return _noindex(resp)


@app.route("/submit", methods=["POST"])
def submit():
    db = SessionLocal()
    try:
        submit_type = request.form.get("type", "tweet")
        content = request.form.get("content", "").strip()
        target_url = request.form.get("target_url", "").strip()
        like_original = 1 if request.form.get("like_original") == "1" else 0

        # --- Poll handling ---
        has_poll = request.form.get("has_poll") == "1"
        poll_choices_json = ""
        poll_duration = 0
        if has_poll and submit_type in ("tweet", "reply"):
            choices = []
            for key in sorted(request.form.keys()):
                if key.startswith("poll_choice_") and request.form.get(key, "").strip():
                    choices.append(request.form[key].strip())
            if len(choices) >= 2:
                poll_choices_json = json.dumps(choices, ensure_ascii=False)
                poll_duration = int(request.form.get("poll_duration", "1440"))
                if poll_duration < 5:
                    poll_duration = 5
                elif poll_duration > 10080:
                    poll_duration = 10080
            elif has_poll:
                flash("* 投票の選択肢を2つ以上入力してください", "error")
                return redirect(url_for("index", type=submit_type, poll="1"))

        # --- Handle file upload (skip if poll) ---
        media_files = []
        if not poll_choices_json:
            uploaded = request.files.getlist("media")
            for file in uploaded[:4]:
                if not file or not file.filename:
                    continue
                ext = os.path.splitext(file.filename)[1].lower().lstrip(".")
                if ext not in ALLOWED_EXTENSIONS:
                    continue
                if file.content_length and file.content_length > MAX_UPLOAD_SIZE:
                    continue
                fname = f"{uuid.uuid4().hex}_{file.filename}"
                file.save(os.path.join(UPLOAD_DIR, fname))
                log.info("File uploaded: %s", fname)
                media_files.append(fname)
        media_filename = json.dumps(media_files, ensure_ascii=False) if media_files else "[]"

        # Parse tweet ID from URL if provided
        target_tweet_id = _parse_tweet_id(target_url)

        # ================ Validation ================

        # * 本文が必須なタイプ（画像/動画/投票があれば本文空欄可）
        has_media_or_poll = bool(media_files) or has_poll
        if submit_type in ("tweet", "reply", "quote") and not content.strip() and not has_media_or_poll:
            flash("* 本文を入力してください（画像/動画がある場合は空欄可）", "error")
            return redirect(url_for("index", type=submit_type))

        # * 文字数制限
        if submit_type in ("tweet", "quote") and _tweet_weight(content) > _TWEET_WEIGHT_LIMIT:
            weight = _tweet_weight(content)
            flash(f"* 文字数制限を超えています（重み{weight}/上限280）", "error")
            return redirect(url_for("index", type=submit_type))

        # * 対象ツイートURLが必須なタイプ
        if submit_type in ("retweet", "reply", "quote") and not target_tweet_id:
            flash("* 対象ツイートURLを入力してください", "error")
            return redirect(url_for("index", type=submit_type))

        # * 投票がONなら選択肢が最低2つ必要
        if has_poll and submit_type in ("tweet", "reply"):
            choice_count = len([k for k in request.form.keys()
                               if k.startswith("poll_choice_") and request.form.get(k, "").strip()])
            if choice_count < 2:
                flash("* 投票の選択肢を2つ以上入力してください", "error")
                return redirect(url_for("index", type=submit_type, poll="1"))

        # For tweet/quote type, clear target URL fields (quote embeds via content)
        if submit_type in ("tweet", "quote"):
            # For quote, append the tweet URL to content for auto-embed
            if submit_type == "quote" and target_url:
                content = f"{content}\n{target_url}"
            target_url = ""
            if submit_type == "tweet":
                target_tweet_id = ""  # not needed for tweet
            # For quote, keep target_tweet_id (used for like_original)

        # --- Thread items ---
        thread_items_json = ""
        if submit_type in ("tweet", "reply", "quote"):
            thread_items = []
            for ti in range(1, 5):  # max 4 sub-tweets
                ti_content = request.form.get(f"th{ti}_content", "").strip()
                ti_has_poll = request.form.get(f"th{ti}_has_poll") == "1"
                ti_poll_choices = []
                ti_poll_choices_json = ""
                ti_poll_duration = 0
                ti_media_files = []

                if ti_has_poll:
                    for ck in sorted(request.form.keys()):
                        if ck.startswith(f"th{ti}_poll_choice_") and request.form.get(ck, "").strip():
                            ti_poll_choices.append(request.form[ck].strip())
                    if len(ti_poll_choices) >= 2:
                        ti_poll_choices_json = json.dumps(ti_poll_choices, ensure_ascii=False)
                        ti_poll_duration = int(request.form.get(f"th{ti}_poll_duration", "1440"))
                else:
                    ti_uploaded = request.files.getlist(f"th{ti}_media")
                    for file in ti_uploaded[:4]:
                        if not file or not file.filename:
                            continue
                        ext = os.path.splitext(file.filename)[1].lower().lstrip(".")
                        if ext not in ALLOWED_EXTENSIONS:
                            continue
                        if file.content_length and file.content_length > MAX_UPLOAD_SIZE:
                            continue
                        fname = f"{uuid.uuid4().hex}_{file.filename}"
                        file.save(os.path.join(UPLOAD_DIR, fname))
                        ti_media_files.append(fname)

                if ti_content or ti_media_files or ti_poll_choices_json:
                    thread_items.append({
                        "content": ti_content,
                        "media": json.dumps(ti_media_files, ensure_ascii=False),
                        "poll_choices": ti_poll_choices,
                        "poll_duration": ti_poll_duration,
                    })
            if thread_items:
                thread_items_json = json.dumps(thread_items, ensure_ascii=False)

        sub = Submission(
            submit_type=submit_type,
            content=content,
            target_tweet_url=target_url,
            target_tweet_id=target_tweet_id,
            like_original=like_original,
            media_file=media_filename,
            poll_choices=poll_choices_json,
            poll_duration=poll_duration,
            thread_items=thread_items_json,
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

        # Calculate queue position for approved items
        from config import MIN_POST_INTERVAL_SECONDS
        interval_mins = MIN_POST_INTERVAL_SECONDS // 60
        # Find last posted time
        last_posted = (
            db.query(Submission)
            .filter(Submission.status == "posted")
            .order_by(Submission.posted_at.desc())
            .first()
        )
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        # Build position map {sub_id: position} for approved items
        approved = [s for s in subs if s.status == "approved"]
        approved.sort(key=lambda s: s.reviewed_at or datetime.min.replace(tzinfo=timezone.utc))
        queue_map = {}
        for i, s in enumerate(approved):
            # Estimate: position 0 = next to post
            mins_until = interval_mins * i
            if i == 0 and last_posted and last_posted.posted_at:
                posted_at = last_posted.posted_at
                if posted_at.tzinfo is None:
                    posted_at = posted_at.replace(tzinfo=timezone.utc)
                elapsed = (now - posted_at).total_seconds()
                remaining = max(0, MIN_POST_INTERVAL_SECONDS - elapsed)
                mins_until = max(0, int(remaining // 60))
            queue_map[s.id] = (i + 1, mins_until)

        return render_template("admin.html", submissions=subs,
                               queue_map=queue_map, interval_mins=interval_mins)
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


@app.route("/admin/media/<path:filename>")
def admin_media(filename: str):
    """Serve uploaded media for admin preview (auth required)."""
    check = _admin_required()
    if check:
        return check
    # Prevent path traversal
    safe = os.path.basename(filename)
    path = os.path.join(UPLOAD_DIR, safe)
    if not os.path.exists(path):
        abort(404)
    return send_from_directory(UPLOAD_DIR, safe, as_attachment=False)


# ============================================================
# Startup
# ============================================================

if __name__ == "__main__":
    # Start the bot worker in background
    worker.start()
    log.info("Starting daiko on %s:%s", HOST, PORT)
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
