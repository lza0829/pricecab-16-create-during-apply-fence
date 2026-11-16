import os
import tempfile

import pytest

# Point DATA_DIR at an isolated temp database before importing app modules.
_TMP = tempfile.mkdtemp(prefix="pricecab-test-")
os.environ["DATA_DIR"] = _TMP


@pytest.fixture()
def client(monkeypatch):
    # Fresh database per test.
    db_path = os.path.join(_TMP, "pricecab.db")
    if os.path.exists(db_path):
        os.remove(db_path)

    from fastapi.testclient import TestClient

    from app.config import DB_PATH
    from app.seed import seed_if_empty
    from app.main import app
    from app.services import apply_service

    seed_if_empty()
    # Reset the test seam between tests.
    monkeypatch.setattr(apply_service, "_on_pinned", None)

    with TestClient(app) as c:
        yield c
