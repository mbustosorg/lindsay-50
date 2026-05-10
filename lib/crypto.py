"""Fernet encryption for sensitive config fields.

Sensitive fields are encrypted before writing to SQLite/S3 and decrypted on read.
Key is derived from CONFIG_MASTER_KEY env var via PBKDF2.

Usage:
    from lib.crypto import Crypto
    crypto = Crypto()              # raises if master key missing
    encrypted = crypto.encrypt("my-secret-value")
    decrypted = crypto.decrypt(encrypted)
"""

import hashlib
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

# Fields in config that are encrypted
SENSITIVE_FIELDS = {"AIO_KEY", "S3_BUCKET", "S3_MESSAGE_LOG_KEY", "S3_CONFIG_PREFIX"}


class Crypto:
    """Fernet encryption using a PBKDF2-derived key from CONFIG_MASTER_KEY."""

    KEY_ENV = "CONFIG_MASTER_KEY"
    PBKDF2_ITERATIONS = 480_000  # OWASP 2023 recommendation for PBKDF2-SHA256

    def __init__(self, master_key: Optional[str] = None):
        self._key: Optional[bytes] = None
        if master_key is None:
            master_key = os.environ.get(self.KEY_ENV)
        if master_key:
            self._key = self._derive_key(master_key)
            self._fernet = Fernet(self._key)
        else:
            self._fernet = None

    def _derive_key(self, master_key: str) -> bytes:
        """Derive a Fernet-compatible key from the master password using PBKDF2-SHA256."""
        import base64
        salt = b"lindsay-heart-v1"  # static salt — key is stored securely in env
        raw = hashlib.pbkdf2_hmac(
            "sha256",
            master_key.encode(),
            salt,
            self.PBKDF2_ITERATIONS,
            dklen=32,
        )
        # Fernet requires a 32-byte url-safe base64-encoded key
        return base64.urlsafe_b64encode(raw)

    @property
    def available(self) -> bool:
        """True if a master key is configured."""
        return self._fernet is not None

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a string, returning a base64-encoded Fernet token."""
        if not self._fernet:
            raise RuntimeError(
                f"CONFIG_MASTER_KEY not set; cannot encrypt. "
                f"Set the {self.KEY_ENV} environment variable."
            )
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        """Decrypt a Fernet token, returning the plaintext string."""
        if not self._fernet:
            raise RuntimeError(f"CONFIG_MASTER_KEY not set; cannot decrypt.")
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as e:
            raise ValueError(f"Failed to decrypt token: {e}") from e


# ---------------------------------------------------------------------------
# Config-level encrypt/decrypt helpers
# ---------------------------------------------------------------------------

def encrypt_value(value: str, crypto: Crypto) -> str:
    """Encrypt a single sensitive config value."""
    return crypto.encrypt(value)


def decrypt_value(token: str, crypto: Crypto) -> str:
    """Decrypt a single sensitive config token."""
    return crypto.decrypt(token)


def is_encrypted_token(value: str) -> bool:
    """Return True if the value looks like a Fernet token (base64, starts with g)."""
    if not value:
        return False
    try:
        return value.startswith("gAAAAAB") and len(value) > 50
    except Exception:
        return False


def encrypt_dict(d: dict, crypto: Crypto, fields: set[str] = SENSITIVE_FIELDS) -> dict:
    """Recursively encrypt sensitive string fields in a dict (returns a copy)."""
    result = {}
    for k, v in d.items():
        if k in fields and isinstance(v, str) and not is_encrypted_token(v):
            result[k] = encrypt_value(v, crypto)
        elif isinstance(v, dict):
            result[k] = encrypt_dict(v, crypto, fields)
        elif isinstance(v, list):
            result[k] = [
                encrypt_dict(item, crypto, fields) if isinstance(item, dict) else item
                for item in v
            ]
        else:
            result[k] = v
    return result


def decrypt_dict(d: dict, crypto: Crypto, fields: set[str] = SENSITIVE_FIELDS) -> dict:
    """Recursively decrypt sensitive string fields in a dict (returns a copy)."""
    result = {}
    for k, v in d.items():
        if k in fields and isinstance(v, str) and is_encrypted_token(v):
            result[k] = decrypt_value(v, crypto)
        elif isinstance(v, dict):
            result[k] = decrypt_dict(v, crypto, fields)
        elif isinstance(v, list):
            result[k] = [
                decrypt_dict(item, crypto, fields) if isinstance(item, dict) else item
                for item in v
            ]
        else:
            result[k] = v
    return result
