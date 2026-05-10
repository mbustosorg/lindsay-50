"""Tests for lib/crypto.py."""

import os
import pytest
from lib.crypto import (
    Crypto,
    encrypt_dict,
    decrypt_dict,
    is_encrypted_token,
)


class TestCrypto:
    def test_encrypt_decrypt_roundtrip(self):
        c = Crypto(master_key="test-master-password-123")
        original = "my-super-secret-key"
        token = c.encrypt(original)
        assert token != original
        assert c.decrypt(token) == original

    def test_encrypted_token_looks_like_fernet(self):
        c = Crypto(master_key="test")
        token = c.encrypt("hello")
        assert token.startswith("gAAAAAB")

    def test_different_master_keys_produce_different_tokens(self):
        c1 = Crypto(master_key="key1")
        c2 = Crypto(master_key="key2")
        token1 = c1.encrypt("same plaintext")
        token2 = c2.encrypt("same plaintext")
        assert token1 != token2

    def test_decrypt_wrong_key_raises(self):
        c1 = Crypto(master_key="key1")
        c2 = Crypto(master_key="key2")
        token = c1.encrypt("secret")
        with pytest.raises(ValueError, match="Failed to decrypt"):
            c2.decrypt(token)

    def test_no_key_available(self):
        c = Crypto(master_key=None)
        assert not c.available
        with pytest.raises(RuntimeError, match="CONFIG_MASTER_KEY not set"):
            c.encrypt("something")

    def test_decrypt_no_key_raises(self):
        c = Crypto(master_key=None)
        assert not c.available
        with pytest.raises(RuntimeError, match="CONFIG_MASTER_KEY not set"):
            c.decrypt("gAAAAABxxx")

    def test_environ_key_not_set(self):
        # When env var is not set and no key passed
        old = os.environ.pop("CONFIG_MASTER_KEY", None)
        try:
            c = Crypto()  # no master key
            assert not c.available
        finally:
            if old is not None:
                os.environ["CONFIG_MASTER_KEY"] = old


class TestIsEncryptedToken:
    def test_fernet_token(self):
        # Real Fernet token (100 chars, starts with gAAAAAB)
        real_token = "gAAAAABp_qUUbW5dy9T8eALNI293EyKHRarWRdE83NbLsF_Ai4dSADd9mZBjcyNn-xWpsJwzrjcrOV61Lf_ufWq0goveEeanRQ=="
        assert is_encrypted_token(real_token)

    def test_plaintext_not_token(self):
        assert not is_encrypted_token("hello world")
        assert not is_encrypted_token("")
        assert not is_encrypted_token("just a short string")


class TestEncryptDict:
    def test_encrypts_sensitive_fields(self):
        c = Crypto(master_key="test")
        d = {"AIO_KEY": "my-aio-key", "body": "hello", "sender": "+15551234567"}
        result = encrypt_dict(d, c)
        assert result["AIO_KEY"] != "my-aio-key"
        assert result["AIO_KEY"].startswith("gAAAAAB")
        assert result["body"] == "hello"  # not sensitive
        assert result["sender"] == "+15551234567"  # not in SENSITIVE_FIELDS

    def test_nested_dict(self):
        c = Crypto(master_key="test")
        d = {"rendering": {"AIO_KEY": "secret"}}
        result = encrypt_dict(d, c)
        assert result["rendering"]["AIO_KEY"].startswith("gAAAAAB")

    def test_list_of_dicts(self):
        c = Crypto(master_key="test")
        d = {"allowed_senders": [{"AIO_KEY": "s1"}, {"AIO_KEY": "s2"}]}
        result = encrypt_dict(d, c)
        assert result["allowed_senders"][0]["AIO_KEY"].startswith("gAAAAAB")
        assert result["allowed_senders"][1]["AIO_KEY"].startswith("gAAAAAB")

    def test_already_encrypted_not_re_encrypted(self):
        c = Crypto(master_key="test")
        token = c.encrypt("already-encrypted")
        d = {"AIO_KEY": token}
        result = encrypt_dict(d, c)
        assert result["AIO_KEY"] == token  # not double-encrypted


class TestDecryptDict:
    def test_decrypts_sensitive_fields(self):
        c = Crypto(master_key="test")
        d = {"AIO_KEY": c.encrypt("my-aio-key"), "body": "hello"}
        result = decrypt_dict(d, c)
        assert result["AIO_KEY"] == "my-aio-key"
        assert result["body"] == "hello"

    def test_nested_dict(self):
        c = Crypto(master_key="test")
        d = {"rendering": {"AIO_KEY": c.encrypt("secret")}}
        result = decrypt_dict(d, c)
        assert result["rendering"]["AIO_KEY"] == "secret"

    def test_plaintext_fields_unchanged(self):
        c = Crypto(master_key="test")
        d = {"body": "hello", "sender": "+15551234567"}
        result = decrypt_dict(d, c)
        assert result == d

    def test_roundtrip(self):
        c = Crypto(master_key="test")
        original = {
            "version": 1,
            "AIO_KEY": "super-secret",
            "S3_BUCKET": "my-bucket",
            "allowed_senders": [
                {"name": "Alice", "phone": "+15551234567"},
            ],
            "rendering": {"mode": "scroll"},
            "filters": [],
        }
        encrypted = encrypt_dict(original, c)
        decrypted = decrypt_dict(encrypted, c)
        assert decrypted["AIO_KEY"] == "super-secret"
        assert decrypted["S3_BUCKET"] == "my-bucket"
        assert decrypted["allowed_senders"][0]["phone"] == "+15551234567"
        assert decrypted["rendering"]["mode"] == "scroll"
