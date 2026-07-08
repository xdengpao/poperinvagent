import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "fixtures" / "golden"


@pytest.fixture(scope="session")
def golden():
    def load(rel: str):
        return json.loads((GOLDEN / rel).read_text(encoding="utf-8"))

    return load
