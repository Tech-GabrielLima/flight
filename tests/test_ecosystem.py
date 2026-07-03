"""Phase 9 — ecosystem: the pytest plugin and at-rest encryption.

The plugin is exercised by running pytest as a subprocess against a generated
test file (clean isolation — the plugin installs `sys.monitoring` globally, so
we don't want it tangled with the outer test run). The crypto tests split in
two: the framing + key derivation are stdlib-only and always run; the AEAD
round-trip runs when `cryptography` is installed and skips cleanly otherwise.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import flight
from flight import _crypto

# =========================================================================
# pytest plugin
# =========================================================================

_SAMPLE = textwrap.dedent(
    """
    def helper(xs):
        return xs[10]        # IndexError

    def test_passes():
        assert 1 + 1 == 2

    def test_fails():
        helper([1, 2, 3])
    """
)


def _run_pytest(workdir: Path, *extra: str):
    # The plugin auto-loads via its `pytest11` entry point (installed with the
    # package) — no `-p` needed, and passing one would double-register it.
    (workdir / "test_sample.py").write_text(_SAMPLE)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "test_sample.py", *extra, "-q"],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_plugin_writes_a_black_box_for_a_failing_test(tmp_path):
    proc = _run_pytest(tmp_path, "--flight", "--flight-dir=fl")
    assert proc.returncode == 1, proc.stdout + proc.stderr  # one test fails
    fl = tmp_path / "fl"
    files = list(fl.glob("*.flight"))
    # Exactly one — the failure, not the pass.
    assert len(files) == 1
    assert "test_fails" in files[0].name
    # It is a real crash black box for the right exception.
    f = flight.read(files[0])
    assert f.has_crash
    assert f.exceptions[0][0] == "IndexError"
    # And the path is surfaced to the user.
    assert "black box" in proc.stdout
    assert "flight recorded 1 black box" in proc.stdout


def test_plugin_is_dormant_without_the_flag(tmp_path):
    proc = _run_pytest(tmp_path, "--flight-dir=fl")  # no --flight
    assert proc.returncode == 1  # the test still fails normally
    assert not (tmp_path / "fl").exists() or not list((tmp_path / "fl").glob("*.flight"))
    assert "black box" not in proc.stdout


def test_plugin_flight_all_also_records_passes(tmp_path):
    proc = _run_pytest(tmp_path, "--flight", "--flight-all", "--flight-dir=fl")
    files = list((tmp_path / "fl").glob("*.flight"))
    names = sorted(f.name for f in files)
    assert any("test_fails" in n for n in names)
    assert any("test_passes" in n for n in names)
    assert len(files) == 2


def test_plugin_never_changes_the_exit_status(tmp_path):
    """A fully-passing suite stays green with the plugin on."""
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "test_ok.py", "--flight", "-q"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_safe_name_sanitises_node_ids():
    from flight._pytest import _safe_name

    assert _safe_name("tests/test_x.py::test_foo[a-b]") == "tests_test_x.py_test_foo_a-b"
    assert _safe_name("") == "test"
    long = _safe_name("a/" * 200 + "test_z")
    assert len(long) <= 120 and long.endswith("test_z")


# =========================================================================
# at-rest encryption — stdlib-only paths (always run)
# =========================================================================


def test_key_derivation_is_deterministic_and_salted():
    k1 = _crypto.derive_key("correct horse", b"\x00" * 16)
    k2 = _crypto.derive_key("correct horse", b"\x00" * 16)
    k3 = _crypto.derive_key("correct horse", b"\x01" * 16)
    k4 = _crypto.derive_key("battery staple", b"\x00" * 16)
    assert k1 == k2  # same passphrase+salt → same key (decryptable)
    assert k1 != k3  # salt matters
    assert k1 != k4  # passphrase matters
    assert len(k1) == 32


def test_envelope_framing_round_trips():
    env = _crypto.MAGIC + b"S" * 16 + b"N" * 12 + b"ciphertext-here"
    salt, nonce, ct = _crypto.parse_envelope(env)
    assert salt == b"S" * 16
    assert nonce == b"N" * 12
    assert ct == b"ciphertext-here"


def test_parse_envelope_rejects_non_envelope():
    with pytest.raises(_crypto.DecryptError):
        _crypto.parse_envelope(b"FLGT....not an envelope")
    with pytest.raises(_crypto.DecryptError):
        _crypto.parse_envelope(b"tiny")


def test_looks_encrypted_distinguishes_files(tmp_path):
    plain = tmp_path / "p.bin"
    plain.write_bytes(b"FLGTsomething")  # a plain .flight starts with FLGT, not FLGTENC1
    enc = tmp_path / "e.bin"
    enc.write_bytes(_crypto.MAGIC + b"x")
    assert not _crypto.looks_encrypted(plain)
    assert _crypto.looks_encrypted(enc)
    assert not _crypto.looks_encrypted(tmp_path / "missing")


def test_encrypt_raises_clearly_when_cryptography_missing():
    if _crypto.is_available():
        pytest.skip("cryptography is installed — the missing-dep path can't be exercised")
    with pytest.raises(_crypto.CryptoUnavailable):
        _crypto.encrypt_bytes(b"data", "pw")


# =========================================================================
# at-rest encryption — the AEAD round-trip (needs `cryptography`)
# =========================================================================

requires_crypto = pytest.mark.skipif(
    not _crypto.is_available(), reason="needs the [crypto] extra (cryptography)"
)


@requires_crypto
def test_encrypt_decrypt_round_trip():
    plaintext = b"the whole .flight file, bytes and all" * 100
    blob = _crypto.encrypt_bytes(plaintext, "s3cret")
    assert blob.startswith(_crypto.MAGIC)
    assert plaintext not in blob  # actually encrypted
    assert _crypto.decrypt_bytes(blob, "s3cret") == plaintext


@requires_crypto
def test_wrong_passphrase_fails_authentication():
    blob = _crypto.encrypt_bytes(b"secret data", "right")
    with pytest.raises(_crypto.DecryptError):
        _crypto.decrypt_bytes(blob, "wrong")


@requires_crypto
def test_tampering_is_detected():
    blob = bytearray(_crypto.encrypt_bytes(b"secret data", "pw"))
    blob[-1] ^= 0xFF  # flip a bit in the tag/ciphertext
    with pytest.raises(_crypto.DecryptError):
        _crypto.decrypt_bytes(bytes(blob), "pw")


@requires_crypto
def test_encrypt_a_real_flight_file_round_trips(tmp_path):
    """Encrypt an actual crash `.flight`, then decrypt and read it back."""
    flight.install(output_dir=tmp_path)
    try:
        {}["missing"]
    except KeyError:
        src = flight.capture(path=str(tmp_path / "crash.flight"))
    flight.uninstall()

    enc = flight.encrypt_file(src, "vendor-pw")
    assert _crypto.looks_encrypted(enc)
    dec = flight.decrypt_file(enc, "vendor-pw", tmp_path / "out.flight")
    # The decrypted file is byte-identical and still a valid, readable .flight.
    assert Path(dec).read_bytes() == Path(src).read_bytes()
    f = flight.read(dec)
    assert f.exceptions[0][0] == "KeyError"
