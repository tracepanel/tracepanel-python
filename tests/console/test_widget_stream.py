from tracepanel.console import widget

TAG = b"<script></script>"


def through(*pieces: bytes) -> list[bytes]:
    script = widget.Stream(TAG)
    return [script.feed(piece) for piece in pieces] + [script.end()]


def test_the_script_goes_right_after_the_head_opens() -> None:
    assert through(b"<!doctype html><html><head><title>t</title>", b"</head><body>x</body>") == [
        b"<!doctype html><html><head><script></script><title>t</title>",
        b"</head><body>x</body>",
        b"",
    ]


def test_a_tag_split_between_pieces_is_held_until_it_ends() -> None:
    assert through(b"<html><he", b'ad class="x">', b"<body>") == [
        b"<html>",
        b'<head class="x"><script></script>',
        b"<body>",
        b"",
    ]


def test_without_a_head_the_script_goes_after_the_body_opens() -> None:
    assert b"".join(through(b"<html><body>", b"hi</body>")) == b"<html><body><script></script>hi</body>"


def test_a_header_element_is_not_the_head() -> None:
    assert b"".join(through(b"<header>a</header><head>")) == b"<header>a</header><head><script></script>"


def test_a_page_without_either_far_enough_in_passes_as_it_came() -> None:
    pieces = [b"<p>" + b"x" * 40_000, b"<p>" + b"y" * 40_000, b"<head>"]

    assert b"".join(through(*pieces)) == b"".join(pieces)


def test_what_is_held_at_the_end_goes_out_then() -> None:
    assert through(b"text <unfinished") == [b"text ", b"<unfinished"]
