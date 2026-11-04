import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Tests never call a real model or read the local .env: the offline engine stands in.
os.environ["TRUSTLOOP_NO_DOTENV"] = "1"
os.environ["TRUSTLOOP_LLM"] = "offline"
os.environ.pop("TRUSTLOOP_API_KEY", None)

from trustloop import bank, db  # noqa: E402


@pytest.fixture
def store():
    s = db.Store(":memory:")
    bank.seed(s)
    db.set_default_store(s)
    yield s
    s.close()
