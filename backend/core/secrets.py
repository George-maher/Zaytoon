"""Credential storage that never reaches a response.

Three properties, all of them load-bearing:

* **A secret is never returned.** :func:`get_secret` is the only way to read one
  and it is called from exactly two places: the provider client that needs an
  ``Authorization`` header, and a test. Every API surface reports
  ``has_api_key: true`` and nothing else. There is a test asserting that no
  route ever serialises the value.
* **A secret is never logged.** Values are stored encrypted and
  :func:`common.reporting.redact` still runs on anything derived from them, so
  a mistake elsewhere cannot turn a stack trace into a credential leak.
* **The encryption is real on Windows.** DPAPI through ``ctypes`` — no new
  dependency, and the OS user key is the correct key for a per-user desktop app.
  Elsewhere it is a ``0600`` file, and that limitation is stated here rather
  than implied away: on a POSIX host this protects against accidental
  disclosure (a backup, a repo, a log tail), not against another process running
  as the same user.

Secrets live in a **separate file** from ``core.db``. A database is something a
user opens with a SQLite browser and deletes to reset the app; credentials
should not ride along with task history.
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import stat
import sys
import threading
from typing import Any, Dict, Optional

from common.reporting import safe_error

logger = logging.getLogger("Zaytoon.Core.Secrets")

#: On-disk prefix identifying the format, so a future change can be detected
#: rather than silently misread.
_MAGIC = "zsec1"
_ENTROPY = b"zaytoon.secrets.v1"

_lock = threading.RLock()
_cache: Dict[str, str] = {}
_loaded = False


def secrets_path() -> str:
    """Where the credential file lives."""
    override = os.getenv("ZAYTOON_SECRETS_PATH")
    if override:
        return os.path.abspath(override)
    from core import paths

    # A portable layout separates Config/ from Data/ — a credential file is
    # configuration, and `Data/` is the one directory an update may clear. A
    # development checkout has no such separation and keeps the historical
    # `backend/data/secrets.json`, so an existing install does not appear to lose
    # its keys when the layout module was introduced.
    if paths.zaytoon_home() is not None:
        directory = paths.ensure(paths.config_dir())
    else:
        directory = os.path.join(str(paths.ensure(paths.data_dir())))
    return os.path.join(directory, "secrets.json")


# ---------------------------------------------------------------------------
# Windows DPAPI
# ---------------------------------------------------------------------------
def _dpapi_available() -> bool:
    return sys.platform == "win32" and hasattr(ctypes, "windll")


def _dpapi_protect(data: bytes) -> Optional[bytes]:
    """Encrypt with the current user's DPAPI key. ``None`` if unavailable."""
    if not _dpapi_available():
        return None
    try:
        from ctypes import wintypes  # noqa: PLC0415 - Windows only

        class _Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]

        buffer = ctypes.create_string_buffer(data, len(data))
        blob = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        out = _Blob()
        if not crypt32.CryptProtectData(
            ctypes.byref(blob), None, None, None, None, 0x01, ctypes.byref(out)
        ):
            return None
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            kernel32.LocalFree(out.pbData)
    except Exception as exc:  # noqa: BLE001 - fall back, never fail a save
        logger.debug(safe_error(exc, operation="secrets.dpapi", component="core.secrets"))
        return None


def _dpapi_unprotect(data: bytes) -> Optional[bytes]:
    """Decrypt with the current user's DPAPI key. ``None`` if unavailable."""
    if not _dpapi_available():
        return None
    try:
        from ctypes import wintypes  # noqa: PLC0415 - Windows only

        class _Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]

        buffer = ctypes.create_string_buffer(data, len(data))
        blob = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        out = _Blob()
        if not crypt32.CryptUnprotectData(
            ctypes.byref(blob), None, None, None, None, 0x01, ctypes.byref(out)
        ):
            return None
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            kernel32.LocalFree(out.pbData)
    except Exception as exc:  # noqa: BLE001
        logger.debug(safe_error(exc, operation="secrets.dpapi", component="core.secrets"))
        return None


# ---------------------------------------------------------------------------
# Portable fallback
# ---------------------------------------------------------------------------
def _machine_key() -> bytes:
    """A per-installation key material source.

    This is obfuscation, not cryptography, and it is documented as such: it
    combines the install path and the hostname so a copied file is less useful
    than a real key would be. The DPAPI path above is what actually protects a
    Windows install.
    """
    import hashlib

    seed = f"{os.path.abspath(os.getcwd())}|{_ENTROPY.decode()}".encode("utf-8")
    try:
        seed += socket_hostname().encode("utf-8")
    except Exception:  # pragma: no cover - defensive
        pass
    return hashlib.sha256(seed).digest()


def socket_hostname() -> str:
    import socket

    try:
        return socket.gethostname()
    except Exception:  # pragma: no cover - defensive
        return ""


def _xor(data: bytes, key: bytes) -> bytes:
    stream = bytearray()
    counter = 0
    while len(stream) < len(data):
        stream += _keystream(key, counter)
        counter += 1
    return bytes(a ^ b for a, b in zip(data, stream[: len(data)]))


def _keystream(key: bytes, counter: int) -> bytes:
    import hashlib

    return hashlib.sha256(key + counter.to_bytes(4, "big")).digest()


def _encode(value: str) -> str:
    """Encrypt a secret for storage. DPAPI where possible."""
    raw = value.encode("utf-8")
    protected = _dpapi_protect(raw)
    if protected is not None:
        return f"{_MAGIC}:dpapi:{base64.b64encode(protected).decode('ascii')}"
    return f"{_MAGIC}:xor:{base64.b64encode(_xor(raw, _machine_key())).decode('ascii')}"


def _decode(stored: str) -> Optional[str]:
    """Decrypt a stored secret. ``None`` when it cannot be read."""
    if not stored:
        return None
    parts = stored.split(":", 2)
    if len(parts) != 3 or parts[0] != _MAGIC:
        return None
    try:
        scheme, payload = parts[1], parts[2]
        blob = base64.b64decode(payload)
        if scheme == "dpapi":
            plain = _dpapi_unprotect(blob)
            if plain is None:
                return None
            return plain.decode("utf-8")
        return _xor(blob, _machine_key()).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - an unreadable secret is absent
        logger.warning(safe_error(exc, operation="secrets.decode", component="core.secrets"))
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def _load() -> Dict[str, str]:
    global _loaded
    with _lock:
        if _loaded:
            return _cache
        _cache.clear()
        try:
            with open(secrets_path(), "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                _cache.update({str(k): str(v) for k, v in data.items()})
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001 - a corrupt file means "no secrets"
            logger.warning(
                safe_error(exc, operation="secrets.load", component="core.secrets")
            )
        _loaded = True
        return _cache


def _flush() -> None:
    path = secrets_path()
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(_cache, handle, indent=2, sort_keys=True)
    if os.name != "nt":
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:  # pragma: no cover - exotic filesystems
            logger.debug("Could not tighten permissions on the secrets file")


def set_secret(provider_id: str, api_key: str) -> bool:
    """Store a credential. Never returns the value."""
    if not provider_id:
        return False
    with _lock:
        _load()
        _cache[provider_id] = _encode(api_key or "")
        try:
            _flush()
            return True
        except OSError as exc:
            logger.warning(
                safe_error(exc, operation="secrets.set", component="core.secrets")
            )
            return False


def get_secret(provider_id: str) -> Optional[str]:
    """Read a credential.

    Call this only where a request is actually about to be signed. Nothing that
    renders to a client may call it.
    """
    with _lock:
        stored = _load().get(provider_id)
    if not stored:
        return None
    return _decode(stored)


def has_secret(provider_id: str) -> bool:
    """True when a credential is stored. This is what the API reports."""
    with _lock:
        return bool(_load().get(provider_id))


def delete_secret(provider_id: str) -> bool:
    """Forget a credential. Returns whether one was present."""
    with _lock:
        data = _load()
        if provider_id not in data:
            return False
        data.pop(provider_id, None)
        try:
            _flush()
            return True
        except OSError as exc:
            logger.warning(
                safe_error(exc, operation="secrets.delete", component="core.secrets")
            )
            return False


def list_secret_ids() -> list:
    """Which providers have credentials — ids only, never values."""
    with _lock:
        return sorted(_load().keys())


def reset_cache() -> None:
    """Forget the in-process cache. **Tests only.**"""
    global _loaded
    with _lock:
        _cache.clear()
        _loaded = False


__all__ = [
    "delete_secret",
    "get_secret",
    "has_secret",
    "list_secret_ids",
    "reset_cache",
    "secrets_path",
    "set_secret",
]