import re
import time

from tracepanel.core.ids import new_id

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def decoded_ms(ulid: str) -> int:
    return sum(ALPHABET.index(char) * 32 ** (9 - i) for i, char in enumerate(ulid[:10]))


def test_ulid_format_and_time() -> None:
    before = time.time_ns() // 1_000_000
    ids = [new_id() for _ in range(1000)]
    after = time.time_ns() // 1_000_000

    assert all(re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", ulid) for ulid in ids)
    assert all(before <= decoded_ms(ulid) <= after for ulid in ids)
    assert len(set(ids)) == len(ids)
