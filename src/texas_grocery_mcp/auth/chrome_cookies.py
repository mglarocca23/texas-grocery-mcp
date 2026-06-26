"""Extract HEB session cookies directly from the user's real Chrome browser.

This is the primary (and most reliable) authentication strategy. HEB.com uses
Imperva's WAF with a ``reese84`` bot-detection token. Imperva blocks *headless*
browsers (returns HTTP 401 before the page loads), which is why the embedded
Playwright ``session_refresh`` path fails.

The user, however, is already logged into HEB in their normal Chrome browser.
Chrome stores cookies in an encrypted SQLite database; the AES key lives in the
OS keystore (macOS Keychain "Chrome Safe Storage" / Linux Secret Service, or a
well-known fallback password). By reading and decrypting that database we can
harvest a complete, valid HEB session -- including ``reese84`` -- without any
browser automation, just file reads and a keystore lookup.

Empirically verified: cookies harvested this way are accepted by Imperva when
replayed through a plain ``httpx`` client (the ``reese84`` token is *not* bound
to Chrome's TLS fingerprint), so the existing GraphQL/SSR clients work unchanged
once ``auth.json`` is populated.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()


class ChromeCookieError(Exception):
    """Raised when Chrome cookies cannot be extracted or decrypted."""


# Chrome stores timestamps as microseconds since 1601-01-01 (Windows epoch).
# Subtract the seconds between 1601-01-01 and 1970-01-01 to get a Unix timestamp.
_CHROME_EPOCH_OFFSET_SECONDS = 11644473600

# PBKDF2 parameters Chromium uses to derive the cookie-encryption key.
_PBKDF2_SALT = b"saltysalt"
_PBKDF2_ITERATIONS_MAC = 1003
_PBKDF2_ITERATIONS_LINUX = 1
_PBKDF2_KEY_LENGTH = 16  # AES-128
# Chromium uses a 16-byte all-spaces IV for v10/v11 cookie values.
_AES_IV = b" " * 16


def _chrome_user_data_dir() -> Path:
    """Return the platform-specific Chrome "User Data" directory."""
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Google/Chrome"
    if sys.platform.startswith("linux"):
        # Try the most common locations in priority order.
        for candidate in (
            Path.home() / ".config/google-chrome",
            Path.home() / ".config/chromium",
        ):
            if candidate.exists():
                return candidate
        return Path.home() / ".config/google-chrome"
    if sys.platform.startswith("win"):
        import os

        local = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))
        return Path(local) / "Google/Chrome/User Data"
    raise ChromeCookieError(f"Unsupported platform for Chrome extraction: {sys.platform}")


def _get_encryption_key() -> tuple[bytes, int]:
    """Return ``(aes_key, pbkdf2_iterations)`` for decrypting cookie values.

    On macOS the key material is the "Chrome Safe Storage" Keychain password.
    On Linux it is the Secret Service entry, falling back to the well-known
    ``"peanuts"`` password Chromium uses when no keyring is present.
    """
    if sys.platform == "darwin":
        try:
            password = subprocess.run(
                [
                    "security",
                    "find-generic-password",
                    "-w",
                    "-s",
                    "Chrome Safe Storage",
                    "-a",
                    "Chrome",
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        except subprocess.CalledProcessError as e:
            raise ChromeCookieError(
                "Could not read 'Chrome Safe Storage' from the macOS Keychain. "
                "Grant terminal/Keychain access and ensure Chrome is installed."
            ) from e
        if not password:
            raise ChromeCookieError("'Chrome Safe Storage' Keychain entry was empty.")
        key = hashlib.pbkdf2_hmac(
            "sha1", password.encode(), _PBKDF2_SALT, _PBKDF2_ITERATIONS_MAC,
            dklen=_PBKDF2_KEY_LENGTH,
        )
        return key, _PBKDF2_ITERATIONS_MAC

    if sys.platform.startswith("linux"):
        password = b"peanuts"  # Chromium default when no Secret Service entry exists.
        try:
            import secretstorage  # type: ignore[import-untyped]

            conn = secretstorage.dbus_init()
            collection = secretstorage.get_default_collection(conn)
            for item in collection.get_all_items():
                if item.get_label() == "Chrome Safe Storage":
                    password = item.get_secret()
                    break
        except Exception as e:  # pragma: no cover - depends on host keyring
            logger.debug("Secret Service lookup failed, using fallback", error=str(e))
        key = hashlib.pbkdf2_hmac(
            "sha1", password if isinstance(password, bytes) else password.encode(),
            _PBKDF2_SALT, _PBKDF2_ITERATIONS_LINUX, dklen=_PBKDF2_KEY_LENGTH,
        )
        return key, _PBKDF2_ITERATIONS_LINUX

    raise ChromeCookieError(
        f"Cookie decryption is not implemented for platform: {sys.platform}"
    )


def _decrypt_value(encrypted: bytes, key: bytes) -> str:
    """Decrypt a single Chrome cookie value (v10/v11, AES-128-CBC).

    Returns an empty string when the value cannot be decrypted, so a single bad
    cookie never aborts the whole extraction.
    """
    if not encrypted:
        return ""

    version = encrypted[:3]
    if version not in (b"v10", b"v11"):
        # Legacy/unencrypted value -- return as-is when it is printable text.
        try:
            return encrypted.decode("utf-8")
        except UnicodeDecodeError:
            return ""

    # Imported lazily so the module imports even if cryptography is missing;
    # callers get a clear error only when they actually attempt extraction.
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as e:  # pragma: no cover - dependency declared in pyproject
        raise ChromeCookieError(
            "The 'cryptography' package is required to decrypt Chrome cookies."
        ) from e

    try:
        cipher = Cipher(algorithms.AES(key), modes.CBC(_AES_IV))
        decryptor = cipher.decryptor()
        decrypted = decryptor.update(encrypted[3:]) + decryptor.finalize()
        # Strip PKCS#7 padding.
        if decrypted and 1 <= decrypted[-1] <= 16:
            decrypted = decrypted[: -decrypted[-1]]
        # Chrome >= v24 prepends a 32-byte SHA-256 of the domain to the
        # plaintext. If a straight UTF-8 decode fails, drop that prefix.
        try:
            return decrypted.decode("utf-8")
        except UnicodeDecodeError:
            return decrypted[32:].decode("utf-8", "replace")
    except Exception as e:
        logger.debug("Failed to decrypt a cookie value", error=str(e))
        return ""


def _chrome_us_to_unix(expires_utc: int) -> float:
    """Convert a Chrome ``expires_utc`` value to a Unix timestamp.

    Chrome uses microseconds since the Windows epoch (1601-01-01). A value of 0
    means a session cookie, which Playwright represents as ``-1``.
    """
    if not expires_utc:
        return -1.0
    return expires_utc / 1_000_000 - _CHROME_EPOCH_OFFSET_SECONDS


def _samesite_to_playwright(value: int) -> str:
    """Map Chrome's integer ``samesite`` column to Playwright's string enum."""
    return {0: "None", 1: "Lax", 2: "Strict"}.get(value, "Lax")


def list_profiles(user_data_dir: Path | None = None) -> list[dict[str, str]]:
    """List Chrome profiles with their human-readable display names.

    Returns a list of ``{"dir": "Profile 1", "name": "Martín - personal",
    "email": "..."}`` dicts, read from Chrome's ``Local State`` file.
    """
    base = user_data_dir or _chrome_user_data_dir()
    local_state = base / "Local State"
    profiles: list[dict[str, str]] = []
    if not local_state.exists():
        return profiles
    try:
        data = json.loads(local_state.read_text())
        info_cache = data.get("profile", {}).get("info_cache", {})
        for dirname, meta in info_cache.items():
            profiles.append(
                {
                    "dir": dirname,
                    "name": meta.get("name", dirname),
                    "email": meta.get("user_name", ""),
                }
            )
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not read Chrome Local State", error=str(e))
    return profiles


def _resolve_profile_dir(profile: str | None, user_data_dir: Path) -> str:
    """Resolve a profile selector (display name OR directory name) to a dir name.

    When ``profile`` is ``None``, auto-detect the profile that holds a logged-in
    HEB session (one with a ``sat`` or ``DYN_USER_ID`` cookie), preferring the
    one with the most HEB cookies.
    """
    profiles = list_profiles(user_data_dir)

    if profile:
        # Match directory name first, then display name (case-insensitive).
        for p in profiles:
            if p["dir"] == profile:
                return p["dir"]
        for p in profiles:
            if p["name"].lower() == profile.lower():
                return p["dir"]
        # Fall back to treating the selector as a literal directory if it exists.
        if (user_data_dir / profile).exists():
            return profile
        available = ", ".join(f"{p['name']!r} ({p['dir']})" for p in profiles)
        raise ChromeCookieError(
            f"Chrome profile {profile!r} not found. Available profiles: {available}"
        )

    # Auto-detect: scan candidate dirs for HEB session cookies.
    candidates = [p["dir"] for p in profiles] or ["Default"]
    best_dir: str | None = None
    best_score = -1
    for dirname in candidates:
        try:
            cookies = _read_raw_heb_cookies(user_data_dir / dirname / "Cookies")
        except ChromeCookieError:
            continue
        names = {name for name, *_ in cookies}
        if not names:
            continue
        # Prefer a profile that is actually logged in.
        score = len(names) + (1000 if ("sat" in names or "DYN_USER_ID" in names) else 0)
        if score > best_score:
            best_score = score
            best_dir = dirname

    if best_dir is None:
        raise ChromeCookieError(
            "No Chrome profile with HEB.com cookies found. Log in to heb.com in "
            "Chrome first, or pass an explicit profile name."
        )
    return best_dir


def _read_raw_heb_cookies(cookies_db: Path) -> list[tuple[Any, ...]]:
    """Read raw (still-encrypted) HEB cookie rows from a Chrome Cookies DB.

    Copies the DB to a temp file first so a running Chrome's lock does not block
    the read.
    """
    if not cookies_db.exists():
        raise ChromeCookieError(f"Chrome Cookies database not found at {cookies_db}")

    tmp = Path(tempfile.mktemp(suffix=".sqlite"))
    try:
        shutil.copy2(cookies_db, tmp)
        conn = sqlite3.connect(str(tmp))
        try:
            rows = conn.execute(
                "SELECT host_key, name, encrypted_value, path, expires_utc, "
                "is_secure, is_httponly, samesite "
                "FROM cookies WHERE host_key LIKE '%heb.com%'"
            ).fetchall()
        finally:
            conn.close()
        return rows
    except sqlite3.Error as e:
        raise ChromeCookieError(f"Could not read Chrome Cookies database: {e}") from e
    finally:
        tmp.unlink(missing_ok=True)


def extract_heb_cookies(profile: str | None = None) -> tuple[list[dict[str, Any]], str]:
    """Extract and decrypt HEB cookies from Chrome in Playwright storage format.

    Args:
        profile: Chrome profile display name (e.g. ``"Martín - personal"``) or
            directory name (e.g. ``"Profile 1"``). When ``None``, auto-detects
            the profile that is logged into HEB.

    Returns:
        ``(cookies, profile_dir)`` where ``cookies`` is a list of Playwright-
        format cookie dicts (``name``, ``value``, ``domain``, ``path``,
        ``expires``, ``httpOnly``, ``secure``, ``sameSite``) for ``*.heb.com``,
        and ``profile_dir`` is the directory the cookies came from.

    Raises:
        ChromeCookieError: If extraction or decryption fails.
    """
    user_data_dir = _chrome_user_data_dir()
    profile_dir = _resolve_profile_dir(profile, user_data_dir)
    key, _ = _get_encryption_key()

    raw_rows = _read_raw_heb_cookies(user_data_dir / profile_dir / "Cookies")

    cookies: list[dict[str, Any]] = []
    decrypt_failures = 0
    for host, name, enc, path, expires_utc, is_secure, is_httponly, samesite in raw_rows:
        value = _decrypt_value(enc, key)
        if not value:
            decrypt_failures += 1
            continue
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": host,
                "path": path or "/",
                "expires": _chrome_us_to_unix(expires_utc),
                "httpOnly": bool(is_httponly),
                "secure": bool(is_secure),
                "sameSite": _samesite_to_playwright(samesite),
            }
        )

    if not cookies:
        raise ChromeCookieError(
            f"No decryptable HEB cookies found in Chrome profile {profile_dir!r}. "
            "Make sure you are logged into heb.com in that profile."
        )

    logger.info(
        "Extracted HEB cookies from Chrome",
        profile=profile_dir,
        count=len(cookies),
        decrypt_failures=decrypt_failures,
    )
    return cookies, profile_dir
