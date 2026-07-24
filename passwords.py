"""Password hashing for toilet-local operator accounts."""

from __future__ import annotations

import bcrypt


MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_BYTES = 72


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"operator password must contain at least {MIN_PASSWORD_LENGTH} characters"
        )
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(
            f"operator password must be at most {MAX_PASSWORD_BYTES} UTF-8 bytes"
        )


def hash_password(password: str) -> str:
    validate_password(password)
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode(
        "ascii"
    )


def verify_password(password: str, password_hash: str) -> bool:
    try:
        encoded = password.encode("utf-8")
        if len(encoded) > MAX_PASSWORD_BYTES:
            return False
        return bcrypt.checkpw(encoded, password_hash.encode("ascii"))
    except (ValueError, TypeError, UnicodeEncodeError, UnicodeDecodeError):
        return False
