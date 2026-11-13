import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import prospector  # noqa: E402


@pytest.fixture
def P(tmp_path, monkeypatch):
    """prospector with all data paths redirected to a temp dir (never the real data/)."""
    data = tmp_path / "data"
    monkeypatch.setattr(prospector, "DATA", str(data))
    monkeypatch.setattr(prospector, "TRACKER", str(data / "tracker.csv"))
    monkeypatch.setattr(prospector, "RESEARCH", str(data / "research.json"))
    monkeypatch.setattr(prospector, "DRAFTS", str(data / "drafts"))
    monkeypatch.setattr(prospector, "BRIEFS", str(data / "briefs"))

    def no_network(*a, **k):
        raise AssertionError("tests must not hit the network")
    monkeypatch.setattr(prospector, "ollama", no_network)
    return prospector
