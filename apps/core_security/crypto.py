"""
Field-level Authenticated Encryption (AES-256-GCM) for sensitive mental-health records
(Journal entries, clinical evidence snippets, and emergency break-glass payloads).
"""

from __future__ import annotations

import base64
import hashlib
import os
from django.conf import settings
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

TOKEN_PREFIX = "enc:v1:"
NONCE_LENGTH = 12  # 96-bit NIST-recommended nonce for AES-GCM


def _derive_aes256_key() -> bytes:
    """Derive a stable 32-byte (256-bit) key from FIELD_ENCRYPTION_KEY or SECRET_KEY via SHA-256."""
    raw_material = getattr(settings, "FIELD_ENCRYPTION_KEY", "") or getattr(settings, "SECRET_KEY", "")
    if not raw_material:
        raw_material = "mindcare-fallback-dev-key"
    return hashlib.sha256(f"mindcare-aes256-gcm::{raw_material}".encode("utf-8")).digest()


def is_encrypted_token(value: str | None) -> bool:
    return bool(value and isinstance(value, str) and value.startswith(TOKEN_PREFIX))


def encrypt_field(plaintext: str, aad: str = "") -> str:
    """
    Encrypt UTF-8 string using AES-256-GCM with optional Associated Authenticated Data (AAD).
    Format: 'enc:v1:<urlsafe_base64(nonce_12B || ciphertext_and_16B_tag)>'
    """
    if plaintext is None:
        plaintext = ""
    key = _derive_aes256_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(NONCE_LENGTH)
    aad_bytes = aad.encode("utf-8") if aad else None
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), aad_bytes)
    packed = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
    return f"{TOKEN_PREFIX}{packed}"


def decrypt_field(token: str, aad: str = "") -> str:
    """
    Decrypt an 'enc:v1:...' AES-256-GCM token and verify authentication tag + AAD.
    Raises ValueError if tampered, malformed, or decrypted with mismatched AAD/key.
    """
    if not token:
        return ""
    if not is_encrypted_token(token):
        raise ValueError("Invalid encrypted field prefix; expected AES-256-GCM token.")
    raw_b64 = token[len(TOKEN_PREFIX) :]
    try:
        decoded = base64.urlsafe_b64decode(raw_b64.encode("ascii"))
    except Exception as exc:
        raise ValueError("Malformed base64 payload in encrypted token.") from exc

    if len(decoded) < NONCE_LENGTH + 16:
        raise ValueError("Encrypted payload too short to contain nonce and GCM tag.")

    nonce = decoded[:NONCE_LENGTH]
    ciphertext = decoded[NONCE_LENGTH:]
    key = _derive_aes256_key()
    aesgcm = AESGCM(key)
    aad_bytes = aad.encode("utf-8") if aad else None
    try:
        plaintext_bytes = aesgcm.decrypt(nonce, ciphertext, aad_bytes)
    except InvalidTag as exc:
        raise ValueError("AES-256-GCM authentication tag verification failed (tampered or wrong AAD).") from exc

    return plaintext_bytes.decode("utf-8")
