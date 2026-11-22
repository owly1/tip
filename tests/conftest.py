import pytest
import stepup


@pytest.fixture(autouse=True)
def _one_round_by_default(request, monkeypatch):
    """Older tests pay $50+ and expect a single phrase; test_strict.py tests the strict rounds."""
    if request.module.__name__.split(".")[-1] != "test_strict":
        monkeypatch.setattr(stepup, "STRICT_OVER_CENTS", 10**12)


@pytest.fixture(autouse=True)
def _detailed_output(monkeypatch):
    """Most tests check the detailed result lines (tip --dev); test_one_terminal.py checks the short default."""
    monkeypatch.setenv("TIP_DEV", "1")
