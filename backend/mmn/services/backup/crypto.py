"""Streaming AES-256-GCM encryption for backup archives.

Format: ``MMNBAK1`` | salt (16) | nonce prefix (8) | chunks.
Each chunk is ``len (4, big endian)`` + ciphertext of up to 1 MiB plaintext + 16-byte tag.
The nonce is ``prefix + counter (4)``; the last chunk is authenticated with AAD ``b"last"``,
so truncation is detected. The key is derived from the passphrase with scrypt.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"MMNBAK1"
CHUNK = 1024 * 1024


class DecryptionError(Exception):
    pass


def _key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode())


def is_encrypted(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(len(MAGIC)) == MAGIC


def encrypt_file(src: Path, dst: Path, passphrase: str) -> None:
    salt, prefix = os.urandom(16), os.urandom(8)
    aes = AESGCM(_key(passphrase, salt))
    with src.open("rb") as fin, dst.open("wb") as fout:
        fout.write(MAGIC + salt + prefix)
        counter = 0
        chunk = fin.read(CHUNK)
        while True:
            nxt = fin.read(CHUNK)
            aad = b"more" if nxt else b"last"
            ct = aes.encrypt(prefix + struct.pack(">I", counter), chunk, aad)
            fout.write(struct.pack(">I", len(ct)) + ct)
            counter += 1
            if not nxt:
                break
            chunk = nxt


def decrypt_file(src: Path, dst: Path, passphrase: str) -> None:
    with src.open("rb") as fin, dst.open("wb") as fout:
        if fin.read(len(MAGIC)) != MAGIC:
            raise DecryptionError("not an encrypted backup")
        salt, prefix = fin.read(16), fin.read(8)
        aes = AESGCM(_key(passphrase, salt))
        counter = 0
        while True:
            hdr = fin.read(4)
            if len(hdr) < 4:
                raise DecryptionError("backup is truncated")
            (n,) = struct.unpack(">I", hdr)
            ct = fin.read(n)
            peek = fin.read(1)
            aad = b"more" if peek else b"last"
            try:
                fout.write(aes.decrypt(prefix + struct.pack(">I", counter), ct, aad))
            except Exception as e:  # noqa: BLE001
                raise DecryptionError("wrong passphrase or damaged backup") from e
            counter += 1
            if not peek:
                break
            fin.seek(-1, os.SEEK_CUR)
