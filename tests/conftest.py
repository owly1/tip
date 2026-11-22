import pytest
import stepup


@pytest.fixture(autouse=True)
def _one_round_by_default(request, monkeypatch):
    """Older tests pay $50+ and expect a single phrase; test_strict.py tests the strict rounds."""
    if request.module.__name__.split(".")[-1] != "test_strict":
        monkeypatch.setattr(stepup, "STRICT_OVER_CENTS", 10**12)
