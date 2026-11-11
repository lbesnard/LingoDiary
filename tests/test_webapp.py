"""Flask route tests for lingodiary/webapp.py.

Strategy
--------
- SECRET_KEY is set via os.environ *before* importing webapp so the module-level
  RuntimeError is not raised.
- Heavy native deps (ovos_plugin_manager, piper, etc.) are stubbed in sys.modules
  before import so the test process doesn't need them installed.
- USER_DB_FILE and CONFIG_ROOT are patched at the module level after import so
  login and JWT-protected routes can use temporary directories.
- A valid JWT is minted with the same secret used by the app to authenticate
  protected routes without hitting the real user database.
- diary_json functions (load/save/upsert) are mocked to avoid real filesystem I/O.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

# ── Stub heavy/optional native modules before any lingodiary import ────────────
for _mod in (
    "ovos_plugin_manager",
    "ovos_plugin_manager.tts",
    "ovos_tts_plugin_piper",
    "piper",
    "piper.voice",
):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import io  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest.mock import patch  # noqa: E402

import bcrypt  # noqa: E402
import jwt  # noqa: E402
import pytest  # noqa: E402
import yaml  # noqa: E402

# ── Set SECRET_KEY before importing webapp (module raises if missing) ──────────
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest-only")

from lingodiary import webapp  # noqa: E402  (must follow env var set)
from lingodiary.webapp import JWT_ALGORITHM, JWT_SECRET, app  # noqa: E402

# ── Helpers ────────────────────────────────────────────────────────────────────

TEST_USERNAME = "testuser"
TEST_PASSWORD = "testpass"
_HASHED_PW = bcrypt.hashpw(TEST_PASSWORD.encode(), bcrypt.gensalt()).decode()

USERS_YAML = yaml.dump({"users": {TEST_USERNAME: {"password": _HASHED_PW}}})


def _mint_token(username: str = TEST_USERNAME, expired: bool = False) -> str:
    """Return a signed JWT for use in Authorization headers."""
    delta = timedelta(hours=-1) if expired else timedelta(hours=24)
    payload = {"sub": username, "exp": datetime.utcnow() + delta}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


@pytest.fixture()
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture()
def tmp_user_dir(tmp_path):
    """Create a minimal user config tree under tmp_path and patch webapp globals."""
    user_dir = tmp_path / TEST_USERNAME
    user_dir.mkdir()
    config = {
        "output_dir": str(tmp_path / "output"),
        "json_diary_path": str(tmp_path / "diary.json"),
    }
    (user_dir / "config.yaml").write_text(yaml.dump(config))
    (tmp_path / "output").mkdir()

    users_yaml_path = tmp_path / "users.yaml"
    users_yaml_path.write_text(USERS_YAML)

    with (
        patch.object(webapp, "USER_DB_FILE", str(users_yaml_path)),
        patch.object(webapp, "CONFIG_ROOT", str(tmp_path)),
    ):
        yield tmp_path


# ── /api/login ─────────────────────────────────────────────────────────────────


class TestLogin:
    def test_valid_credentials_return_token(self, client, tmp_user_dir):
        r = client.post(
            "/api/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
        )
        assert r.status_code == 200
        data = r.get_json()
        assert "token" in data
        assert "expires_at" in data
        # token must be decodeable
        payload = jwt.decode(data["token"], JWT_SECRET, algorithms=[JWT_ALGORITHM])
        assert payload["sub"] == TEST_USERNAME

    def test_missing_fields_returns_400(self, client, tmp_user_dir):
        r = client.post("/api/login", json={"username": TEST_USERNAME})
        assert r.status_code == 400
        assert "error" in r.get_json()

    def test_wrong_password_returns_401(self, client, tmp_user_dir):
        r = client.post(
            "/api/login", json={"username": TEST_USERNAME, "password": "wrongpass"}
        )
        assert r.status_code == 401

    def test_unknown_user_returns_401(self, client, tmp_user_dir):
        r = client.post(
            "/api/login", json={"username": "ghost", "password": "irrelevant"}
        )
        assert r.status_code == 401

    def test_missing_user_config_returns_403(self, client, tmp_user_dir):
        """User exists in users.yaml but has no config.yaml directory."""
        users_yaml = tmp_user_dir / "users.yaml"
        extra_hash = bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode()
        users_yaml.write_text(
            yaml.dump(
                {
                    "users": {
                        TEST_USERNAME: {"password": _HASHED_PW},
                        "noconfig": {"password": extra_hash},
                    }
                }
            )
        )
        r = client.post("/api/login", json={"username": "noconfig", "password": "pw"})
        assert r.status_code == 403


# ── JWT middleware ─────────────────────────────────────────────────────────────


class TestJwtMiddleware:
    def test_missing_token_returns_401(self, client, tmp_user_dir):
        r = client.get("/api/generate/status")
        assert r.status_code == 401

    def test_expired_token_returns_401(self, client, tmp_user_dir):
        token = _mint_token(expired=True)
        r = client.get(
            "/api/generate/status", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 401

    def test_invalid_token_returns_401(self, client, tmp_user_dir):
        r = client.get(
            "/api/generate/status",
            headers={"Authorization": "Bearer not.a.valid.token"},
        )
        assert r.status_code == 401


# ── /api/generate/status ───────────────────────────────────────────────────────


class TestGenerateStatus:
    def _get(self, client, tmp_user_dir, offset=0):
        token = _mint_token()
        return client.get(
            f"/api/generate/status?offset={offset}",
            headers={"Authorization": f"Bearer {token}"},
        )

    def test_no_log_file_returns_empty_not_done(self, client, tmp_user_dir):
        r = self._get(client, tmp_user_dir)
        assert r.status_code == 200
        data = r.get_json()
        assert data["log"] == ""
        assert data["offset"] == 0
        assert data["done"] is False

    def test_returns_log_chunk_from_offset(self, client, tmp_user_dir):
        log_path = tmp_user_dir / "output" / "output.log"
        log_path.write_text("line1\nline2\nline3\n")
        size = log_path.stat().st_size

        r = self._get(client, tmp_user_dir, offset=0)
        data = r.get_json()
        assert "line1" in data["log"]
        assert data["offset"] == size
        assert data["done"] is False

    def test_offset_returns_only_new_content(self, client, tmp_user_dir):
        log_path = tmp_user_dir / "output" / "output.log"
        log_path.write_text("AAAAABBBBB")
        # first 5 bytes already seen
        r = self._get(client, tmp_user_dir, offset=5)
        data = r.get_json()
        assert data["log"] == "BBBBB"

    def test_done_flag_set_on_finish_marker(self, client, tmp_user_dir):
        log_path = tmp_user_dir / "output" / "output.log"
        log_path.write_text("some output\n=== Generation finished ===\n")
        r = self._get(client, tmp_user_dir)
        assert r.get_json()["done"] is True

    def test_error_marker_is_not_terminal(self, client, tmp_user_dir):
        # Backfill steps keep running after a generation error; only the
        # finish marker ends the run.
        log_path = tmp_user_dir / "output" / "output.log"
        log_path.write_text("ERROR: Generation failed\n")
        r = self._get(client, tmp_user_dir)
        assert r.get_json()["done"] is False

    def test_invalid_offset_does_not_500(self, client, tmp_user_dir):
        r = self._get(client, tmp_user_dir, offset="abc")
        assert r.status_code == 200


# ── /api/diary/entry ───────────────────────────────────────────────────────────


class TestAddDiaryEntry:
    def _post(self, client, tmp_user_dir, payload):
        token = _mint_token()
        return client.post(
            "/api/diary/entry",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )

    def test_missing_date_returns_400(self, client, tmp_user_dir):
        r = self._post(client, tmp_user_dir, {"sentences": ["Hello"]})
        assert r.status_code == 400

    def test_missing_sentences_returns_400(self, client, tmp_user_dir):
        r = self._post(client, tmp_user_dir, {"date": "2026-01-01"})
        assert r.status_code == 400

    def test_invalid_date_format_returns_400(self, client, tmp_user_dir):
        r = self._post(
            client, tmp_user_dir, {"date": "01/01/2026", "sentences": ["Hi"]}
        )
        assert r.status_code == 400

    def test_new_day_succeeds_no_warnings(self, client, tmp_user_dir):
        from lingodiary.diary_json import DiaryJson

        empty_diary = DiaryJson()
        with (
            patch("lingodiary.diary_json.load_diary_json", return_value=empty_diary),
            patch("lingodiary.diary_json.save_diary_json"),
            patch(
                "lingodiary.diary_json.upsert_day", return_value=empty_diary
            ) as mock_upsert,
            patch("lingodiary.diary_json.get_day", return_value=None),
        ):
            r = self._post(
                client,
                tmp_user_dir,
                {"date": "2026-01-15", "sentences": ["Hello", "World"]},
            )

        assert r.status_code == 200
        data = r.get_json()
        assert data["success"] is True
        assert data["sentences_added"] == 2
        assert data["warnings"] == []

    def test_existing_day_appends_without_deleting(self, client, tmp_user_dir):
        from lingodiary.diary_json import (
            DiaryDay,
            DiaryJson,
            Sentence,
        )

        diary = DiaryJson(
            diaries=[
                DiaryDay(
                    date="2026/01/15",
                    entries=[
                        Sentence(index=0, input_language_sentence="One"),
                        Sentence(index=1, input_language_sentence="Two"),
                    ],
                )
            ]
        )
        with (
            patch("lingodiary.diary_json.load_diary_json", return_value=diary),
            patch("lingodiary.diary_json.save_diary_json"),
        ):
            r = self._post(
                client,
                tmp_user_dir,
                {"date": "2026-01-15", "sentences": ["Two", "Three"]},
            )

        assert r.status_code == 200
        data = r.get_json()
        assert data["sentences_added"] == 1
        assert data["sentences_skipped"] == 1
        assert len(data["warnings"]) == 1
        assert [e.input_language_sentence for e in diary.diaries[0].entries] == [
            "One",
            "Two",
            "Three",
        ]
        assert [e.index for e in diary.diaries[0].entries] == [0, 1, 2]


# ── /api/lessons/score ─────────────────────────────────────────────────────────


class TestLessonScore:
    def _post(self, client, tmp_user_dir, payload):
        token = _mint_token()
        return client.post(
            "/api/lessons/score",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )

    def test_missing_fields_returns_400(self, client, tmp_user_dir):
        r = self._post(client, tmp_user_dir, {"date": "2026-01-01", "score": 3})
        assert r.status_code == 400

    def test_invalid_score_returns_400(self, client, tmp_user_dir):
        r = self._post(
            client,
            tmp_user_dir,
            {"date": "2026-01-01", "entry_index": 0, "score": 99},
        )
        assert r.status_code == 400

    def test_valid_score_returns_ok(self, client, tmp_user_dir):
        from lingodiary.diary_json import DiaryJson, ReviewingState

        fake_diary = DiaryJson()
        fake_reviewing = ReviewingState()
        with (
            patch("lingodiary.diary_json.load_diary_json", return_value=fake_diary),
            patch("lingodiary.diary_json.save_diary_json"),
            patch(
                "lingodiary.diary_json.update_srs",
                return_value=(fake_diary, fake_reviewing),
            ),
        ):
            r = self._post(
                client,
                tmp_user_dir,
                {"date": "2026-01-01", "entry_index": 0, "score": 3},
            )
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

    def test_entry_not_found_returns_404(self, client, tmp_user_dir):
        from lingodiary.diary_json import DiaryJson

        fake_diary = DiaryJson()
        with (
            patch("lingodiary.diary_json.load_diary_json", return_value=fake_diary),
            patch(
                "lingodiary.diary_json.update_srs", side_effect=ValueError("not found")
            ),
        ):
            r = self._post(
                client,
                tmp_user_dir,
                {"date": "2026-01-01", "entry_index": 99, "score": 5},
            )
        assert r.status_code == 404


# ── security / validation ──────────────────────────────────────────────────────


class TestSyncFileTraversal:
    def test_sibling_directory_with_shared_prefix_is_denied(self, client, tmp_user_dir):
        out = tmp_user_dir / "output"
        sibling = tmp_user_dir / "output2"
        sibling.mkdir()
        (sibling / "secret.txt").write_text("nope")
        token = _mint_token()
        r = client.get(
            "/api/sync/file/../output2/secret.txt",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code in (403, 404)
        assert b"nope" not in r.data
        assert out.exists()

    def test_token_query_param_rejected_outside_file_route(self, client, tmp_user_dir):
        token = _mint_token()
        r = client.get(f"/api/home?token={token}")
        assert r.status_code == 401


class TestLoginThrottle:
    def test_repeated_failures_are_throttled(self, client, tmp_user_dir):
        webapp._login_failures.clear()
        codes = [
            client.post(
                "/api/login", json={"username": TEST_USERNAME, "password": "bad"}
            ).status_code
            for _ in range(webapp._LOGIN_MAX_FAILURES + 1)
        ]
        assert codes[-1] == 429
        webapp._login_failures.clear()


class TestInputValidation:
    def test_unknown_variant_returns_400(self, client, tmp_user_dir):
        token = _mint_token()
        r = client.get(
            "/api/lessons/entries/2026-01-01/reviewing",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 400

    def test_non_numeric_entry_index_returns_400(self, client, tmp_user_dir):
        token = _mint_token()
        r = client.post(
            "/api/lessons/score",
            json={"date": "2026-01-01", "entry_index": "x", "score": 3},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 400

    def test_bad_last_reviewed_timestamp_returns_400(self, client, tmp_user_dir):
        token = _mint_token()
        r = client.put(
            "/api/lessons/last_reviewed/2026-01-01",
            json={"timestamp": "yesterday-ish"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 400
