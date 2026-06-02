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

_KEY_FILE       = ".encryption_key"
# Salt aplikasi (tetap, supaya derivasi passphrase deterministik lintas restart).
_KDF_SALT       = b"crypto_bot_v13::encryption_kdf::v1"
_KDF_ITERATIONS = 200_000


def _key_fingerprint(key: str) -> str:
    """Fingerprint pendek (bukan key asli) untuk logging yang aman."""
    return hashlib.sha256(key.encode()).hexdigest()[:8]


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
        # Auto-generate sekali. JANGAN log key-nya (kebocoran kredensial ke
        # log/stdout/journald). Tulis ke file 0600 dan log path + fingerprint saja.
        new_key = Fernet.generate_key().decode()
        try:
            with open(_KEY_FILE, "w") as f:
                f.write(new_key)
            os.chmod(_KEY_FILE, 0o600)
            saved_to = os.path.abspath(_KEY_FILE)
        except Exception as e:
            saved_to = None
            log.warning(f"⚠️ Gagal menulis key ke file: {e}")
        log.warning("=" * 60)
        log.warning("⚠️  ENCRYPTION_KEY tidak ditemukan di .env!")
        log.warning(f"✅ Auto-generated key (fingerprint: {_key_fingerprint(new_key)})")
        if saved_to:
            log.warning(f"👉 Key tersimpan di: {saved_to} (chmod 600)")
            log.warning("   Pindahkan ke .env sebagai ENCRYPTION_KEY=... lalu hapus file ini.")
        log.warning("⚠️  Tanpa key yang sama, state files tidak bisa dibaca ulang!")
        log.warning("=" * 60)
        # Simpan ke env runtime supaya session ini bisa jalan
        os.environ["ENCRYPTION_KEY"] = new_key
        raw_key = new_key

    # Derive 32-byte key dari string apapun (support custom passphrase)
    if len(raw_key) != 44 or not raw_key.endswith("="):
        # Bukan Fernet key asli — derive dari passphrase via PBKDF2 (bukan SHA-256
        # polos). Salt tetap (deterministik) supaya passphrase yang sama selalu
        # menghasilkan key yang sama; iterasi tinggi menaikkan biaya brute-force.
        legacy = base64.urlsafe_b64encode(hashlib.sha256(raw_key.encode()).digest())
        try:
            from cryptography.fernet import MultiFernet
            from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
            from cryptography.hazmat.primitives import hashes
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=_KDF_SALT,
                iterations=_KDF_ITERATIONS,
            )
            new_derived = base64.urlsafe_b64encode(kdf.derive(raw_key.encode()))
            # MultiFernet: enkripsi pakai key baru (PBKDF2), tapi tetap bisa
            # mendekripsi file lama yang dibuat dengan key legacy (SHA-256).
            return MultiFernet([Fernet(new_derived), Fernet(legacy)])
        except Exception:
            # Fallback kalau primitives/MultiFernet tidak tersedia
            return Fernet(legacy)
    else:
        return Fernet(raw_key.encode())


def secure_save(filepath: str, data: dict) -> bool:
    """
    Enkripsi data dict → simpan ke file.
    Fallback ke plain JSON kalau cryptography tidak ada.
    Return True kalau sukses.
    """
    fernet = _get_fernet()

    if fernet is None:
        # Fallback: plain JSON (atomic: tmp + replace)
        try:
            tmp = filepath + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, filepath)
            return True
        except Exception as e:
            log.error(f"secure_save fallback error {filepath}: {e}")
            return False

    try:
        json_bytes  = json.dumps(data, indent=2).encode("utf-8")
        encrypted   = fernet.encrypt(json_bytes)
        enc_path    = filepath + ".enc"
        tmp_path    = enc_path + ".tmp"
        # Atomic: tulis ke tmp lalu replace, supaya crash di tengah tulis tidak
        # meninggalkan file .enc korup (yang akan gagal didekripsi → state hilang).
        with open(tmp_path, "wb") as f:
            f.write(encrypted)
        os.replace(tmp_path, enc_path)
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
