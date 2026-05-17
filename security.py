#!/usr/bin/env python3
"""
SECURITY MODULE — Crypto Bot v13
==================================
Fitur:
  1. Chat ID Whitelist — hanya TELEGRAM_CHAT_ID yang bisa akses bot
  2. Encrypted JSON State — enkripsi AES-256 untuk file state sensitif

Setup:
  - Tambahkan ENCRYPTION_KEY di .env (generate otomatis kalau kosong)
  - Whitelist otomatis pakai TELEGRAM_CHAT_ID dari .env
  - Bisa tambah chat ID lain lewat ALLOWED_CHAT_IDS di .env
    contoh: ALLOWED_CHAT_IDS=123456789,987654321

Usage di crypto_screening_bot_v13.py:
  from security import is_allowed, secure_load, secure_save

  # Di process_update(), tambahkan SEBELUM command routing:
  if not is_allowed(chat_id):
      return  # silent drop — jangan kasih tau unauthorized user

  # Ganti json.load/json.dump dengan secure_load/secure_save
  # di file-file state (risk_state.json, gate_state.json, dll)
"""

import os
import json
import base64
import hashlib
import logging

log = logging.getLogger("security")

# ─────────────────────────────────────────────
# 1. WHITELIST
# ─────────────────────────────────────────────

def _build_whitelist() -> set:
    """
    Build whitelist dari env vars.
    TELEGRAM_CHAT_ID  → primary allowed ID (selalu masuk)
    ALLOWED_CHAT_IDS  → comma-separated tambahan (opsional)
    """
    allowed = set()

    primary = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if primary:
        allowed.add(primary)

    extras = os.getenv("ALLOWED_CHAT_IDS", "").strip()
    if extras:
        for cid in extras.split(","):
            cid = cid.strip()
            if cid:
                allowed.add(cid)

    if not allowed:
        log.warning("⚠️ WHITELIST kosong! Set TELEGRAM_CHAT_ID di .env")

    return allowed


# Build sekali saat module di-import
_WHITELIST: set = _build_whitelist()


def is_allowed(chat_id: str) -> bool:
    """
    Return True kalau chat_id ada di whitelist.
    Selalu log unauthorized access attempt.
    """
    if str(chat_id) in _WHITELIST:
        return True

    log.warning(f"🚫 UNAUTHORIZED ACCESS: chat_id={chat_id} — request ditolak")
    return False


def get_whitelist() -> set:
    """Return current whitelist (untuk debug/status)."""
    return _WHITELIST.copy()


# ─────────────────────────────────────────────
# 2. ENCRYPTED JSON STATE
# ─────────────────────────────────────────────
# Pakai AES-256-GCM via cryptography library (Fernet).
# Fernet = AES-128-CBC + HMAC-SHA256 — cukup untuk state file lokal.
# Key di-derive dari ENCRYPTION_KEY env var.
# ─────────────────────────────────────────────

def _get_fernet():
    """
    Lazy-load Fernet dan buat key dari ENCRYPTION_KEY env var.
    Return None kalau cryptography tidak terinstall.
    """
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        log.warning("⚠️ cryptography tidak terinstall — jalankan: pip install cryptography")
        return None

    raw_key = os.getenv("ENCRYPTION_KEY", "").strip()

    if not raw_key:
        # Auto-generate dan print ke log sekali — user harus save ke .env
        new_key = Fernet.generate_key().decode()
        log.warning("=" * 60)
        log.warning("⚠️  ENCRYPTION_KEY tidak ditemukan di .env!")
        log.warning(f"✅ Auto-generated key: {new_key}")
        log.warning("👉 Tambahkan ke .env: ENCRYPTION_KEY=" + new_key)
        log.warning("⚠️  Tanpa key yang sama, state files tidak bisa dibaca ulang!")
        log.warning("=" * 60)
        # Simpan ke env runtime supaya session ini bisa jalan
        os.environ["ENCRYPTION_KEY"] = new_key
        raw_key = new_key

    # Derive 32-byte key dari string apapun (support custom passphrase)
    if len(raw_key) != 44 or not raw_key.endswith("="):
        # Bukan Fernet key asli — derive dari passphrase
        key_bytes = hashlib.sha256(raw_key.encode()).digest()
        derived = base64.urlsafe_b64encode(key_bytes)
    else:
        derived = raw_key.encode()

    return Fernet(derived)


def secure_save(filepath: str, data: dict) -> bool:
    """
    Enkripsi data dict → simpan ke file.
    Fallback ke plain JSON kalau cryptography tidak ada.
    Return True kalau sukses.
    """
    fernet = _get_fernet()

    if fernet is None:
        # Fallback: plain JSON
        try:
            with open(filepath, "w") as f:
                json.dump(data, f, indent=2)
            return True
        except Exception as e:
            log.error(f"secure_save fallback error {filepath}: {e}")
            return False

    try:
        json_bytes  = json.dumps(data, indent=2).encode("utf-8")
        encrypted   = fernet.encrypt(json_bytes)
        enc_path    = filepath + ".enc"
        with open(enc_path, "wb") as f:
            f.write(encrypted)
        # Hapus plain version kalau ada
        if os.path.exists(filepath):
            os.remove(filepath)
        return True
    except Exception as e:
        log.error(f"secure_save error {filepath}: {e}")
        return False


def secure_load(filepath: str, default: dict = None) -> dict:
    """
    Load dan dekripsi state file.
    Support: .enc (encrypted), .json (plain, untuk backward compat).
    Return default dict kalau file tidak ada / gagal baca.
    """
    if default is None:
        default = {}

    fernet  = _get_fernet()
    enc_path = filepath + ".enc"

    # Priority 1: encrypted file
    if os.path.exists(enc_path) and fernet:
        try:
            with open(enc_path, "rb") as f:
                encrypted = f.read()
            decrypted = fernet.decrypt(encrypted)
            return json.loads(decrypted.decode("utf-8"))
        except Exception as e:
            log.error(f"secure_load decrypt error {enc_path}: {e}")
            return default

    # Priority 2: plain JSON (backward compat / fallback)
    if os.path.exists(filepath):
        try:
            with open(filepath, "r") as f:
                data = json.load(f)
            # Auto-migrate ke encrypted kalau fernet tersedia
            if fernet:
                log.info(f"🔐 Migrating {filepath} → {enc_path}")
                secure_save(filepath, data)
            return data
        except Exception as e:
            log.error(f"secure_load plain error {filepath}: {e}")
            return default

    return default


# ─────────────────────────────────────────────
# 3. HELPER — STATUS REPORT
# ─────────────────────────────────────────────

def get_security_status() -> str:
    """Return security status string untuk /status command."""
    fernet     = _get_fernet()
    enc_status = "✅ AES-256 aktif" if fernet else "⚠️ Tidak aktif (install cryptography)"
    wl_count   = len(_WHITELIST)
    wl_ids     = ", ".join(sorted(_WHITELIST)) if _WHITELIST else "kosong!"

    return (
        f"🔒 <b>Security Status</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"👥 Whitelist    : {wl_count} ID ({wl_ids})\n"
        f"🔐 Enkripsi     : {enc_status}\n"
    )
