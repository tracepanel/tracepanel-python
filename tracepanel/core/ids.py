"""ULID identifiers: sortable by creation time, 26 characters of Crockford base32."""

import base64
import random
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
# RFC 4648 base32 → Crockford's alphabet: the C codec does the work (every span gets an id).
_CROCKFORD = bytes.maketrans(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ234567", _ALPHABET.encode())


def new_id() -> str:
    # Ids must be unique, not secret: random beats os.urandom's system call on every span.
    value = (time.time_ns() // 1_000_000) << 80 | random.getrandbits(80)
    # 128 bits in 26 five-bit digits, most significant first: shifted into 17 bytes, the first 26 digits are them.
    return base64.b32encode((value << 6).to_bytes(17, "big")).translate(_CROCKFORD)[:26].decode()
