"""Tier 1 tests — auth whitelist and encrypted state.

`is_allowed` is the only thing standing between an unauthorized Telegram user
and the bot's commands, and `secure_save`/`secure_load` protect persisted
state. Both fail in dangerous, silent ways if broken, so they get round-trip
coverage including the plaintext backward-compat path.
"""
import base64

import pytest
from cryptography.fernet import Fernet

import security as sec


# ── whitelist ────────────────────────────────────────────────────

def test_build_whitelist_primary_only(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    monkeypatch.delenv("ALLOWED_CHAT_IDS", raising=False)
    assert sec._build_whitelist() == {"111"}


def test_build_whitelist_with_extras(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "222, 333 ,")  # spaces + trailing comma
    assert sec._build_whitelist() == {"111", "222", "333"}


def test_build_whitelist_empty(monkeypatch):
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("ALLOWED_CHAT_IDS", raising=False)
    assert sec._build_whitelist() == set()


def test_is_allowed_membership(monkeypatch):
    monkeypatch.setattr(sec, "_WHITELIST", {"111"})
    assert sec.is_allowed("111") is True
    assert sec.is_allowed(111) is True  # int coerced to str
    assert sec.is_allowed("999") is False


# ── Fernet key derivation ────────────────────────────────────────

def test_get_fernet_real_fernet_key(monkeypatch):
    key = Fernet.generate_key().decode()  # 44 chars ending '='
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    f = sec._get_fernet()
    assert f is not None
    # A token encrypted with the same raw key decrypts back.
    token = f.encrypt(b"hello")
    assert Fernet(key.encode()).decrypt(token) == b"hello"


def test_get_fernet_passphrase_uses_pbkdf2_and_is_deterministic(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", "my-secret-passphrase")
    f = sec._get_fernet()
    assert f is not None
    # Enkripsi sekarang pakai PBKDF2 (bukan SHA-256 polos). Verifikasi token
    # yang dibuat modul bisa didekripsi dengan key PBKDF2 yang diturunkan mandiri.
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=sec._KDF_SALT, iterations=sec._KDF_ITERATIONS)
    pbkdf2_key = base64.urlsafe_b64encode(kdf.derive(b"my-secret-passphrase"))
    token = f.encrypt(b"payload")
    assert Fernet(pbkdf2_key).decrypt(token) == b"payload"
    # Derivasi harus deterministik lintas instance (passphrase sama → key sama).
    assert sec._get_fernet().decrypt(token) == b"payload"


def test_get_fernet_decrypts_legacy_sha256_tokens(monkeypatch):
    """Backward-compat: file lama yang dienkripsi dengan key SHA-256 (legacy)
    harus tetap bisa didekripsi (MultiFernet) supaya state tidak hilang."""
    monkeypatch.setenv("ENCRYPTION_KEY", "my-secret-passphrase")
    import hashlib
    legacy_key = base64.urlsafe_b64encode(
        hashlib.sha256(b"my-secret-passphrase").digest())
    legacy_token = Fernet(legacy_key).encrypt(b"old-state")
    f = sec._get_fernet()
    assert f.decrypt(legacy_token) == b"old-state"


# ── secure_save / secure_load round-trip ─────────────────────────

@pytest.fixture
def enc_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())


def test_round_trip_encrypts_and_decrypts(tmp_path, enc_key):
    path = str(tmp_path / "state.json")
    data = {"capital": 1000, "nested": {"a": [1, 2, 3]}}

    assert sec.secure_save(path, data) is True
    # Encrypted file written, plaintext not left behind.
    assert (tmp_path / "state.json.enc").exists()
    assert not (tmp_path / "state.json").exists()

    assert sec.secure_load(path) == data


def test_encrypted_file_is_not_plaintext(tmp_path, enc_key):
    path = str(tmp_path / "secret.json")
    sec.secure_save(path, {"token": "supersecret"})
    blob = (tmp_path / "secret.json.enc").read_bytes()
    assert b"supersecret" not in blob


def test_secure_load_missing_returns_default(tmp_path, enc_key):
    default = {"fresh": True}
    assert sec.secure_load(str(tmp_path / "nope.json"), default=default) == default


def test_secure_load_migrates_plaintext(tmp_path, enc_key):
    # Simulate a legacy plaintext state file.
    import json
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({"legacy": 1}))

    loaded = sec.secure_load(str(path))
    assert loaded == {"legacy": 1}
    # After migration the encrypted version exists and plaintext is gone.
    assert (tmp_path / "legacy.json.enc").exists()
    assert not path.exists()


def test_wrong_key_fails_to_decrypt_returns_default(tmp_path, monkeypatch):
    path = str(tmp_path / "state.json")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    sec.secure_save(path, {"x": 1})

    # Rotate to a different key: decryption fails -> default returned, no crash.
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    assert sec.secure_load(path, default={"d": 0}) == {"d": 0}
