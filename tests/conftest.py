import pytest


@pytest.fixture(autouse=True)
def _fresh_suggestion_memory():
    """The last-known-good suggestion cache is process-wide; no test may inherit another's rows."""
    from shared import scrum_suggest
    scrum_suggest._KNOWN.clear()
    yield
    scrum_suggest._KNOWN.clear()
