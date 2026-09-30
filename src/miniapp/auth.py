"""Telegram Mini App request authentication."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class InitDataError(ValueError):
    """Raised when Telegram Mini App initData is missing, stale, or invalid."""


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
