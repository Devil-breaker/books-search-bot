"""Telegram Mini App request authentication."""

from __future__ import annotations

import hashlib
import hmac
import json
import base64
import secrets
import time
from urllib.parse import parse_qsl


class InitDataError(ValueError):
    """Raised when Telegram Mini App initData is missing, stale, or invalid."""


def _inline_token_signature(payload: bytes, bot_token: str, purpose: str) -> bytes:
    if not bot_token:
        raise InitDataError("Telegram bot token is not configured")
    key = hmac.new(b"AnnieInline:" + purpose.encode(), bot_token.encode(), hashlib.sha256).digest()
    return hmac.new(key, payload, hashlib.sha256).digest()


def issue_inline_token(user: dict, bot_token: str, *, purpose: str, ttl_seconds: int) -> str:
    """Create a compact signed inline-launch token that any app replica can verify."""
    now = int(time.time())
    payload = json.dumps({
        "v": 1, "iat": now, "exp": now + ttl_seconds,
        "nonce": secrets.token_urlsafe(12),
        "user": {"id": int(user["id"]),
                 "first_name": str(user.get("first_name") or ""),
                 "language_code": str(user.get("language_code") or "")},
    }, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
    signature = base64.urlsafe_b64encode(
        _inline_token_signature(encoded, bot_token, purpose)
    ).rstrip(b"=")
    return (encoded + b"." + signature).decode()


def validate_inline_token(token: str, bot_token: str, *, purpose: str,
                          now: int | None = None, max_lifetime: int = 3600) -> dict:
    """Validate a signed launch/session token without process-local state."""
    if not token or len(token) > 2048:
        raise InitDataError("Missing or oversized inline token")
    try:
        encoded, provided = token.encode().split(b".", 1)
        expected = base64.urlsafe_b64encode(
            _inline_token_signature(encoded, bot_token, purpose)
        ).rstrip(b"=")
        if not hmac.compare_digest(expected, provided):
            raise InitDataError("Invalid inline token signature")
        payload = json.loads(base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4)))
        current = int(time.time()) if now is None else now
        issued, expires = int(payload["iat"]), int(payload["exp"])
        user = payload["user"]
        user_id = int(user["id"])
    except InitDataError:
        raise
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise InitDataError("Malformed inline token") from exc
    if payload.get("v") != 1 or user_id <= 0 or issued > current + 30:
        raise InitDataError("Invalid inline token")
    if expires <= current or expires <= issued or expires - issued > max_lifetime:
        raise InitDataError("Expired inline token")
    return {"id": user_id, "first_name": str(user.get("first_name") or ""),
            "language_code": str(user.get("language_code") or "")}


def validate_init_data(
    raw_init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = 900,
    now: int | None = None,
) -> dict:
    """Validate Telegram's signed initData and return its trusted user object.

    The caller must pass the raw ``Telegram.WebApp.initData`` string. Values
    from ``initDataUnsafe`` must never be used as authentication credentials.
    """
    if not raw_init_data or not bot_token:
        raise InitDataError("Missing Telegram authentication data")

    try:
        pairs = parse_qsl(raw_init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise InitDataError("Malformed Telegram authentication data") from exc

    # Reject duplicate keys so parsing and signature verification cannot
    # interpret the same payload differently.
    data: dict[str, str] = {}
    for key, value in pairs:
        if key in data:
            raise InitDataError("Duplicate Telegram authentication field")
        data[key] = value

    received_hash = data.pop("hash", "")
    if not received_hash:
        raise InitDataError("Missing Telegram signature")

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(data.items())
    )
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    calculated_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        raise InitDataError("Invalid Telegram signature")

    try:
        auth_date = int(data["auth_date"])
        user = json.loads(data["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InitDataError("Incomplete Telegram authentication data") from exc

    current_time = int(time.time()) if now is None else now
    if auth_date > current_time + 30 or current_time - auth_date > max_age_seconds:
        raise InitDataError("Telegram authentication data has expired")
    if user_id <= 0:
        raise InitDataError("Invalid Telegram user")

    # Return only the fields the API needs; don't propagate arbitrary signed
    # payload fields into application code.
    return {
        "id": user_id,
        "first_name": str(user.get("first_name") or ""),
        "language_code": str(user.get("language_code") or ""),
    }
