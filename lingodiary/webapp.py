#!/usr/bin/env python3
import logging
import os
import re
import traceback
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
import queue
import time
from threading import Lock, Thread

import jwt

import bcrypt
import yaml
from flask import (
    Flask,
    g,
    jsonify,
    request,
    send_file,
    send_from_directory,
)
from platformdirs import user_config_dir

from lingodiary.diary import (
    APP_NAME,
    DiaryHandler,
    TprsCreation,
    main as main_diary_tprs,
)

app = Flask(__name__)

# When running behind a reverse proxy, set TRUST_PROXY=<number of proxies> so
# request.remote_addr is the real client (used by the login throttle).  Off by
# default: trusting X-Forwarded-For without a proxy lets clients spoof their IP.
_trust_proxy = int(os.getenv("TRUST_PROXY", "0") or 0)
if _trust_proxy > 0:
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=_trust_proxy)
_secret_key = os.getenv("SECRET_KEY", "super-secret-key")
if _secret_key == "super-secret-key":
    raise RuntimeError(
        "SECRET_KEY environment variable is not set. "
        'Generate one with: python3 -c "import secrets; print(secrets.token_hex(32))"'
    )
app.secret_key = _secret_key
JWT_SECRET = _secret_key
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 24 * 7  # 7 days

# Bounded job queue — at most 2 queued jobs besides the one currently running.
_job_queue: queue.Queue = queue.Queue(maxsize=2)


class _OutputLogHandler(logging.FileHandler):
    """FileHandler that flushes after every record (mirrors DiaryHandler's setup)."""

    def emit(self, record):
        super().emit(record)
        self.flush()


def _attach_output_log(log_file: str) -> tuple:
    """Add a file handler to the lingodiary.audio_timing logger so that
    backfill progress is visible in output.log.  Returns (logger, handler)
    so the caller can remove the handler when done."""
    handler = _OutputLogHandler(log_file, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    at_logger = logging.getLogger("lingodiary.audio_timing")
    at_logger.setLevel(logging.DEBUG)
    at_logger.addHandler(handler)
    return at_logger, handler


def _detach_output_log(at_logger, handler) -> None:
    at_logger.removeHandler(handler)
    handler.close()


def _start_job_worker():
    """Single background worker that drains the job queue sequentially."""
    while True:
        fn = _job_queue.get()
        try:
            fn()
        except Exception as exc:
            app.logger.error(f"Background job failed: {exc}", exc_info=True)
        finally:
            _job_queue.task_done()


Thread(target=_start_job_worker, daemon=True).start()


@app.after_request
def add_cache_headers(response):
    """Add cache-control headers to prevent browser caching of API responses."""
    # API responses should not be cached by the browser to avoid stale data issues
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = (
            "no-cache, no-store, must-revalidate, max-age=0"
        )
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# Flutter web build directory (copied into the image at /app/web_build)
_WEB_BUILD_DIR = os.path.join(os.path.dirname(__file__), "..", "web_build")


@app.route("/")
@app.route("/<path:subpath>")
def serve_flutter_web(subpath=""):
    """Serve the Flutter web app for all non-API routes."""
    if subpath.startswith("api/"):
        return jsonify({"error": "Not found"}), 404
    full = os.path.realpath(os.path.join(_WEB_BUILD_DIR, subpath))
    if subpath and os.path.isfile(full):
        return send_from_directory(_WEB_BUILD_DIR, subpath)
    # For unknown paths (SPA routes) serve index.html so go_router handles it.
    index = os.path.join(_WEB_BUILD_DIR, "index.html")
    if os.path.isfile(index):
        return send_from_directory(_WEB_BUILD_DIR, "index.html")
    # Fallback: no web build present, return 404 with a helpful message.
    return "Flutter web build not found. Run 'flutter build web' in android_app/.", 404


USER_CONFIG_FILE = "users.yaml"
users_config_path = Path(user_config_dir(APP_NAME)) / USER_CONFIG_FILE

USER_DB_FILE = os.getenv("USER_DB_FILE", users_config_path)
CONFIG_ROOT = os.getenv(
    "CONFIG_ROOT", os.path.expanduser(Path(user_config_dir(APP_NAME)))
)


def get_mp3_variants(folder):
    variants_by_base = defaultdict(list)

    if os.path.exists(folder):
        for f in os.listdir(folder):
            if f.endswith(".mp3"):
                match = re.match(
                    r"(.+?)(_enhanced|_present|_future)?\.mp3$", f, re.IGNORECASE
                )
                if match:
                    base, variant = match.groups()
                    variant_display = (
                        variant.lstrip("_").title() if variant else "Original"
                    )
                    variants_by_base[base].append((variant_display, f))

    display_items = []
    for base, variants in variants_by_base.items():
        parts = base.split("_TPRS_")
        if len(parts) == 2:
            display = parts[1]
        else:
            display = base
        display_items.append((base, display, dict(variants)))

    display_items.sort(key=lambda x: x[1], reverse=True)
    return display_items


_SYNC_SKIP_NAMES = {"diary.json", "segments.json"}


def _is_within(path: str, root: str) -> bool:
    """True if *path* (already realpath'd) is *root* or inside it."""
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # different drives / mixed absolute+relative
        return False


def _int_arg(name: str, default: int, minimum: int = 0, maximum: int = 10_000) -> int:
    """Parse an integer query arg, clamped; falls back to *default* when invalid."""
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _parse_iso_utc(value) -> datetime | None:
    """Parse an ISO-8601 string to an aware UTC datetime, or None if invalid."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json_diary_backup(json_path: str) -> None:
    """Copy diary.json into <output>/.backup before a destructive overwrite."""
    import shutil

    if not os.path.exists(json_path):
        return
    backup_dir = Path(json_path).parent / ".backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    shutil.copy2(json_path, backup_dir / f"diary_{ts}.json")


# REST API for mobile app
# ---------------------------------------------------------------------------


def _jwt_required(f):
    """Decorator that validates a Bearer JWT token and injects user context."""

    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[len("Bearer ") :]
        elif request.endpoint == "api_sync_file" and request.args.get("token"):
            # Only for media streaming (HTML5 audio can't set headers); accepting
            # tokens in the URL elsewhere would leak them into logs and proxies.
            token = request.args["token"]
        else:
            return jsonify({"error": "Missing or invalid Authorization header"}), 401
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Token expired"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Invalid token"}), 401

        username = payload.get("sub")
        user_config_path = os.path.join(CONFIG_ROOT, username, "config.yaml")
        if not os.path.exists(user_config_path):
            return jsonify({"error": "User config not found"}), 403

        # Inject into Flask's g so route handlers can access it.
        # Load config directly (no DiaryHandler instantiation) to avoid a race
        # condition where stop() would destroy the background generation thread's
        # file log handler on every polling request.
        config = DiaryHandler.load_config(user_config_path)
        g.api_username = username
        g.api_config_path = user_config_path
        g.api_output_folder = config["output_dir"]
        g.api_tprs_folder = os.path.join(g.api_output_folder, "TPRS")
        g.api_json_diary_path = config.get("json_diary_path") or os.path.join(
            g.api_output_folder, "diary.json"
        )
        return f(*args, **kwargs)

    return decorated


_LOGIN_WINDOW_S = 300
_LOGIN_MAX_FAILURES = 10
_login_failures: dict[str, list[float]] = defaultdict(list)
_login_lock = Lock()
# Verified against when the user doesn't exist, so timing doesn't reveal usernames.
_DUMMY_HASH = bcrypt.hashpw(b"dummy-password", bcrypt.gensalt())


def _login_throttled(key: str) -> bool:
    now = time.monotonic()
    with _login_lock:
        recent = [t for t in _login_failures[key] if now - t < _LOGIN_WINDOW_S]
        _login_failures[key] = recent
        return len(recent) >= _LOGIN_MAX_FAILURES


def _login_record_failure(key: str) -> None:
    with _login_lock:
        _login_failures[key].append(time.monotonic())


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    username = data.get("username", "")
    password = data.get("password", "")

    if not isinstance(username, str) or not isinstance(password, str):
        return jsonify({"error": "username and password required"}), 400
    if not username or not password:
        return jsonify({"error": "username and password required"}), 400

    throttle_key = f"{request.remote_addr}|{username}"
    if _login_throttled(throttle_key):
        return jsonify({"error": "Too many failed attempts. Try again later."}), 429

    try:
        with open(USER_DB_FILE) as f_db:
            users = (yaml.safe_load(f_db) or {}).get("users") or {}
    except OSError:
        app.logger.error(f"Cannot read user database {USER_DB_FILE}")
        return jsonify({"error": "Server misconfigured"}), 500

    user = users.get(username)
    stored = (user or {}).get("password", "") if isinstance(user, dict) else ""
    hashed = stored.encode() if stored else _DUMMY_HASH
    try:
        password_ok = bcrypt.checkpw(password.encode(), hashed) and bool(stored)
    except ValueError:  # malformed hash in users.yaml
        password_ok = False
    if not password_ok:
        _login_record_failure(throttle_key)
        return jsonify({"error": "Invalid credentials"}), 401

    user_config_path = os.path.join(CONFIG_ROOT, username, "config.yaml")
    if not os.path.exists(user_config_path):
        return jsonify({"error": "No config found for this user"}), 403

    expiry = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS)
    token = jwt.encode(
        {"sub": username, "exp": expiry}, JWT_SECRET, algorithm=JWT_ALGORITHM
    )
    return jsonify({"token": token, "expires_at": expiry.isoformat()})


@app.route("/api/diary", methods=["GET"])
@_jwt_required
def api_get_diary():
    return (
        jsonify(
            {
                "deprecated": True,
                "message": "This endpoint is removed. Use /api/diary/json instead.",
            }
        ),
        410,
    )


@app.route("/api/diary", methods=["POST"])
@_jwt_required
def api_save_diary():
    return (
        jsonify(
            {
                "deprecated": True,
                "message": "This endpoint is removed. Use /api/diary/json instead.",
            }
        ),
        410,
    )


@app.route("/api/generate", methods=["POST"])
@_jwt_required
def api_generate():
    config_path = g.api_config_path
    output_folder = g.api_output_folder

    def _run():
        log_file = os.path.join(output_folder, "output.log")

        # Truncate log from any previous run so the client always starts fresh
        # and never mistakes an old "=== Generation finished ===" marker for the
        # current run completing immediately.
        try:
            open(log_file, "w", encoding="utf-8").close()
        except Exception:
            pass

        def _log_to_file(msg):
            try:
                with open(log_file, "a", encoding="utf-8") as lf:
                    lf.write(msg + "\n")
            except Exception:
                pass

        # Back up diary.json before anything mutates it; prune old backups.
        try:
            _json_diary_backup(os.path.join(output_folder, "diary.json"))
            cutoff = time.time() - 7 * 86400
            for old in (Path(output_folder) / ".backup").glob("diary_*.json"):
                try:
                    if old.stat().st_mtime < cutoff:
                        old.unlink()
                except OSError:
                    pass
        except Exception as exc:
            app.logger.warning(f"diary.json backup failed: {exc}")

        try:
            main_diary_tprs(config_path=config_path)
        except Exception as exc:
            app.logger.error(f"API generate error: {exc}")
            _log_to_file(f"ERROR: Generation failed: {exc}")
        json_path = os.path.join(output_folder, "diary.json")
        # 1. Audio segments + timing — run first: critical for playback.
        #    Must happen before the slow translation backfills so a
        #    container restart cannot starve the new entry of timing data.
        at_logger, at_handler = _attach_output_log(log_file)
        try:
            from lingodiary.audio_timing import backfill_audio_timings

            backfill_audio_timings(config_path=config_path, diary_json_path=json_path)
        except Exception as exc:
            tb = traceback.format_exc()
            app.logger.warning(f"Audio timing backfill failed: {exc}\n{tb}")
            _log_to_file(f"WARNING: Audio timing backfill failed: {exc}\n{tb}")
        finally:
            _detach_output_log(at_logger, at_handler)
        # 2. Q&A translations — needed for flashcard review.
        try:
            TprsCreation(config_path, generate_missing=False).backfill_qa_translations(
                json_path
            )
        except Exception as exc:
            app.logger.warning(f"Q&A translation backfill failed: {exc}")
            _log_to_file(f"WARNING: Q&A translation backfill failed: {exc}")
        # 3. sentence_input translations — low-priority metadata; runs last
        #    because it iterates ALL historical entries and can be slow.
        try:
            TprsCreation(config_path, generate_missing=False).backfill_sentence_inputs(
                json_path
            )
        except Exception as exc:
            app.logger.warning(f"sentence_input backfill failed: {exc}")
            _log_to_file(f"WARNING: sentence_input backfill failed: {exc}")
        # 4. Drive Mode Input Language audio — only if input_voice is configured.
        try:

            with open(config_path, encoding="utf-8") as _cf:
                _cfg = yaml.safe_load(_cf)
            if _cfg.get("tts", {}).get("piper_input_language", {}).get("voice"):
                from lingodiary.audio_timing import backfill_drive_mode_audio

                backfill_drive_mode_audio(
                    config_path=config_path, diary_json_path=json_path
                )
        except Exception as exc:
            tb = traceback.format_exc()
            app.logger.warning(f"Drive Mode audio backfill failed: {exc}\n{tb}")
            _log_to_file(f"WARNING: Drive Mode audio backfill failed: {exc}")
        # Write a completion marker so the client can stop polling.
        try:
            with open(log_file, "a", encoding="utf-8") as lf:
                lf.write("=== Generation finished ===\n")
        except Exception:
            pass

    try:
        _job_queue.put_nowait(_run)
    except queue.Full:
        return (
            jsonify(
                {"ok": False, "error": "A job is already queued. Try again later."}
            ),
            409,
        )
    return jsonify({"ok": True, "message": "Generation started"})


@app.route("/api/backfill/qa_translations", methods=["POST"])
@_jwt_required
def api_backfill_qa_translations():
    """Background job: translate all missing Q&A pairs to the primary language."""

    config_path = g.api_config_path
    output_folder = g.api_output_folder

    def _run():
        try:
            json_path = os.path.join(output_folder, "diary.json")
            TprsCreation(config_path, generate_missing=False).backfill_qa_translations(
                json_path
            )
        except Exception as exc:
            app.logger.error(f"Q&A translation backfill error: {exc}")

    try:
        _job_queue.put_nowait(_run)
    except queue.Full:
        return (
            jsonify(
                {"ok": False, "error": "A job is already queued. Try again later."}
            ),
            409,
        )
    return jsonify({"ok": True, "message": "Q&A translation backfill started"})


@app.route("/api/backfill/audio_timing", methods=["POST"])
@_jwt_required
def api_backfill_audio_timing():
    """Background job: generate per-sentence audio segments and write timings to diary.json.

    Accepts optional JSON body: {"overwrite": true} to force TTS re-generation for all
    days (slow).  Without the flag (default false), only recomputes timing from existing
    segment files when zero timing is detected — much faster.
    """

    config_path = g.api_config_path
    output_folder = g.api_output_folder

    body = request.get_json(silent=True) or {}
    overwrite = bool(body.get("overwrite", False))

    def _run():
        log_file = os.path.join(output_folder, "output.log")
        at_logger, at_handler = _attach_output_log(log_file)
        try:
            from lingodiary.audio_timing import backfill_audio_timings

            json_path = os.path.join(output_folder, "diary.json")
            backfill_audio_timings(
                config_path=config_path,
                diary_json_path=json_path,
                overwrite_existing=overwrite,
            )
        except Exception as exc:
            tb = traceback.format_exc()
            app.logger.error(f"Audio timing backfill error: {exc}\n{tb}")
            try:
                with open(os.path.join(output_folder, "output.log"), "a") as _lf:
                    _lf.write(f"ERROR: Audio timing backfill failed: {exc}\n{tb}\n")
            except Exception:
                pass
        finally:
            _detach_output_log(at_logger, at_handler)

    try:
        _job_queue.put_nowait(_run)
    except queue.Full:
        return (
            jsonify(
                {"ok": False, "error": "A job is already queued. Try again later."}
            ),
            409,
        )
    return jsonify({"ok": True, "message": "Audio timing backfill started"})


@app.route("/api/backfill/all", methods=["POST"])
@_jwt_required
def api_backfill_all():
    """Background job: fill in everything missing — audio segments, Q&A translations, sentence inputs.

    Runs in sequence:
      1. backfill_audio_timings      (generate missing segment MP3s + timing data — critical for playback)
      2. backfill_qa_translations    (translate missing Q&A pairs)
      3. backfill_sentence_inputs    (translate missing variant sentence_input fields — slow, runs last)
    """

    config_path = g.api_config_path
    output_folder = g.api_output_folder

    def _run():
        log_file = os.path.join(output_folder, "output.log")
        at_logger, at_handler = _attach_output_log(log_file)
        try:
            json_path = os.path.join(output_folder, "diary.json")

            app.logger.info("Backfill all: step 1/3 — audio timing / segments")
            try:
                from lingodiary.audio_timing import backfill_audio_timings

                backfill_audio_timings(
                    config_path=config_path, diary_json_path=json_path
                )
            except Exception as exc:
                app.logger.warning(f"Backfill all: audio timing failed: {exc}")

            app.logger.info("Backfill all: step 2/3 — Q&A translations")
            try:
                TprsCreation(
                    config_path, generate_missing=False
                ).backfill_qa_translations(json_path)
            except Exception as exc:
                app.logger.warning(f"Backfill all: Q&A translation failed: {exc}")

            app.logger.info(
                "Backfill all: step 3/3 — variant sentence_input translations"
            )
            try:
                TprsCreation(
                    config_path, generate_missing=False
                ).backfill_sentence_inputs(json_path)
            except Exception as exc:
                app.logger.warning(
                    f"Backfill all: sentence_input backfill failed: {exc}"
                )

            app.logger.info("Backfill all: complete")
        finally:
            _detach_output_log(at_logger, at_handler)

    try:
        _job_queue.put_nowait(_run)
    except queue.Full:
        return (
            jsonify(
                {"ok": False, "error": "A job is already queued. Try again later."}
            ),
            409,
        )
    return jsonify(
        {
            "ok": True,
            "message": "Full backfill started (Q&A translations + audio segments)",
        }
    )


@app.route("/api/backfill/drive_mode_audio", methods=["POST"])
@app.route("/api/backfill/drive_mode_audio/<date>", methods=["POST"])
@_jwt_required
def api_backfill_drive_mode_audio(date: str | None = None):
    """Background job: generate missing Input Language audio segments for Drive Mode.

    Optional URL segment ``<date>`` (YYYY-MM-DD or YYYY/MM/DD) limits processing
    to a single day.  Without it, all days are processed.

    Requires ``tts.piper_input_language.voice`` in the user config; returns 400 if absent.
    Progress is written to ``output.log`` and readable via ``GET /api/generate/status``.
    """

    config_path = g.api_config_path
    output_folder = g.api_output_folder

    # Validate that input_voice is configured before queueing the job.
    try:

        with open(config_path, encoding="utf-8") as _cf:
            _cfg = yaml.safe_load(_cf)
        if not _cfg.get("tts", {}).get("piper_input_language", {}).get("voice"):
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Drive Mode requires tts.piper_input_language.voice in config",
                    }
                ),
                400,
            )
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Config read error: {exc}"}), 500

    def _run():
        log_file = os.path.join(output_folder, "output.log")
        try:
            open(log_file, "w", encoding="utf-8").close()
        except Exception:
            pass
        at_logger, at_handler = _attach_output_log(log_file)
        try:
            from lingodiary.audio_timing import backfill_drive_mode_audio

            json_path = os.path.join(output_folder, "diary.json")
            backfill_drive_mode_audio(
                config_path=config_path,
                diary_json_path=json_path,
                date=date,
            )
        except Exception as exc:
            tb = traceback.format_exc()
            app.logger.error(f"Drive Mode audio backfill error: {exc}\n{tb}")
        finally:
            _detach_output_log(at_logger, at_handler)
        try:
            with open(log_file, "a", encoding="utf-8") as lf:
                lf.write("=== Generation finished ===\n")
        except Exception:
            pass

    try:
        _job_queue.put_nowait(_run)
    except queue.Full:
        return (
            jsonify(
                {"ok": False, "error": "A job is already queued. Try again later."}
            ),
            409,
        )
    scope = f" for {date}" if date else ""
    return jsonify({"ok": True, "message": f"Drive Mode audio backfill started{scope}"})


@app.route("/api/generate/status", methods=["GET"])
@_jwt_required
def api_generate_status():
    log_file = os.path.join(g.api_output_folder, "output.log")
    offset = _int_arg("offset", 0, 0, 2**40)
    chunk = ""
    new_offset = offset

    if os.path.exists(log_file):
        # Binary mode: offsets are byte positions, stable across calls.
        with open(log_file, "rb") as f:
            f.seek(0, os.SEEK_END)
            if offset > f.tell():  # log was truncated by a new run
                offset = 0
            f.seek(offset)
            raw = f.read()
        # Never split a multi-byte character or a half-written line.
        cut = raw.rfind(b"\n") + 1
        if cut:
            raw = raw[:cut]
        chunk = raw.decode("utf-8", errors="replace")
        new_offset = offset + len(raw)

    # "ERROR: Generation failed" is NOT terminal: the backfill steps still run
    # after it and the finish marker is always written last.
    done = "=== Generation finished ===" in chunk
    return jsonify({"log": chunk, "offset": new_offset, "done": done})


@app.route("/api/lessons", methods=["GET"])
@_jwt_required
def api_lessons():

    result = []
    diary = None
    try:
        from lingodiary.diary_json import load_diary_json

        diary = load_diary_json(g.api_json_diary_path)
    except Exception:
        diary = None

    if diary is None:
        # Fallback: no diary.json — return filesystem-scanned items with no metadata
        for base, display, variants in get_mp3_variants(g.api_tprs_folder):
            result.append({"base": base, "display": display, "variants": variants})
        return jsonify({"lessons": result})

    for day in diary.diaries:
        if not day.lesson_audio_paths:
            continue
        date_dash = day.date.replace("/", "-")
        display = f"{date_dash}_{day.title}" if day.title else date_dash
        # Build variants dict: capitalised key → bare filename (no directory)
        variants = {
            k.title(): os.path.basename(v)
            for k, v in day.lesson_audio_paths.items()
            if v
        }
        if not variants:
            continue
        # base = stem of the original MP3 (used by sync manifest matching)
        original_path = day.lesson_audio_paths.get(
            "original", next(iter(day.lesson_audio_paths.values()))
        )
        base = os.path.splitext(os.path.basename(original_path))[0]
        lesson = {
            "base": base,
            "display": display,
            "date": date_dash,
            "variants": variants,
        }
        total = len(day.entries)
        mastered = sum(
            1 for e in day.entries if e.lessons.reviewing.status == "mastered"
        )
        learning = sum(
            1 for e in day.entries if e.lessons.reviewing.status == "learning"
        )
        lesson["srs"] = {
            "total": total,
            "mastered": mastered,
            "learning": learning,
            "new": total - mastered - learning,
        }
        result.append(lesson)

    result.sort(key=lambda x: x["date"], reverse=True)
    return jsonify({"lessons": result})


@app.route("/api/sync/manifest", methods=["GET"])
@_jwt_required
def api_sync_manifest():
    """Return a manifest of all syncable files (mp3 + md) with size and mtime."""

    output_folder = g.api_output_folder
    manifest = []
    for root, _, files in os.walk(output_folder):
        for fname in files:
            if (
                fname.startswith(".")
                or fname.endswith(".zip")
                or fname.endswith(".log")
                or fname in _SYNC_SKIP_NAMES
            ):
                continue
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, output_folder)
            stat = os.stat(full)
            manifest.append(
                {
                    "path": rel,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                }
            )
    return jsonify({"manifest": manifest})


@app.route("/api/sync/file/<path:rel_path>", methods=["GET"])
@_jwt_required
def api_sync_file(rel_path):
    """Download a single output file by its relative path."""

    output_folder = g.api_output_folder
    # Security: resolve and ensure the path stays within output_folder
    safe_path = os.path.realpath(os.path.join(output_folder, rel_path))
    if not _is_within(safe_path, os.path.realpath(output_folder)):
        return jsonify({"error": "Access denied"}), 403
    if not os.path.isfile(safe_path):
        return jsonify({"error": "File not found"}), 404
    return send_file(safe_path, as_attachment=True)


def _audio_paths_of(v_obj) -> list[str]:
    """All audio file paths referenced by one SentenceBlock (incl. Drive Mode)."""
    paths = [v_obj.sentence_audio_path, v_obj.sentence_input_language_audio_path]
    for qa in v_obj.qa:
        paths += [
            qa.question_audio_path,
            qa.question_input_language_audio_path,
            qa.answer_audio_path,
            qa.answer_input_language_audio_path,
        ]
    return [p for p in paths if p]


@app.route("/api/sync/manifest/<path:base>", methods=["GET"])
@_jwt_required
def api_sync_lesson_manifest(base):
    """Manifest filtered to files belonging to a single lesson (by base name),
    including all primary variants and nested Drive Mode assets.
    """
    from lingodiary.diary_json import VARIANTS, load_diary_json

    output_folder = g.api_output_folder
    root_real = os.path.realpath(output_folder)

    # 1. Files whose relative path contains the lesson base name.
    matched_paths = set()
    for root, _, files in os.walk(output_folder):
        for fname in files:
            if fname.startswith(".") or fname in _SYNC_SKIP_NAMES:
                continue
            if fname.endswith((".zip", ".log")):
                continue
            rel = os.path.relpath(os.path.join(root, fname), output_folder)
            if base in rel:
                matched_paths.add(rel)

    # 2. Segment files referenced from diary.json for the matching day.
    try:
        diary = load_diary_json(g.api_json_diary_path)
        for day in diary.diaries:
            original_path = day.lesson_audio_paths.get("original", "")
            day_base = (
                os.path.splitext(os.path.basename(original_path))[0]
                if original_path
                else ""
            )
            if not day_base or base not in day_base:
                continue
            for entry in day.entries:
                for variant_name in VARIANTS:
                    matched_paths.update(
                        _audio_paths_of(entry.lessons.get_variant(variant_name))
                    )
    except Exception as exc:
        app.logger.warning(f"Failed parsing diary.json inside manifest route: {exc}")

    manifest = []
    for rel_path in sorted(matched_paths):
        full_path = os.path.realpath(os.path.join(output_folder, rel_path))
        # diary.json is user-writable data: never follow paths out of the output dir
        if not _is_within(full_path, root_real) or not os.path.isfile(full_path):
            continue
        stat = os.stat(full_path)
        manifest.append(
            {"path": rel_path, "size": stat.st_size, "mtime": stat.st_mtime}
        )

    return jsonify({"manifest": manifest})


@app.route("/api/diary/json", methods=["GET"])
@_jwt_required
def api_get_diary_json():
    """Return the full diary JSON for the authenticated user."""
    from lingodiary.diary_json import load_diary_json

    json_path = g.api_json_diary_path
    if not os.path.exists(json_path):
        return jsonify({"diaries": []})
    try:
        diary = load_diary_json(json_path)
        return jsonify(diary.to_dict())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/diary/day/<date>", methods=["GET"])
@_jwt_required
def api_diary_day(date):
    """Return the complete Day object for a given date (all variants, all entries, title, etc.)."""
    from lingodiary.diary_json import load_diary_json, get_day

    date_slash = date.replace("-", "/")
    try:
        diary = load_diary_json(g.api_json_diary_path)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    day = get_day(diary, date_slash)
    if day is None:
        return jsonify({"error": "Date not found"}), 404

    return jsonify(day.to_dict())


@app.route("/api/diary/json", methods=["POST"])
@_jwt_required
def api_post_diary_json():
    """Overwrite diary JSON (full replace, for migration tool)."""
    from lingodiary.diary_json import DiaryJson, diary_lock, save_diary_json

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("diaries"), list):
        return jsonify({"error": "Body must be an object with a 'diaries' list"}), 400
    try:
        diary = DiaryJson.from_dict(data)
        with diary_lock(g.api_json_diary_path):
            _json_diary_backup(g.api_json_diary_path)
            save_diary_json(diary, g.api_json_diary_path)
        return jsonify({"ok": True})
    except Exception as exc:
        app.logger.error(f"api_post_diary_json error: {exc}")
        return jsonify({"error": "Could not save diary"}), 400


@app.route("/api/lessons/entries/<date>/<variant>", methods=["GET"])
@_jwt_required
def api_lesson_entries(date, variant):
    """Return entries list with audio timings for a given date and variant."""
    from lingodiary.diary_json import load_diary_json, get_day

    from lingodiary.diary_json import VARIANTS

    if variant not in VARIANTS:
        return jsonify({"error": f"variant must be one of {list(VARIANTS)}"}), 400

    # Normalise date to YYYY/MM/DD
    date_slash = date.replace("-", "/")
    try:
        diary = load_diary_json(g.api_json_diary_path)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    day = get_day(diary, date_slash)
    if day is None:
        return jsonify({"date": date_slash, "variant": variant, "entries": []})

    entries_out = []
    for entry in day.entries:
        v_obj = entry.lessons.get_variant(variant)
        if not v_obj or not v_obj.sentence:
            continue
        entries_out.append(
            {
                "index": entry.index,
                "input_language_sentence": entry.input_language_sentence,
                "output_language_translation": entry.output_language_translation,
                "sentence": v_obj.sentence,
                "sentence_input": v_obj.sentence_input,
                "sentence_audio_path": v_obj.sentence_audio_path,
                "sentence_input_language_audio_path": v_obj.sentence_input_language_audio_path,
                "audio_timing": v_obj.audio_timing.to_dict(),
                "qa": [q.to_dict() for q in v_obj.qa],
                "reviewing": entry.lessons.reviewing.to_dict(),
            }
        )

    return jsonify({"date": date_slash, "variant": variant, "entries": entries_out})


@app.route("/api/lessons/score", methods=["POST"])
@_jwt_required
def api_lesson_score():
    """Apply SM-2 score to a diary entry.

    Body: {date, entry_index, score, scored_at?}.  ``scored_at`` (ISO-8601) lets
    the app replay offline reviews with the time they actually happened.
    """
    from lingodiary.diary_json import (
        diary_lock,
        load_diary_json,
        save_diary_json,
        update_srs,
    )

    data = request.get_json(silent=True) or {}
    date = data.get("date", "")
    entry_index = data.get("entry_index")
    score = data.get("score")

    if not date or entry_index is None or score is None:
        return jsonify({"error": "date, entry_index, and score are required"}), 400
    if isinstance(score, bool) or score not in (0, 2, 3, 5):
        return jsonify({"error": "score must be one of 0, 2, 3, 5"}), 400
    try:
        entry_index = int(entry_index)
    except (TypeError, ValueError):
        return jsonify({"error": "entry_index must be an integer"}), 400

    scored_at = None
    if data.get("scored_at"):
        scored_at = _parse_iso_utc(data["scored_at"])
        if scored_at is None:
            return jsonify({"error": "scored_at must be an ISO-8601 timestamp"}), 400
        if scored_at > datetime.now(timezone.utc):
            scored_at = None  # clock skew: never schedule from the future

    date_slash = str(date).replace("-", "/")
    try:
        with diary_lock(g.api_json_diary_path):
            diary = load_diary_json(g.api_json_diary_path)
            diary, reviewing = update_srs(
                diary, date_slash, entry_index, int(score), now=scored_at
            )
            save_diary_json(diary, g.api_json_diary_path)
        return jsonify({"ok": True, "reviewing": reviewing.to_dict()})
    except ValueError:
        return jsonify({"error": "Entry not found"}), 404
    except Exception as exc:
        app.logger.error(f"api_lesson_score error: {exc}")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/home", methods=["GET"])
@_jwt_required
def api_home():
    """Dashboard: stats + recent + recommended lesson."""
    from lingodiary.diary_json import (
        load_diary_json,
        compute_stats,
        get_due_entries,
    )

    try:
        diary = load_diary_json(g.api_json_diary_path)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    stats = compute_stats(diary)

    # Get recently studied lessons (by lesson last_reviewed, not entry last_reviewed)
    recently_studied = [day for day in diary.diaries if day.last_reviewed is not None]
    recently_studied.sort(key=lambda d: d.last_reviewed, reverse=True)

    recent_lessons = []
    for day in recently_studied:
        recent_lessons.append(
            {
                "date": day.date,
                "title": day.title,
                "last_reviewed": day.last_reviewed,
                "entry_count": len(day.entries),
            }
        )

    recommended = None
    due = get_due_entries(diary)
    if due:
        day, entry = due[0]
        recommended = {
            "date": day.date,
            "title": day.title,
            "entry_index": entry.index,
            "variant": "original",
            "reason": "due_for_review",
        }
    else:
        # Fall back to first new entry
        for day in diary.diaries:
            for entry in day.entries:
                if entry.lessons.reviewing.status == "new":
                    recommended = {
                        "date": day.date,
                        "title": day.title,
                        "entry_index": entry.index,
                        "variant": "original",
                        "reason": "new",
                    }
                    break
            if recommended:
                break

    return jsonify(
        {"stats": stats, "recent_lessons": recent_lessons, "recommended": recommended}
    )


@app.route("/api/sentences/due", methods=["GET"])
@_jwt_required
def api_sentences_due():
    """Return due (or new) sentences for Anki-style review.

    Query params:
      limit  (int, default 20)  — max sentences to return
      variant (str, default "original") — which variant lesson to pull sentence/qa from
    """
    from lingodiary.diary_json import load_diary_json, get_due_entries

    from lingodiary.diary_json import VARIANTS

    limit = _int_arg("limit", 20, 1, 500)
    variant = request.args.get("variant", "original")
    if variant not in VARIANTS:
        return jsonify({"error": f"variant must be one of {list(VARIANTS)}"}), 400

    try:
        diary = load_diary_json(g.api_json_diary_path)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    due_pairs = get_due_entries(diary)[:limit]
    sentences = []
    for day, entry in due_pairs:
        vl = entry.lessons.get_variant(variant)
        sentences.append(
            {
                "date": day.date,
                "title": day.title,
                "entry_index": entry.index,
                "variant": variant,
                "input_language_sentence": entry.input_language_sentence,
                "output_language_translation": entry.output_language_translation,
                "tips": entry.tips,
                "sentence": vl.sentence,
                "sentence_audio_path": vl.sentence_audio_path,
                "audio_timing": vl.audio_timing.to_dict(),
                "qa": [qa.to_dict() for qa in vl.qa],
                "reviewing": entry.lessons.reviewing.to_dict(),
            }
        )
    return jsonify({"sentences": sentences, "total": len(sentences)})


@app.route("/api/diary/entry", methods=["POST"])
@_jwt_required
def api_add_diary_entry():
    """Add sentences to a day: {date: 'YYYY-MM-DD', sentences: [...]}.

    Never deletes or renumbers existing sentences.  Sentences already present on
    that day (same text) are skipped, so replaying an offline request is safe.
    """
    from lingodiary.diary_json import (
        ReviewingState,
        Sentence,
        VariantSet,
        diary_lock,
        get_day,
        load_diary_json,
        save_diary_json,
        upsert_day,
    )

    data = request.get_json(silent=True) or {}
    selected_date = data.get("date", "")
    sentences = data.get("sentences", [])

    if not selected_date or not sentences:
        return jsonify({"error": "date and sentences are required"}), 400
    if not isinstance(sentences, list) or not all(
        isinstance(s, str) for s in sentences
    ):
        return jsonify({"error": "sentences must be a list of strings"}), 400
    sentences = [s.strip() for s in sentences if s.strip()]
    if not sentences:
        return jsonify({"error": "date and sentences are required"}), 400

    try:
        datetime.strptime(selected_date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Invalid date format, expected YYYY-MM-DD"}), 400

    json_path = g.api_json_diary_path
    if not json_path:
        return jsonify({"error": "json_diary_path not configured"}), 500

    try:
        date_slash = selected_date.replace("-", "/")
        with diary_lock(json_path):
            diary_json = load_diary_json(json_path)
            day = get_day(diary_json, date_slash)
            day_existed = day is not None
            known = (
                {e.input_language_sentence.strip() for e in day.entries}
                if day
                else set()
            )
            next_index = (
                max((e.index for e in day.entries), default=0) + 1 if day else 1
            )

            new_entries = []
            for text in sentences:
                if text in known:
                    continue
                known.add(text)
                new_entries.append(
                    Sentence(
                        index=next_index,
                        input_language_sentence=text,
                        lessons=VariantSet(reviewing=ReviewingState()),
                    )
                )
                next_index += 1

            if new_entries:
                diary_json = upsert_day(diary_json, date_slash, "", new_entries)
                save_diary_json(diary_json, json_path)
    except Exception as exc:
        app.logger.error(f"api_add_diary_entry error: {exc}")
        return jsonify({"error": str(exc)}), 500

    warnings = []
    if day_existed and new_entries:
        warnings.append(
            f"Day {selected_date} already existed — new sentences were appended. "
            "Audio will need to be regenerated."
        )
    skipped = len(sentences) - len(new_entries)
    return jsonify(
        {
            "success": True,
            "date": selected_date,
            "sentences_added": len(new_entries),
            "sentences_skipped": skipped,
            "warnings": warnings,
        }
    )


@app.route("/api/diary/<date>/trials", methods=["POST"])
@_jwt_required
def api_save_trials(date):
    """Save user_trial_translation for each sentence of a day.
    Body: {"trials": ["attempt1", "attempt2", ...]} in the day's display order.
    """
    from lingodiary.diary_json import (
        diary_lock,
        get_day,
        load_diary_json,
        save_diary_json,
    )

    data = request.get_json(silent=True) or {}
    trials = data.get("trials", [])
    if not trials or not isinstance(trials, list):
        return jsonify({"error": "trials list is required"}), 400
    if not all(isinstance(t, str) for t in trials):
        return jsonify({"error": "trials must be a list of strings"}), 400

    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Invalid date format, expected YYYY-MM-DD"}), 400

    json_path = g.api_json_diary_path
    if not json_path:
        return jsonify({"error": "json_diary_path not configured"}), 500

    try:
        with diary_lock(json_path):
            diary_json = load_diary_json(json_path)
            day = get_day(diary_json, date.replace("-", "/"))
            if day is None:
                return jsonify({"error": f"No diary entry for {date}"}), 404
            for entry, trial in zip(day.entries, trials):
                entry.user_trial_translation = trial
            save_diary_json(diary_json, json_path)
    except Exception as exc:
        app.logger.error(f"api_save_trials error: {exc}")
        return jsonify({"error": str(exc)}), 500

    return jsonify({"success": True, "date": date, "trials_saved": len(trials)})


@app.route("/api/config", methods=["GET"])
@_jwt_required
def api_get_config():
    """Return user config values relevant to the mobile app."""

    config_path = g.api_config_path
    try:
        with open(config_path, encoding="utf-8") as _cf:
            cfg = yaml.safe_load(_cf)
    except Exception:
        cfg = {}

    cfg = cfg or {}
    kw = cfg.get("tprs_keywords") or {}
    tts_cfg = cfg.get("tts", {})
    drive_mode_available = bool(
        tts_cfg.get("piper_input_language", {}).get("voice", "")
    )
    pause_ms = int(tts_cfg.get("pause_between_sentences_duration", 600))

    return jsonify(
        {
            "tprs": {
                "sentence": kw.get("sentence", "SETNING:"),
                "question": kw.get("question", "SPØRSMÅL:"),
                "answer": kw.get("answer", "SVAR:"),
            },
            "drive_mode_available": drive_mode_available,
            "pause_between_sentences_duration": pause_ms,
        }
    )


@app.route("/api/lessons/last_reviewed/<date>", methods=["GET"])
@_jwt_required
def api_get_lesson_last_reviewed(date):
    """Get the last_reviewed timestamp for a specific lesson date."""
    from lingodiary.diary_json import load_diary_json, get_day

    # Normalize date to YYYY/MM/DD
    date_slash = date.replace("-", "/")

    try:
        diary = load_diary_json(g.api_json_diary_path)
        day = get_day(diary, date_slash)
        if day is None:
            return jsonify({"error": "Lesson not found"}), 404

        return jsonify({"date": date_slash, "last_reviewed": day.last_reviewed})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/lessons/last_reviewed/<date>", methods=["PUT"])
@_jwt_required
def api_update_lesson_last_reviewed(date):
    """Update the last_reviewed timestamp for a specific lesson date."""
    from lingodiary.diary_json import (
        diary_lock,
        get_day,
        load_diary_json,
        save_diary_json,
    )

    date_slash = date.replace("-", "/")
    data = request.get_json(silent=True) or {}

    if data.get("timestamp"):
        parsed = _parse_iso_utc(data["timestamp"])
        if parsed is None:
            return jsonify({"error": "timestamp must be an ISO-8601 string"}), 400
    else:
        parsed = datetime.now(timezone.utc)
    # Normalised to UTC so string sorting of last_reviewed is chronological.
    timestamp = parsed.isoformat()

    try:
        with diary_lock(g.api_json_diary_path):
            diary = load_diary_json(g.api_json_diary_path)
            day = get_day(diary, date_slash)
            if day is None:
                return jsonify({"error": "Lesson not found"}), 404
            # Keep the newest value if an older offline replay arrives late.
            if not day.last_reviewed or timestamp > day.last_reviewed:
                day.last_reviewed = timestamp
            save_diary_json(diary, g.api_json_diary_path)
            current = day.last_reviewed

        return jsonify({"date": date_slash, "last_reviewed": current, "success": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/lessons/recently_studied", methods=["GET"])
@_jwt_required
def api_recently_studied_lessons():
    """Get the 10 most recently studied lessons (by last_reviewed timestamp)."""
    from lingodiary.diary_json import load_diary_json

    limit = _int_arg("limit", 10, 1, 200)

    try:
        diary = load_diary_json(g.api_json_diary_path)

        # Filter days that have been reviewed and sort by last_reviewed desc
        reviewed_days = [day for day in diary.diaries if day.last_reviewed is not None]
        reviewed_days.sort(key=lambda d: d.last_reviewed, reverse=True)

        # Take the most recent ones
        recent = reviewed_days[:limit]

        result = []
        for day in recent:
            result.append(
                {
                    "date": day.date,
                    "title": day.title,
                    "last_reviewed": day.last_reviewed,
                    "entry_count": len(day.entries),
                }
            )

        return jsonify({"lessons": result, "total": len(result)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def main():
    # The Werkzeug debugger allows remote code execution: opt-in only (FLASK_DEBUG=1).
    debug = os.getenv("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
    app.run(debug=debug, use_reloader=False, host="0.0.0.0", port=8084)


if __name__ == "__main__":
    main()
