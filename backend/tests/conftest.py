import pytest

@pytest.fixture(autouse=True)
def isolate_legacy_mode(monkeypatch):
    """Legacy regression tests explicitly exercise the supported single-user adapter."""
    monkeypatch.setenv('FW_REQUIRE_AUTH','false')
    monkeypatch.setenv('FW_SCHEDULER_ENABLED','false')
