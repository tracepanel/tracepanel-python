from tracepanel.console import widget


def test_a_page_is_asked_for_by_navigating_or_by_accepting_html() -> None:
    assert widget.navigated({"sec-fetch-mode": "navigate"})
    assert widget.navigated({"accept": "text/html,application/xhtml+xml"})

    assert not widget.navigated({"sec-fetch-mode": "cors", "accept": "text/html"})
    assert not widget.navigated({"accept": "application/json"})
    assert not widget.navigated({})
