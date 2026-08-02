"""Encoding helpers for the Kocom binary protocol."""

from __future__ import annotations

import hashlib
import struct


def string_to_hex(value: str) -> str:
    """Encode an ASCII string as hexadecimal."""
    return value.encode("ascii").hex()


def hex_to_ascii(hex_string: str) -> str:
    """Decode an ASCII field and remove protocol padding."""
    return bytes.fromhex(hex_string).decode("ascii").rstrip("\x00")


def hex_to_double(hex_string: str) -> float:
    """Decode a little-endian IEEE 754 double."""
    return struct.unpack("<d", bytes.fromhex(hex_string))[0]


def md5_hashing(input_string: object) -> str:
    """Return the legacy MD5 digest required by the Kocom server protocol."""
    return hashlib.md5(str(input_string).encode()).hexdigest()  # noqa: S324


def string_to_padded_hex(input_string: object, size: int) -> str:
    """Encode a string and right-pad the hexadecimal field with zeros."""
    return str(input_string).encode().hex().ljust(size, "0")
