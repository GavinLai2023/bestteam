"""The suite's engine helper: foreign keys enforced in every shape (spec §5, §8)."""

import pytest

pytestmark = pytest.mark.integration
pytest.importorskip("sqlalchemy")

from sqlalchemy.exc import IntegrityError

from helpers import make_test_engine
from ui.backend.db import init_db, session_factory
from ui.backend.db.models import TraceEventRecord


@pytest.mark.parametrize("use_file", [False, True], ids=["memory", "file"])
def test_the_test_engine_enforces_foreign_keys(tmp_path, use_file):
    engine = make_test_engine(tmp_path if use_file else None)
    init_db(engine)
    try:
        with session_factory(engine)() as db:
            db.add(TraceEventRecord(run_id="no-such-run", seq=0, type="run_failed"))
            with pytest.raises(IntegrityError):
                db.commit()
    finally:
        engine.dispose()
