"""
Encrypt / decrypt Bunny credentials at rest.

The Bunny API key and Security key live on the singleton ``BunnyConfiguration``
row in plaintext is unacceptable: a database dump (or a careless screen-share
of Django admin) would leak playback-signing material.

Strategy mirrors Cubite's ``secretManager``: AES-equivalent symmetric
encryption with a key derived from a project-wide secret. We use Fernet
(AES-128-CBC + HMAC) and derive the key from ``settings.SECRET_KEY`` via HKDF —
so no extra environment variable is needed and key rotation is the same
operation as rotating Django's secret.

Trade-off (documented in the README): rotating ``SECRET_KEY`` invalidates
existing ciphertext. Admins recover by re-pasting credentials in Django admin.
"""

import base64

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

# Static info parameter for HKDF. Kept stable so a given SECRET_KEY always
# produces the same Fernet key — required for round-tripping ciphertext
# across process restarts.
_HKDF_INFO = b"xblock-bunny:fernet:v1"


def _fernet() -> Fernet:
    """Derive a Fernet key from ``settings.SECRET_KEY`` and return a Fernet."""
    secret = settings.SECRET_KEY
    if not secret:
        raise RuntimeError(
            "xblock-bunny: settings.SECRET_KEY is empty; cannot derive an "
            "encryption key. Refusing to store credentials in plaintext."
        )
    raw_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_HKDF_INFO,
    ).derive(secret.encode("utf-8") if isinstance(secret, str) else secret)
    # Fernet wants a urlsafe-base64-encoded 32-byte key.
    return Fernet(base64.urlsafe_b64encode(raw_key))


def encrypt(plaintext: str) -> str:
    """Encrypt a string; returns a urlsafe-base64 token suitable for TextField storage."""
    if not plaintext:
        raise ValueError("xblock-bunny.crypto.encrypt: refusing to encrypt empty string")
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """
    Decrypt a ciphertext produced by :func:`encrypt`.

    Returns an empty string on failure rather than raising — callers
    (models / load_config) translate that into the "needs re-paste" error
    state the admin sees, mirroring Cubite's ``BunnyKeyUndecryptableError``
    flow. Logged so the misconfiguration is visible.
    """
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return ""
