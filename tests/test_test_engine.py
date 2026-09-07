"""The suite's engine helper: only the Postgres lane enforces foreign keys
(spec 2026-09-07 §5, Ruling 7's fallback; §8)."""

import pytest

pytestmark = pytest.mark.integration
pytest.importorskip("sqlalchemy")

from sqlalchemy.exc import IntegrityError

import _postgres
from helpers import make_test_engine
from ui.backend.db import init_db, session_factory
from ui.backend.db.models import TraceEventRecord


@pytest.mark.parametrize("use_file", [False, True], ids=["memory", "file"])
def test_only_the_postgres_lane_enforces_foreign_keys(tmp_path, use_file):
    # `PRAGMA foreign_keys=ON` was tried on both SQLite shapes and broke 194
    # tests, ~150 of them fixtures inserting a child without its parent. Ruling
    # 7 agreed that trade in advance: the pragma comes off, SQLite behaves as
    # the production file does, and the lane that always enforces keys is the
    # one that catches a child-before-parent write.
    engine = make_test_engine(tmp_path if use_file else None)
    init_db(engine)
    try:
        with session_factory(engine)() as db:
            db.add(TraceEventRecord(run_id="no-such-run", seq=0, type="run_failed"))
            if _postgres.enabled():
                with pytest.raises(IntegrityError):
                    db.commit()
            else:
                db.commit()  # the orphan lands: nothing enforces it here
    finally:
        engine.dispose()
