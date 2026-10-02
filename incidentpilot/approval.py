"""Signed, expiring, single-use approval tokens for write actions.

A human mints a token for one exact action (service + target revision). The
remediation tool refuses to act without a valid one, so even a hijacked agent
can't roll anything back on its own.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

TTL_S = 600
_used: set[str] = set()  # ponytail: in-memory, forgotten on restart; the 10-minute expiry bounds the replay window.


def _secret() -> bytes:
    return os.environ.get("APPROVAL_SECRET", "dev-approval-secret").encode()


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def mint(service: str, to_revision: str, ttl_s: int = TTL_S) -> str:
    body = json.dumps({"service": service, "to_revision": to_revision, "exp": int(time.time()) + ttl_s})
    payload = base64.urlsafe_b64encode(body.encode()).decode()
    return f"{payload}.{_sign(payload)}"


def verify(token: str, service: str, to_revision: str) -> None:
    """Raise PermissionError unless the token approves exactly this action."""
    payload, _, sig = token.partition(".")
    if not sig or not hmac.compare_digest(sig, _sign(payload)):
        raise PermissionError("approval token signature is invalid")
    claims = json.loads(base64.urlsafe_b64decode(payload))
    if claims["exp"] < time.time():
        raise PermissionError("approval token has expired")
    if (claims["service"], claims["to_revision"]) != (service, to_revision):
        raise PermissionError(
            f"approval token is for {claims['service']} -> {claims['to_revision']}, "
            f"not {service} -> {to_revision}"
        )
    if sig in _used:
        raise PermissionError("approval token was already used")
    _used.add(sig)
