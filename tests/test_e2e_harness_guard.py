"""The E2E fixture's proof that the backend on the fixed port is its own.

`tests/e2e/conftest.py` checks port 8000 is free, then spends up to three
import-heavy provisioning subprocesses (its own budget: 120s each) before
uvicorn is even started, and `_wait_healthy` returns on the first successful
/api/health without asking who answered. Anything that binds 8000 inside that
window inherits `_reshape_model_catalog`, which DELETEs every provider entry.
That happened twice on a dev box; the second time, three days of Team Builder
output was canned before anyone noticed. This guard is what makes the reshape
refuse to run against a database the fixture does not own.
"""

import sqlite3

import pytest

pytestmark = pytest.mark.unit

from e2e._guard import assert_backend_is_ours


def _catalog(path, specs):
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE model_catalog (id INTEGER PRIMARY KEY, spec TEXT)")
    con.executemany("INSERT INTO model_catalog (spec) VALUES (?)", [(s,) for s in specs])
    con.commit()
    con.close()


def test_probe_written_through_the_api_lands_in_our_database(tmp_path):
    db = tmp_path / "e2e.db"
    _catalog(db, ["fake:ok", "fake:e2e-probe-abc123"])
    assert_backend_is_ours("fake:e2e-probe-abc123", str(db))


def test_a_backend_writing_elsewhere_is_refused(tmp_path):
    # The probe PUT succeeded (some backend accepted it) but it is not in the
    # database we created -- so that backend is serving a different one.
    db = tmp_path / "e2e.db"
    _catalog(db, ["fake:ok"])
    with pytest.raises(RuntimeError) as exc:
        assert_backend_is_ours("fake:e2e-probe-abc123", str(db))
    assert "8000" in str(exc.value), "the error must name the port to stop"


def test_a_missing_database_is_refused_not_ignored(tmp_path):
    # Fail closed: unable to verify is never the same as verified.
    with pytest.raises(RuntimeError):
        assert_backend_is_ours("fake:e2e-probe-abc123", str(tmp_path / "absent.db"))


def test_a_database_without_the_table_is_refused_not_ignored(tmp_path):
    db = tmp_path / "e2e.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    with pytest.raises(RuntimeError):
        assert_backend_is_ours("fake:e2e-probe-abc123", str(db))
