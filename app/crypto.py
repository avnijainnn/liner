"""Canonical hashing, Ed25519 signing envelopes, secret encryption, passwords.

Canonical form (documented, one method): JSON, keys sorted, no insignificant
whitespace, UTF-8, no NaN/Infinity. sha256 is over exactly those bytes.
The signature lives in a separate envelope and never inside the hashed body.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature

from . import config


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def hash_policy(body: dict) -> str:
    return sha256_hex(canonical(body))


# -- secret encryption (tokens, private keys) ------------------------------
def _fernet() -> Fernet:
    key = hashlib.sha256(('vaada-secrets:' + config.secret_key()).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(s: str | None) -> str | None:
    if not s:
        return None
    return _fernet().encrypt(s.encode()).decode()


def decrypt(s: str | None) -> str | None:
    if not s:
        return None
    try:
        return _fernet().decrypt(s.encode()).decode()
    except InvalidToken:
        return None


# -- Ed25519 ---------------------------------------------------------------
def new_keypair() -> tuple[str, str]:
    """Returns (public_b64, private_b64). Store the private half encrypted."""
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    pub = base64.b64encode(pk.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    priv = base64.b64encode(sk.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                             serialization.NoEncryption())).decode()
    return pub, priv


def sign_hash(private_b64: str, sha256: str) -> str:
    sk = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_b64))
    return 'ed25519:' + base64.b64encode(sk.sign(bytes.fromhex(sha256))).decode()


def verify_hash(public_b64: str, sha256: str, signature: str) -> bool:
    if not signature.startswith('ed25519:'):
        return False
    try:
        pk = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64))
        pk.verify(base64.b64decode(signature[8:]), bytes.fromhex(sha256))
        return True
    except (InvalidSignature, ValueError):
        return False


# -- passwords / tokens ----------------------------------------------------
def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return 'scrypt$' + base64.b64encode(salt).decode() + '$' + base64.b64encode(dk).decode()


def check_password(pw: str, stored: str) -> bool:
    try:
        _, s, d = stored.split('$')
        dk = hashlib.scrypt(pw.encode(), salt=base64.b64decode(s), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(dk, base64.b64decode(d))
    except Exception:
        return False


def token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(t: str) -> str:
    return sha256_hex('vaada-token:' + t)
