from tracepanel.core.registry import Integration


def test_an_integration_whose_library_is_missing_is_not_installed() -> None:
    # Its module imports the library: importing it would fail the app's startup.
    integration = Integration("otel", "OpenTelemetry", ("no_such_library.sdk",), "no_such_module")

    assert integration.available is False

    integration.install()
