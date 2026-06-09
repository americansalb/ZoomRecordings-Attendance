"""
Zoom Meeting SDK signature (JWT, HS256).

The Web Meeting SDK requires a short-lived JWT signed with your SDK secret to
authorize a join. We build it with stdlib only (no PyJWT dependency) so it's
trivially testable.

Claim shape per Zoom's Meeting SDK auth docs:
  appKey / sdkKey, mn (meeting number), role (0=attendee, 1=host),
  iat, exp, tokenExp.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def meeting_sdk_signature(
    sdk_key: str,
    sdk_secret: str,
    meeting_number: str,
    role: int = 0,
    expire_seconds: int = 7200,
) -> str:
    if not sdk_key or not sdk_secret:
        raise ValueError("SDK key/secret required to sign a join")

    iat = int(time.time()) - 30          # small skew allowance
    exp = iat + max(1800, expire_seconds)  # Zoom requires >= 30 min

    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "appKey": sdk_key,
        "sdkKey": sdk_key,
        "mn": str(meeting_number),
        "role": role,
        "iat": iat,
        "exp": exp,
        "tokenExp": exp,
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    sig = hmac.new(sdk_secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return signing_input + "." + _b64url(sig)
