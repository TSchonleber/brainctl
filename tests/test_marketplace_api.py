"""Unit tests for ``agentmemory.marketplace_api`` — session persistence
+ HTTP wrapper error handling. Network-dependent endpoints get tested
in ``tests/test_marketplace_api_live.py`` (gated on
``BRAINCTL_LIVE_MARKETPLACE_API=1``).
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from agentmemory import marketplace_api as api


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------

class TestSessionPersistence:
    def _isolate(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(api, "SESSION_DIR", str(tmp_path))

    def test_write_read_roundtrip(self, tmp_path: Path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        payload = {
            "session_token": "a" * 96,
            "pubkey": "P",
            "expires_at": "2099-01-01T00:00:00+00:00",
        }
        api.store_session("https://example/api", "P", payload)
        loaded = api.stored_session_for("https://example/api", "P")
        assert loaded == payload

    def test_returns_none_when_missing(self, tmp_path: Path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        assert api.stored_session_for("https://example/api", "MissingP") is None

    def test_returns_none_when_near_expiry(self, tmp_path: Path, monkeypatch):
        from datetime import datetime, timedelta, timezone

        self._isolate(tmp_path, monkeypatch)
        soon = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
        api.store_session("https://example/api", "P", {
            "session_token": "x" * 96,
            "expires_at": soon,
        })
        # 10s away — within the 30s slop window — should not return.
        assert api.stored_session_for("https://example/api", "P") is None

    def test_file_perms_0600(self, tmp_path: Path, monkeypatch):
        if os.name == "nt":
            pytest.skip("POSIX-only perm semantics")
        self._isolate(tmp_path, monkeypatch)
        api.store_session("https://example/api", "P", {
            "session_token": "y" * 96,
            "expires_at": "2099-01-01T00:00:00+00:00",
        })
        st = (tmp_path / api.SESSION_FILENAME).stat()
        assert stat.S_IMODE(st.st_mode) == 0o600

    def test_clear_session_specific(self, tmp_path: Path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        api.store_session("https://a/api", "P1", {
            "session_token": "t1" + "0" * 94,
            "expires_at": "2099-01-01T00:00:00+00:00",
        })
        api.store_session("https://a/api", "P2", {
            "session_token": "t2" + "0" * 94,
            "expires_at": "2099-01-01T00:00:00+00:00",
        })
        api.clear_session("https://a/api", "P1")
        assert api.stored_session_for("https://a/api", "P1") is None
        assert api.stored_session_for("https://a/api", "P2") is not None

    def test_clear_session_all_for_base(self, tmp_path: Path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        api.store_session("https://a/api", "P1", {
            "session_token": "t1" + "0" * 94,
            "expires_at": "2099-01-01T00:00:00+00:00",
        })
        api.store_session("https://b/api", "P2", {
            "session_token": "t2" + "0" * 94,
            "expires_at": "2099-01-01T00:00:00+00:00",
        })
        api.clear_session("https://a/api")
        assert api.stored_session_for("https://a/api", "P1") is None
        assert api.stored_session_for("https://b/api", "P2") is not None


# ---------------------------------------------------------------------------
# API base resolution
# ---------------------------------------------------------------------------

class TestApiBase:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("BRNCTL_MARKETPLACE_API", raising=False)
        assert api.api_base_from_env() == api.DEFAULT_API_BASE

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("BRNCTL_MARKETPLACE_API", "https://staging.brnctl.fun/api/marketplace/")
        # Trailing slash should be stripped.
        assert api.api_base_from_env() == "https://staging.brnctl.fun/api/marketplace"


# ---------------------------------------------------------------------------
# Error wrapping
# ---------------------------------------------------------------------------

class TestErrorPayload:
    def test_error_renders_with_detail(self):
        err = api.MarketplaceApiError(409, {"error": "offer_terminal", "detail": "status=accepted"})
        assert "409" in str(err)
        assert "offer_terminal" in str(err)
        assert "status=accepted" in str(err)

    def test_error_renders_minimal(self):
        err = api.MarketplaceApiError(500, {})
        assert str(err) == "HTTP 500"

    def test_payload_accessible(self):
        err = api.MarketplaceApiError(400, {"error": "bad_pubkey"})
        assert err.payload == {"error": "bad_pubkey"}
        assert err.status == 400


# ---------------------------------------------------------------------------
# URL encoding
# ---------------------------------------------------------------------------

class TestUrlEncode:
    def test_basic(self):
        assert api._url_encode("hello") == "hello"

    def test_special_chars(self):
        # listing_ids contain dashes — should pass through unmodified.
        assert api._url_encode("20260512-abc123") == "20260512-abc123"
        # / and = need escaping.
        assert api._url_encode("a/b=c") == "a%2Fb%3Dc"
