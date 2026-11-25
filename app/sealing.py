"""Stateless sealed tokens.

Everything the OAuth server hands out (client IDs, auth codes, access and
refresh tokens, the Google sign-in state) is a small JSON payload encrypted
and signed with one secret key (Fernet: AES-128-CBC + HMAC-SHA256). This means
the server needs no database: restarting it never logs anyone out, and the
Google refresh token is only ever stored encrypted, inside tokens Claude holds.

Rotating TOKEN_KEY signs everyone out at once.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import zlib
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class Sealer:
    def __init__(self, key: str):
        try:
            self._fernet = Fernet(key.encode())
        except ValueError:
            # Not a Fernet key: derive one from any long random string.
            if len(key) < 32:
                raise ValueError("TOKEN_KEY must be at least 32 characters") from None
            self._fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))

    def seal(self, kind: str, payload: dict[str, Any], ttl: int | None) -> str:
        body = {"k": kind, "p": payload}
        if ttl is not None:
            body["e"] = int(time.time()) + ttl
        raw = zlib.compress(json.dumps(body, separators=(",", ":")).encode())
        token = self._fernet.encrypt(raw)
        # Fernet output is already urlsafe base64; strip padding for shorter strings.
        return token.decode().rstrip("=")

    def unseal(self, kind: str, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        padded = token + "=" * (-len(token) % 4)
        try:
            raw = self._fernet.decrypt(padded.encode())
            body = json.loads(zlib.decompress(raw))
        except (InvalidToken, ValueError, zlib.error, base64.binascii.Error):
            return None
        if body.get("k") != kind:
            return None
        exp = body.get("e")
        if exp is not None and exp < time.time():
            return None
        return body.get("p")
