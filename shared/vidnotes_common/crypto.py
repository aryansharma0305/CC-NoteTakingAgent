"""Fernet encryption of the Notion token while it travels inside queue messages.

The token is never written to Postgres; it exists only (encrypted) in the
message body and in worker memory.
"""

from __future__ import annotations

import hashlib

from cryptography.fernet import Fernet, InvalidToken


class TokenCipher:
    def __init__(self, key: str):
        if not key:
            raise ValueError("TOKEN_ENC_KEY is not set (generate one with: python -c "
                             "\"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\")")
        self._f = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, token: str) -> str:
        return self._f.encrypt(token.encode()).decode()

    def decrypt(self, blob: str) -> str:
        try:
            return self._f.decrypt(blob.encode()).decode()
        except InvalidToken as e:  # pragma: no cover - defensive
            raise ValueError("could not decrypt Notion token (TOKEN_ENC_KEY mismatch?)") from e


def token_fingerprint(token: str) -> str:
    """Stable, non-reversible id for a token (rate-limit bucket keys, logs)."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def redact(token: str | None) -> str:
    if not token:
        return "<none>"
    return f"{token[:6]}…({token_fingerprint(token)})"
