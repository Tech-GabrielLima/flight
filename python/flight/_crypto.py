"""At-rest encryption for `.flight` files (Phase 9).

A `.flight` is meant to be *shared* — mailed to a vendor, attached to a ticket,
dropped in a bucket. But even after scrubbing (P5) it still holds real values
from a real run: request bodies, object contents, source code. Encryption at
rest lets you hand a black box to someone who can open the *bug* without handing
them the *data* in the clear on the way there.

The construction is deliberately boring and standard — no home-grown crypto:

* the key comes from a passphrase via **scrypt** (Python stdlib ``hashlib``),
  with a random 16-byte salt stored in the envelope;
* the payload is sealed with **AES-256-GCM** (an AEAD: confidentiality *and*
  tamper-detection), with a random 12-byte nonce;
* the envelope is ``FLGTENC1 | salt(16) | nonce(12) | ciphertext+tag`` — its own
  magic so a reader can tell an encrypted file from a plain one at a glance, and
  refuse to parse ciphertext as a `.flight`.

AES-GCM needs a real cipher, which the Python standard library does not ship, so
this feature depends on the **`cryptography`** package (``pip install
'flight-recorder[crypto]'``). The KDF and the envelope framing are stdlib-only
and always available; only the seal/open step needs the extra. When it is
missing, the functions raise a clear, catchable :class:`CryptoUnavailable`
rather than failing obscurely.
"""

from __future__ import annotations

import hashlib
import os
import struct
from pathlib import Path
from typing import Optional, Union

#: Envelope magic — distinct from the `.flight` ``FLGT`` magic so the two are
#: never confused. Version byte lets the envelope evolve.
MAGIC = b"FLGTENC1"
SALT_LEN = 16
NONCE_LEN = 12
# scrypt work factors (RFC 7914). N must be a power of two; these are the
# widely-used "interactive login" parameters — strong, ~tens of ms to derive.
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32  # AES-256
# scrypt needs ~128 * N * r * p bytes; OpenSSL's default maxmem (32 MiB) is too
# tight, so we ask for the exact requirement plus headroom.
_SCRYPT_MAXMEM = 128 * _SCRYPT_N * _SCRYPT_R * _SCRYPT_P * 2

_Key = Union[str, bytes]


class CryptoError(Exception):
    """Base class for encryption errors."""


class CryptoUnavailable(CryptoError):
    """The `cryptography` package is required but not installed."""

    def __init__(self):
        super().__init__(
            "at-rest encryption needs the 'cryptography' package — install it with:\n"
            "    pip install 'flight-recorder[crypto]'   (or: pip install cryptography)"
        )


class DecryptError(CryptoError):
    """The file is not a Flight envelope, or the passphrase is wrong / the data
    was tampered with (AEAD authentication failed)."""


def _aesgcm():
    """Return the AESGCM class, or raise :class:`CryptoUnavailable`."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        return AESGCM
    except Exception as exc:  # ImportError, or a broken install
        raise CryptoUnavailable() from exc


def is_available() -> bool:
    """True if at-rest encryption can be used in this interpreter."""
    try:
        _aesgcm()
        return True
    except CryptoUnavailable:
        return False


def derive_key(passphrase: _Key, salt: bytes) -> bytes:
    """Derive a 32-byte key from a passphrase and salt via scrypt (stdlib).

    Deterministic for a given ``(passphrase, salt)`` — this is what makes
    decryption possible — and independent of `cryptography`, so it is always
    testable."""
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
    """Split an envelope into ``(salt, nonce, ciphertext)``. Raises
    :class:`DecryptError` if the magic/length is wrong — stdlib-only, so the
    framing is testable without `cryptography`."""
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
    """True if `path` begins with the envelope magic."""
    try:
        with open(path, "rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except Exception:
        return False


def encrypt_bytes(plaintext: bytes, passphrase: _Key) -> bytes:
    """Seal `plaintext` into a Flight envelope with a fresh salt + nonce."""
    AESGCM = _aesgcm()
    salt = os.urandom(SALT_LEN)
    nonce = os.urandom(NONCE_LEN)
    key = derive_key(passphrase, salt)
    # Bind the header into the AEAD as associated data — tampering with the
    # salt/nonce then fails authentication too.
    aad = MAGIC + salt + nonce
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    return MAGIC + salt + nonce + ciphertext


def decrypt_bytes(blob: bytes, passphrase: _Key) -> bytes:
    """Open a Flight envelope. Raises :class:`DecryptError` on a wrong
    passphrase, a truncated file, or any tampering."""
    salt, nonce, ciphertext = parse_envelope(blob)
    AESGCM = _aesgcm()
    key = derive_key(passphrase, salt)
    aad = MAGIC + salt + nonce
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad)
    except Exception as exc:  # cryptography raises InvalidTag
        raise DecryptError(
            "could not decrypt — wrong passphrase, or the file was truncated or tampered with"
        ) from exc


def encrypt_file(
    in_path: Union[str, Path], passphrase: _Key, out_path: Optional[Union[str, Path]] = None
) -> Path:
    """Encrypt a `.flight` at `in_path` → `out_path` (default: `<in>.enc`)."""
    in_path = Path(in_path)
    out_path = Path(out_path) if out_path else in_path.with_suffix(in_path.suffix + ".enc")
    data = in_path.read_bytes()
    out_path.write_bytes(encrypt_bytes(data, passphrase))
    return out_path


def decrypt_file(
    in_path: Union[str, Path], passphrase: _Key, out_path: Optional[Union[str, Path]] = None
) -> Path:
    """Decrypt an envelope at `in_path` → `out_path`. If `out_path` is omitted
    and the input ends in `.enc`, that suffix is stripped; otherwise `.flight`
    is appended."""
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
