"""ULID identifiers: sortable by creation time, 26 characters of Crockford base32."""

import base64
import random
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
# RFC 4648 base32 → Crockford's alphabet: the C codec does the work (every span gets an id).
_CROCKFORD = bytes.maketrans(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ234567", _ALPHABET.encode())

# The time part changes once a millisecond; ids made in the same one share it. One tuple, swapped whole, so a thread
# never pairs a millisecond with another one's digits.
_time_part: tuple[int, str] = (-1, "")


def new_id() -> str:
    global _time_part  # noqa: PLW0603 (a one-entry cache)
    ms = time.time_ns() // 1_000_000
    cached = _time_part
    if cached[0] != ms:
        # 48 bits in 10 digits (50 bits, the top two zero): shifted to the top of 7 bytes, the first 10 digits.
        cached = _time_part = (ms, base64.b32encode((ms << 6).to_bytes(7, "big")).translate(_CROCKFORD)[:10].decode())
    # Ids must be unique, not secret: random beats os.urandom's system call on every span. 80 bits, 16 digits.
    return cached[1] + base64.b32encode(random.getrandbits(80).to_bytes(10, "big")).translate(_CROCKFORD).decode()
