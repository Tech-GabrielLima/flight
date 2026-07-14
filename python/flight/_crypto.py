from __future__ import annotations

import hashlib
import os
import struct
from pathlib import Path
from typing import Optional, Union

MAGIC = b"FLGTENC1"
SALT_LEN = 16
NONCE_LEN = 12
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32
_SCRYPT_MAXMEM = 128 * _SCRYPT_N * _SCRYPT_R * _SCRYPT_P * 2

_Key = Union[str, bytes]


class CryptoError(Exception):
    pass


class CryptoUnavailable(CryptoError):

    def __init__(self):
        super().__init__(
            "at-rest encryption needs the 'cryptography' package — install it with:\n"
            "    pip install 'pyflight[crypto]'   (or: pip install cryptography)"
        )


class DecryptError(CryptoError):
    pass


def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        return AESGCM
    except Exception as exc:
        raise CryptoUnavailable() from exc


def is_available() -> bool:
    try:
        _aesgcm()
        return True
    except CryptoUnavailable:
        return False


def derive_key(passphrase: _Key, salt: bytes) -> bytes:
    if isinstance(passphrase, str):
        passphrase = passphrase.encode("utf-8")
    return hashlib.scrypt(
        passphrase,
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_KEY_LEN,
        maxmem=_SCRYPT_MAXMEM,
    )


def parse_envelope(blob: bytes) -> tuple[bytes, bytes, bytes]:
    header = len(MAGIC) + SALT_LEN + NONCE_LEN
    if len(blob) < header or blob[: len(MAGIC)] != MAGIC:
        raise DecryptError("not a Flight encrypted envelope (bad magic)")
    off = len(MAGIC)
    salt = blob[off : off + SALT_LEN]
    off += SALT_LEN
    nonce = blob[off : off + NONCE_LEN]
    off += NONCE_LEN
    return salt, nonce, blob[off:]


def looks_encrypted(path: Union[str, Path]) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except Exception:
        return False


def encrypt_bytes(plaintext: bytes, passphrase: _Key) -> bytes:
    AESGCM = _aesgcm()
    salt = os.urandom(SALT_LEN)
    nonce = os.urandom(NONCE_LEN)
    key = derive_key(passphrase, salt)
    aad = MAGIC + salt + nonce
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    return MAGIC + salt + nonce + ciphertext


def decrypt_bytes(blob: bytes, passphrase: _Key) -> bytes:
    salt, nonce, ciphertext = parse_envelope(blob)
    AESGCM = _aesgcm()
    key = derive_key(passphrase, salt)
    aad = MAGIC + salt + nonce
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad)
    except Exception as exc:
        raise DecryptError(
            "could not decrypt — wrong passphrase, or the file was truncated or tampered with"
        ) from exc


def encrypt_file(
    in_path: Union[str, Path], passphrase: _Key, out_path: Optional[Union[str, Path]] = None
) -> Path:
    in_path = Path(in_path)
    out_path = Path(out_path) if out_path else in_path.with_suffix(in_path.suffix + ".enc")
    data = in_path.read_bytes()
    out_path.write_bytes(encrypt_bytes(data, passphrase))
    return out_path


def decrypt_file(
    in_path: Union[str, Path], passphrase: _Key, out_path: Optional[Union[str, Path]] = None
) -> Path:
    in_path = Path(in_path)
    if out_path is None:
        if in_path.suffix == ".enc":
            out_path = in_path.with_suffix("")
        else:
            out_path = in_path.with_suffix(in_path.suffix + ".flight")
    out_path = Path(out_path)
    plaintext = decrypt_bytes(in_path.read_bytes(), passphrase)
    out_path.write_bytes(plaintext)
    return out_path
