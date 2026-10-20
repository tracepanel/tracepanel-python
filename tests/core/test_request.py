import json

from tracepanel.core import request


def test_request_headers_are_keyed_joined_and_masked_and_cookies_left_out() -> None:
    headers = [
        ("Accept", "text/html"),
        ("X-Forwarded-For", "10.0.0.1"),
        ("X-Forwarded-For", "10.0.0.2"),
        ("Authorization", "Bearer abc"),
        ("X-Api-Key", "k-123"),
        ("X-CSRFToken", "t-456"),
        ("Cookie", "sessionid=s; theme=dark"),
    ]

    assert request.request_headers(headers) == {
        "http.request.header.accept": "text/html",
        "http.request.header.x_forwarded_for": "10.0.0.1, 10.0.0.2",
        "http.request.header.authorization": "********",
        "http.request.header.x_api_key": "********",
        "http.request.header.x_csrftoken": "********",
    }


def test_response_headers_mask_their_cookies() -> None:
    headers = [("Content-Type", "text/html"), ("Set-Cookie", "sessionid=s"), ("Set-Cookie", "theme=dark")]

    assert request.response_headers(headers) == {
        "http.response.header.content_type": "text/html",
        "http.response.header.set_cookie": "********",
    }


def test_the_query_keeps_its_parameters_and_masks_secrets() -> None:
    assert request.query("page=2&q=red+shoes&token=abc&api_key=k") == {
        "url.query": "page=2&q=red+shoes&token=********&api_key=********",
    }

    assert request.query("") == {}


def test_cookies_are_read_from_the_header_with_session_ids_masked() -> None:
    assert request.cookies("theme=dark; sessionid=abc; csrftoken=xyz; __Host-Lang=en") == {
        "http.request.cookie.theme": "dark",
        "http.request.cookie.sessionid": "********",
        "http.request.cookie.csrftoken": "********",
        "http.request.cookie.__host_lang": "en",
    }

    assert request.cookie_values({"theme": "dark", "sessionid": "abc"}) == {
        "http.request.cookie.theme": "dark",
        "http.request.cookie.sessionid": "********",
    }


def test_the_session_is_shown_short_its_secrets_and_djangos_hash_masked() -> None:
    items = [
        ("_auth_user_id", "7"),
        ("_auth_user_backend", "django.contrib.auth.backends.ModelBackend"),
        ("_auth_user_hash", "f00d"),
        ("cart", [1, 2, 3]),
        ("oauth_token", "abc"),
    ]

    assert request.session(items) == {
        "session._auth_user_id": "'7'",
        "session._auth_user_backend": "'django.contrib.auth.backends.ModelBackend'",
        "session._auth_user_hash": "********",
        "session.cart": "[1, 2, 3]",
        "session.oauth_token": "********",
    }


def test_a_json_body_is_compacted_with_secrets_masked_at_any_depth() -> None:
    data = json.dumps({"email": "ann@example.com", "password": "p", "card": {"number": "4242", "cvc_token": "t"}})

    assert request.body("application/json; charset=utf-8", data.encode()) == {
        "http.request.body": (
            '{"email":"ann@example.com","password":"********","card":{"number":"4242","cvc_token":"********"}}'
        ),
    }

    assert request.body("application/json", b"{not json") == {"http.request.body": "{not json"}


def test_a_form_body_masks_its_secret_fields() -> None:
    assert request.body("application/x-www-form-urlencoded", b"username=ann&password=p&next=%2F") == {
        "http.request.body": "username=ann&password=********&next=/",
    }


def test_a_multipart_form_lists_its_files_by_name_and_size() -> None:
    boundary = "b0undary"
    data = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="title"\r\n\r\n'
        "Quarterly report\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="report.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
        "%PDF-1.7 0123456789\r\n"
        f"--{boundary}--\r\n"
    ).encode()

    assert request.body(f"multipart/form-data; boundary={boundary}", data) == {
        "http.request.body": "title=Quarterly report&file=<file report.pdf, 19 bytes>",
    }


def test_a_form_a_framework_parsed_is_shown_the_same_way() -> None:
    assert request.form([("title", "Q3"), ("password", "p")], [("file", "a.png", 10240)]) == {
        "http.request.body": "title=Q3&password=********&file=<file a.png, 10240 bytes>",
    }

    assert request.form([], []) == {}


def test_text_is_kept_up_to_the_limit_and_says_when_it_was_cut() -> None:
    long = b"x" * (request.MAX_BODY + 10)

    assert request.body("text/plain", b"hello") == {"http.request.body": "hello"}

    assert request.body("text/plain", long, complete=False) == {
        "http.request.body": "x" * request.MAX_BODY,
        "http.request.body.truncated": True,
    }


def test_a_binary_body_or_a_part_of_a_form_is_not_kept() -> None:
    assert request.body("application/octet-stream", b"\x00\x01") == {}
    assert request.body("image/png", b"\x89PNG") == {}
    assert request.body("application/json", b'{"a": 1', complete=False) == {}
    assert request.body("application/json", b"") == {}
