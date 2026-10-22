import json

import pytest
from dirty_equals import IsStr

from tracepanel.core import conf, request


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


def test_a_request_without_a_body_has_no_content_headers() -> None:
    # As WSGI hands them over for a GET: CONTENT_TYPE filled in, CONTENT_LENGTH empty.
    headers = [("Content-Length", ""), ("Content-Type", "text/plain"), ("Host", "localhost")]

    assert request.request_headers(headers) == {"http.request.header.host": "localhost"}


def test_a_request_with_a_body_keeps_its_content_type() -> None:
    headers = [("Content-Length", "12"), ("Content-Type", "application/json")]

    assert request.request_headers(headers) == {
        "http.request.header.content_length": "12",
        "http.request.header.content_type": "application/json",
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
            '{"email":"ann@example.com","password":"********","card":{"number":"********","cvc_token":"********"}}'
        ),
    }

    assert request.body("application/json", b"{not json") == {"http.request.body": "{not json"}


@pytest.mark.parametrize(
    "name",
    [
        "cvc",
        "cvv",
        "cvv2",
        "cvn",
        "card_cvc",
        "card_number",
        "cardNumber",
        "cardnumber",
        "card_no",
        "card-number",
        "card[number]",
        "card[cvc]",
        "pan",
        "card_pan",
    ],
)
def test_a_payment_cards_number_and_code_are_secrets(name: str) -> None:
    assert request.sensitive(name)


@pytest.mark.parametrize("name", ["company", "span", "expand", "pane", "card_brand", "card_last4", "discount"])
def test_names_that_only_contain_a_cards_letters_are_not(name: str) -> None:
    assert not request.sensitive(name)


def test_a_cards_number_is_masked_under_the_card_that_holds_it() -> None:
    data = {
        "card": {"number": "4242424242424242", "exp_month": 12, "brand": "visa"},
        "payment": {"creditCard": {"number": "4000056655665556", "cvv": "123"}},
        "cards": [{"number": "1"}],
        "order": {"number": 7},
    }

    assert request.body("application/json", json.dumps(data).encode()) == {
        "http.request.body": (
            '{"card":{"number":"********","exp_month":12,"brand":"visa"},'
            '"payment":{"creditCard":{"number":"********","cvv":"********"}},'
            '"cards":[{"number":"1"}],"order":{"number":7}}'
        ),
    }


def test_a_forms_card_fields_are_masked() -> None:
    form = b"card[number]=4242424242424242&card[cvc]=123&card[exp_month]=12&pan=4242"

    assert request.body("application/x-www-form-urlencoded", form) == {
        "http.request.body": "card[number]=********&card[cvc]=********&card[exp_month]=12&pan=********",
    }


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
    long = b"x" * (64 * 1024 + 10)

    assert request.body("text/plain", b"hello") == {"http.request.body": "hello"}

    assert request.body("text/plain", long, complete=False) == {
        "http.request.body": "x" * 64 * 1024,
        "http.request.body.truncated": True,
    }


def test_the_limit_is_a_setting_and_bodies_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEPANEL_MAX_BODY_KB", "1")
    conf.reset()

    assert request.body("text/plain", b"x" * 1100, complete=False) == {
        "http.request.body": "x" * 1024,
        "http.request.body.truncated": True,
    }

    assert request.response_body("text/plain", "", b"y" * 1100) == {
        "http.response.body": "y" * 1024,
        "http.response.body.truncated": True,
        "http.response.body.size": 1100,
    }

    monkeypatch.setenv("TRACEPANEL_CAPTURE_BODY", "0")
    conf.reset()

    assert request.body("application/json", b'{"a": 1}') == {}
    assert request.form([("title", "Q3")], []) == {}
    assert request.response_body("application/json", "", b'{"a": 1}') == {}


def test_a_response_body_is_read_as_a_request_body_with_its_size() -> None:
    data = json.dumps({"id": 7, "api_key": "k", "items": [{"refresh_token": "t", "sku": "A1"}]}).encode()

    assert request.response_body("application/json", "", data) == {
        "http.response.body": '{"id":7,"api_key":"********","items":[{"refresh_token":"********","sku":"A1"}]}',
        "http.response.body.size": len(data),
    }

    assert request.response_body("application/problem+json", "identity", b'{"title": "Gone"}') == {
        "http.response.body": '{"title":"Gone"}',
        "http.response.body.size": 17,
    }

    assert request.response_body("text/plain; charset=utf-8", "", "héllo".encode()) == {
        "http.response.body": "héllo",
        "http.response.body.size": 6,
    }


def test_a_response_body_cut_at_the_limit_is_masked_first() -> None:
    data = json.dumps({"password": "p", "rows": ["x" * 100] * 1000}).encode()

    found = request.response_body("application/json", "", data)

    assert found == {
        "http.response.body": IsStr(regex=r'\{"password":"\*{8}","rows":\["x+.*'),
        "http.response.body.truncated": True,
        "http.response.body.size": len(data),
    }
    assert len(str(found["http.response.body"])) == 64 * 1024


def test_a_response_body_kept_by_its_size_only() -> None:
    assert request.response_body("image/png", "", b"\x89PNG") == {"http.response.body.size": 4}
    assert request.response_body("application/json", "gzip", b"\x1f\x8b") == {"http.response.body.size": 2}
    assert request.response_body("text/html; charset=utf-8", "", b"<html></html>") == {"http.response.body.size": 13}
    assert request.response_body("application/json", "", b"") == {"http.response.body.size": 0}

    huge = b'{"a": "' + b"x" * request.MAX_PARSED + b'"}'
    assert request.response_body("application/json", "", huge) == {"http.response.body.size": len(huge)}


def test_a_binary_body_or_a_part_of_a_form_is_not_kept() -> None:
    assert request.body("application/octet-stream", b"\x00\x01") == {}
    assert request.body("image/png", b"\x89PNG") == {}
    assert request.body("application/json", b'{"a": 1', complete=False) == {}
    assert request.body("application/json", b"") == {}
