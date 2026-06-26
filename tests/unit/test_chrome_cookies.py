"""Tests for Chrome cookie extraction and the session_sync_from_chrome tool."""

import json
import time

import pytest

from texas_grocery_mcp.auth import chrome_cookies as cc

# =============================================================================
# Pure helpers (no Chrome / no Keychain required)
# =============================================================================


def test_chrome_us_to_unix_session_cookie():
    """A zero expires_utc (session cookie) maps to -1 (Playwright convention)."""
    assert cc._chrome_us_to_unix(0) == -1.0


def test_chrome_us_to_unix_known_timestamp():
    """Chrome microseconds-since-1601 converts to the right Unix time."""
    # 2021-01-01T00:00:00Z == 1609459200 unix.
    chrome_us = (1609459200 + cc._CHROME_EPOCH_OFFSET_SECONDS) * 1_000_000
    assert cc._chrome_us_to_unix(chrome_us) == pytest.approx(1609459200, abs=1)


def test_samesite_mapping():
    assert cc._samesite_to_playwright(0) == "None"
    assert cc._samesite_to_playwright(1) == "Lax"
    assert cc._samesite_to_playwright(2) == "Strict"
    assert cc._samesite_to_playwright(99) == "Lax"  # unknown -> safe default


def test_decrypt_value_empty_returns_empty():
    assert cc._decrypt_value(b"", b"\x00" * 16) == ""


def test_decrypt_value_legacy_plaintext():
    """Non-v10/v11 values are treated as legacy plaintext."""
    assert cc._decrypt_value(b"plain-value", b"\x00" * 16) == "plain-value"


# =============================================================================
# Profile discovery / resolution (uses a fake User Data dir)
# =============================================================================


@pytest.fixture
def fake_user_data(tmp_path):
    """Build a fake Chrome 'User Data' dir with a Local State file."""
    local_state = {
        "profile": {
            "info_cache": {
                "Default": {"name": "Your Chrome", "user_name": "work@corp.com"},
                "Profile 1": {"name": "Martín - personal", "user_name": "me@gmail.com"},
            }
        }
    }
    (tmp_path / "Local State").write_text(json.dumps(local_state))
    (tmp_path / "Default").mkdir()
    (tmp_path / "Profile 1").mkdir()
    return tmp_path


def test_list_profiles(fake_user_data):
    profiles = cc.list_profiles(fake_user_data)
    by_dir = {p["dir"]: p for p in profiles}
    assert by_dir["Profile 1"]["name"] == "Martín - personal"
    assert by_dir["Default"]["email"] == "work@corp.com"


def test_resolve_profile_by_display_name(fake_user_data):
    assert cc._resolve_profile_dir("Martín - personal", fake_user_data) == "Profile 1"


def test_resolve_profile_by_dir_name(fake_user_data):
    assert cc._resolve_profile_dir("Profile 1", fake_user_data) == "Profile 1"


def test_resolve_profile_unknown_raises(fake_user_data):
    with pytest.raises(cc.ChromeCookieError) as exc:
        cc._resolve_profile_dir("Nonexistent", fake_user_data)
    # Error message should list available profiles to guide the user.
    assert "Martín - personal" in str(exc.value)


# =============================================================================
# reese84 cookie validation (the Chrome-sourced session path)
# =============================================================================


def test_is_authenticated_with_reese84_cookie(monkeypatch, tmp_path):
    """A session with reese84 as a COOKIE (no localStorage) is authenticated.

    This is the shape produced by extracting straight from Chrome, and is the
    key behavior that makes session_sync_from_chrome usable end to end.
    """
    from texas_grocery_mcp.auth import session as sess

    auth_file = tmp_path / "auth.json"
    future = time.time() + 86400

    class MockSettings:
        auth_state_path = auth_file

    monkeypatch.setattr(sess, "get_settings", lambda: MockSettings())

    state = {
        "cookies": [
            {"name": "sat", "value": "t", "domain": "www.heb.com", "expires": future},
            {"name": "DYN_USER_ID", "value": "1", "domain": "www.heb.com",
             "expires": future},
            {"name": "reese84", "value": "tok", "domain": ".heb.com",
             "expires": future},
        ],
        "origins": [],  # No localStorage -- reese84 only lives in the cookie.
    }
    auth_file.write_text(json.dumps(state))

    assert sess.is_authenticated() is True
    status = sess.get_session_status()
    assert status["authenticated"] is True
    assert status["reese84_present"] is True
    assert status["time_remaining_hours"] is not None


def test_expired_reese84_cookie_not_authenticated(monkeypatch, tmp_path):
    """An expired reese84 cookie (and no localStorage) is not authenticated."""
    from texas_grocery_mcp.auth import session as sess

    auth_file = tmp_path / "auth.json"
    future = time.time() + 86400
    past = time.time() - 3600

    class MockSettings:
        auth_state_path = auth_file

    monkeypatch.setattr(sess, "get_settings", lambda: MockSettings())

    state = {
        "cookies": [
            {"name": "sat", "value": "t", "domain": "www.heb.com", "expires": future},
            {"name": "DYN_USER_ID", "value": "1", "domain": "www.heb.com",
             "expires": future},
            {"name": "reese84", "value": "tok", "domain": ".heb.com", "expires": past},
        ],
        "origins": [],
    }
    auth_file.write_text(json.dumps(state))

    assert sess.is_authenticated() is False


# =============================================================================
# session_sync_from_chrome tool (extraction mocked)
# =============================================================================


@pytest.mark.asyncio
async def test_session_sync_from_chrome_success(monkeypatch, tmp_path):
    """The tool writes auth.json and reports authenticated when cookies sync."""
    from texas_grocery_mcp.auth import session as sess
    from texas_grocery_mcp.tools import session as session_tools

    auth_file = tmp_path / "auth.json"
    future = time.time() + 86400

    class MockSettings:
        auth_state_path = auth_file

    monkeypatch.setattr(session_tools, "get_settings", lambda: MockSettings())
    monkeypatch.setattr(sess, "get_settings", lambda: MockSettings())

    fake_cookies = [
        {"name": "sat", "value": "t", "domain": "www.heb.com", "path": "/",
         "expires": future, "httpOnly": True, "secure": True, "sameSite": "Lax"},
        {"name": "DYN_USER_ID", "value": "1", "domain": "www.heb.com", "path": "/",
         "expires": future, "httpOnly": True, "secure": True, "sameSite": "Lax"},
        {"name": "reese84", "value": "tok", "domain": ".heb.com", "path": "/",
         "expires": future, "httpOnly": False, "secure": True, "sameSite": "None"},
    ]
    monkeypatch.setattr(
        session_tools, "extract_heb_cookies", lambda profile=None: (fake_cookies, "Profile 1")
    )

    result = await session_tools.session_sync_from_chrome(profile="Profile 1")

    assert result["success"] is True
    assert result["authenticated"] is True
    assert result["profile"] == "Profile 1"
    assert result["cookies_count"] == 3
    assert auth_file.exists()
    # Auth file should contain the HEB cookies in storage-state format.
    saved = json.loads(auth_file.read_text())
    assert any(c["name"] == "reese84" for c in saved["cookies"])


@pytest.mark.asyncio
async def test_session_sync_from_chrome_extraction_failure(monkeypatch, tmp_path):
    """When extraction fails, the tool returns a structured error with profiles."""
    from texas_grocery_mcp.tools import session as session_tools

    auth_file = tmp_path / "auth.json"

    class MockSettings:
        auth_state_path = auth_file

    monkeypatch.setattr(session_tools, "get_settings", lambda: MockSettings())

    def _raise(profile=None):
        raise cc.ChromeCookieError("Chrome not logged in")

    monkeypatch.setattr(session_tools, "extract_heb_cookies", _raise)
    monkeypatch.setattr(
        session_tools, "list_profiles",
        lambda: [{"dir": "Profile 1", "name": "Martín - personal", "email": ""}],
    )

    result = await session_tools.session_sync_from_chrome()

    assert result["success"] is False
    assert result["error_type"] == "chrome_extraction_failed"
    assert result["profiles"][0]["name"] == "Martín - personal"
    assert not auth_file.exists()
