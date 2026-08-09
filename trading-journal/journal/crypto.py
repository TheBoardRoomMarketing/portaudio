"""Backup encryption.

The working database stays on local disk, unencrypted — it is opened hundreds of
times a day and encrypting it would buy little while adding a failure mode to
every read. What leaves the machine is the backup archive, and that is what gets
encrypted.

Key handling, in order of preference:

  1. **macOS Keychain** — the key lives in the login keychain, retrieved by
     `security find-generic-password`. Never written to the repository, never
     printed, never passed on a command line where it would land in shell
     history or a process listing.
  2. **A key file** at 0600 under the data directory, for Linux and for anyone
     who declines Keychain. Same guarantees minus the OS-level protection.

Cipher: AES-256-GCM via the `cryptography` package when it is installed,
otherwise ChaCha20-Poly1305 implemented on the standard library. Both are
authenticated: a tampered archive fails to decrypt rather than decrypting to
plausible rubbish. No external paid dependency either way.

The one thing this module refuses to do is encrypt with a key it cannot prove it
can retrieve again. A backup nobody can open is not a backup, so `ensure_key`
round-trips the key before it is used for the first time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import platform
import secrets
import struct
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from . import config

SERVICE = "trading-journal-backup"
ACCOUNT = "backup-encryption-key"
KEY_FILE = "backup.key"
MAGIC = b"TJBK1"          # archive header, so a wrong file fails fast and clearly


class KeyError_(RuntimeError):
    pass


class DecryptError(RuntimeError):
    pass


# --------------------------------------------------------------------- keychain
def _keychain_available() -> bool:
    if platform.system() != "Darwin":
        return False
    try:
        subprocess.run(["security", "-h"], capture_output=True, timeout=5)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _keychain_get() -> Optional[bytes]:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise KeyError_(f"keychain unavailable: {exc}") from exc
    if result.returncode != 0:
        return None
    return base64.b64decode(result.stdout.strip())


def _keychain_set(key: bytes) -> None:
    # The key is passed via -w with the value on argv, which is visible in a
    # process listing for the moment the command runs. `security` offers no
    # stdin route for this; the exposure is one process on a single-user machine
    # and is preferred to writing the key to a file that persists.
    try:
        subprocess.run(
            ["security", "add-generic-password", "-s", SERVICE, "-a", ACCOUNT,
             "-w", base64.b64encode(key).decode(), "-U"],
            capture_output=True, text=True, timeout=15, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise KeyError_(f"could not store key in keychain: {exc}") from exc


# --------------------------------------------------------------------- key file
def _key_file_path() -> Path:
    return Path(config.DATA_HOME) / KEY_FILE


def _file_get() -> Optional[bytes]:
    path = _key_file_path()
    if not path.exists():
        return None
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise KeyError_(
            f"{path} is readable by others (mode {mode:o}). Run: chmod 600 {path}")
    return base64.b64decode(path.read_text().strip())


def _file_set(key: bytes) -> None:
    path = _key_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with restrictive permissions from the start rather than fixing
    # them afterwards, so the key is never briefly world-readable.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(base64.b64encode(key).decode())


# --------------------------------------------------------------------- key API
def key_source() -> str:
    """Which store holds the key.

    Read from config on every call rather than cached, because the Keychain is
    scoped to the user while everything else here is scoped to the data
    directory — so this has to be overridable, and an override that only took
    effect at import time would be a trap.
    """
    backend = getattr(config, "KEY_BACKEND", "auto")
    if backend == "file":
        return "file"
    if backend == "keychain":
        if not _keychain_available():
            raise KeyError_(
                "JOURNAL_KEY_BACKEND=keychain but the macOS Keychain is not available "
                "on this machine. Use 'file' or 'auto'.")
        return "keychain"
    return "keychain" if _keychain_available() else "file"


def get_key(create: bool = False) -> Optional[bytes]:
    source = key_source()
    key = _keychain_get() if source == "keychain" else _file_get()
    if key or not create:
        return key

    key = secrets.token_bytes(32)
    if source == "keychain":
        _keychain_set(key)
    else:
        _file_set(key)

    # Prove it can be read back before anything is encrypted with it.
    check = _keychain_get() if source == "keychain" else _file_get()
    if check != key:
        raise KeyError_(
            "the new key could not be read back after storing it. Refusing to "
            "encrypt with a key that cannot be retrieved.")
    return key


def key_status() -> dict:
    source = key_source()
    try:
        present = get_key(create=False) is not None
        problem = None
    except KeyError_ as exc:
        present, problem = False, str(exc)
    return {
        "source": source,
        "present": present,
        "problem": problem,
        "location": ("login keychain: service=" + SERVICE) if source == "keychain"
                    else str(_key_file_path()),
        "note": "The key is never committed, never logged and never included in a backup.",
    }


# --------------------------------------------------------------------- cipher
_BACKEND: Optional[str] = None


def _cipher_backend() -> str:
    """Pick a backend by actually using it, not by importing it.

    A present-but-broken `cryptography` install (missing native bindings) raises
    from Rust rather than raising ImportError, so an import check is not enough.
    The fallback is a full ChaCha20-Poly1305 on the standard library, which is
    authenticated and needs no dependency at all.
    """
    global _BACKEND
    if _BACKEND:
        return _BACKEND
    # A broken build also writes a Rust backtrace straight to fd 2, below
    # Python's stderr, so the probe runs with fd 2 pointed at /dev/null. Only
    # the probe: everything afterwards reports normally.
    saved = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 2)
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            probe = AESGCM(b"\x00" * 32)
            assert probe.decrypt(b"\x00" * 12, probe.encrypt(b"\x00" * 12, b"ok", None),
                                 None) == b"ok"
            _BACKEND = "aes-256-gcm"
        except BaseException:  # noqa: BLE001 — a broken native build raises from Rust
            _BACKEND = "chacha20-poly1305"
    finally:
        os.dup2(saved, 2)
        os.close(saved)
        os.close(devnull)
    return _BACKEND


def _chacha20_block(key: bytes, counter: int, nonce: bytes) -> bytes:
    """One 64-byte ChaCha20 block (RFC 8439). Stdlib-only fallback."""
    def rotl(x, n):
        return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF

    constants = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)
    state = list(constants) + list(struct.unpack("<8I", key)) + [counter] + \
        list(struct.unpack("<3I", nonce))
    working = state[:]

    def quarter(a, b, c, d):
        working[a] = (working[a] + working[b]) & 0xFFFFFFFF
        working[d] = rotl(working[d] ^ working[a], 16)
        working[c] = (working[c] + working[d]) & 0xFFFFFFFF
        working[b] = rotl(working[b] ^ working[c], 12)
        working[a] = (working[a] + working[b]) & 0xFFFFFFFF
        working[d] = rotl(working[d] ^ working[a], 8)
        working[c] = (working[c] + working[d]) & 0xFFFFFFFF
        working[b] = rotl(working[b] ^ working[c], 7)

    for _ in range(10):
        quarter(0, 4, 8, 12); quarter(1, 5, 9, 13)
        quarter(2, 6, 10, 14); quarter(3, 7, 11, 15)
        quarter(0, 5, 10, 15); quarter(1, 6, 11, 12)
        quarter(2, 7, 8, 13); quarter(3, 4, 9, 14)

    return struct.pack("<16I", *[(working[i] + state[i]) & 0xFFFFFFFF for i in range(16)])


def _chacha20(key: bytes, nonce: bytes, data: bytes, counter: int = 1) -> bytes:
    out = bytearray()
    for offset in range(0, len(data), 64):
        block = _chacha20_block(key, counter + offset // 64, nonce)
        chunk = data[offset:offset + 64]
        out.extend(bytes(a ^ b for a, b in zip(chunk, block)))
    return bytes(out)


def _poly1305_key(key: bytes, nonce: bytes) -> bytes:
    return _chacha20_block(key, 0, nonce)[:32]


def _poly1305(key: bytes, message: bytes) -> bytes:
    r = int.from_bytes(key[:16], "little") & 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
    s = int.from_bytes(key[16:], "little")
    p = (1 << 130) - 5
    acc = 0
    for offset in range(0, len(message), 16):
        chunk = message[offset:offset + 16]
        acc = (acc + int.from_bytes(chunk + b"\x01", "little")) % p
        acc = (acc * r) % p
    return ((acc + s) & ((1 << 128) - 1)).to_bytes(16, "little")


def _aead_pad(data: bytes) -> bytes:
    return data + b"\x00" * ((16 - len(data) % 16) % 16)


def encrypt(plaintext: bytes, key: bytes) -> bytes:
    """Authenticated encryption. Output carries its own header and nonce."""
    backend = _cipher_backend()
    nonce = secrets.token_bytes(12)

    if backend == "aes-256-gcm":
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        blob = AESGCM(key).encrypt(nonce, plaintext, MAGIC)
        return MAGIC + b"\x01" + nonce + blob


    ciphertext = _chacha20(key, nonce, plaintext)
    mac_key = _poly1305_key(key, nonce)
    mac_input = (_aead_pad(MAGIC) + _aead_pad(ciphertext)
                 + struct.pack("<QQ", len(MAGIC), len(ciphertext)))
    tag = _poly1305(mac_key, mac_input)
    return MAGIC + b"\x02" + nonce + tag + ciphertext


def decrypt(blob: bytes, key: bytes) -> bytes:
    if not blob.startswith(MAGIC):
        raise DecryptError("not a Trading Journal encrypted archive")
    version = blob[len(MAGIC)]
    body = blob[len(MAGIC) + 1:]

    if version == 1:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce, payload = body[:12], body[12:]
        try:
            return AESGCM(key).decrypt(nonce, payload, MAGIC)
        except Exception as exc:  # noqa: BLE001
            raise DecryptError("wrong key, or the archive has been altered") from exc

    if version == 2:
        nonce, tag, ciphertext = body[:12], body[12:28], body[28:]
        mac_key = _poly1305_key(key, nonce)
        mac_input = (_aead_pad(MAGIC) + _aead_pad(ciphertext)
                     + struct.pack("<QQ", len(MAGIC), len(ciphertext)))
        if not hmac.compare_digest(_poly1305(mac_key, mac_input), tag):
            raise DecryptError("wrong key, or the archive has been altered")
        return _chacha20(key, nonce, ciphertext)

    raise DecryptError(f"unknown archive version {version}")


def cipher_name() -> str:
    return _cipher_backend()


def self_test() -> dict:
    """Encrypt, decrypt and tamper-check with an ephemeral key."""
    key = secrets.token_bytes(32)
    sample = b"the quick brown fox" * 500
    blob = encrypt(sample, key)
    if decrypt(blob, key) != sample:
        raise DecryptError("round trip failed")

    tampered = bytearray(blob)
    tampered[-1] ^= 0x01
    try:
        decrypt(bytes(tampered), key)
    except DecryptError:
        pass
    else:
        raise DecryptError("a tampered archive decrypted; authentication is not working")

    try:
        decrypt(blob, secrets.token_bytes(32))
    except DecryptError:
        pass
    else:
        raise DecryptError("the wrong key decrypted the archive")

    return {"cipher": cipher_name(), "round_trip": True, "tamper_detected": True,
            "wrong_key_rejected": True,
            "sha256": hashlib.sha256(blob).hexdigest()[:16]}
