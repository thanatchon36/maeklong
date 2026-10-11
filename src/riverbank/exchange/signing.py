"""Request signing (HMAC-SHA256, RSA PKCS#1 v1.5 SHA-256, Ed25519).

Secrets come only from the environment / key files and are never logged:
  BINANCE_API_KEY            (required)
  BINANCE_API_SECRET         (HMAC)
  BINANCE_PRIVATE_KEY_PATH   (RSA or Ed25519 PEM, PKCS#8) [+ BINANCE_PRIVATE_KEY_PASSWORD]
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlencode


class Signer(Protocol):
    kind: str

    def sign(self, payload: str) -> str: ...


class HmacSigner:
    kind = "hmac"

    def __init__(self, secret: str) -> None:
        self._secret = secret.encode()

    def sign(self, payload: str) -> str:
        return hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()

    def __repr__(self) -> str:
        return "HmacSigner(<redacted>)"


class _PemSigner:
    kind = "pem"

    def __init__(self, pem: bytes, password: bytes | None = None) -> None:
        from cryptography.hazmat.primitives import serialization

        self._key = serialization.load_pem_private_key(pem, password=password)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"


class RsaSigner(_PemSigner):
    kind = "rsa"

    def sign(self, payload: str) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        assert isinstance(self._key, rsa.RSAPrivateKey)
        sig = self._key.sign(payload.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(sig).decode()


class Ed25519Signer(_PemSigner):
    kind = "ed25519"

    def sign(self, payload: str) -> str:
        from cryptography.hazmat.primitives.asymmetric import ed25519

        assert isinstance(self._key, ed25519.Ed25519PrivateKey)
        return base64.b64encode(self._key.sign(payload.encode("ascii"))).decode()


def signer_from_pem(pem: bytes, password: bytes | None = None) -> Signer:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

    key = serialization.load_pem_private_key(pem, password=password)
    if isinstance(key, rsa.RSAPrivateKey):
        return RsaSigner(pem, password)
    if isinstance(key, ed25519.Ed25519PrivateKey):
        return Ed25519Signer(pem, password)
    raise ValueError("unsupported private key type (need RSA or Ed25519)")


@dataclass(frozen=True)
class Credentials:
    api_key: str
    signer: Signer

    def __repr__(self) -> str:
        return f"Credentials(api_key=<redacted>, signer={self.signer.kind})"


def credentials_from_env(env: Mapping[str, str] | None = None) -> Credentials | None:
    env = os.environ if env is None else env
    api_key = env.get("BINANCE_API_KEY", "")
    if not api_key:
        return None
    secret = env.get("BINANCE_API_SECRET", "")
    key_path = env.get("BINANCE_PRIVATE_KEY_PATH", "")
    if key_path:
        pw = env.get("BINANCE_PRIVATE_KEY_PASSWORD")
        return Credentials(api_key, signer_from_pem(Path(key_path).read_bytes(), pw.encode() if pw else None))
    if secret:
        return Credentials(api_key, HmacSigner(secret))
    raise ValueError("BINANCE_API_KEY set but neither BINANCE_API_SECRET nor BINANCE_PRIVATE_KEY_PATH")


def signed_query(params: Mapping[str, object], signer: Signer) -> str:
    """Encode params in order, sign, and append `signature` last (URL-encoded)."""
    payload = urlencode([(k, _param(v)) for k, v in params.items() if v is not None])
    sig = signer.sign(payload)
    return f"{payload}&signature={quote(sig, safe='')}"


def _param(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)
