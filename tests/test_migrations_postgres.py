"""The Alembic chain replays on Postgres and lands on the same schema as
`create_all` (spec §8, Ruling 6). Runs only on the Postgres lane."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]
pytest.importorskip("sqlalchemy")
pytest.importorskip("alembic")

import _postgres

if not _postgres.enabled():
    pytest.skip("BESTTEAM_TEST_DATABASE_URL is not set", allow_module_level=True)

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from ui.backend.db import init_db

_ROOT = Path(__file__).resolve().parent.parent


def _columns(engine):
    inspector = sa.inspect(engine)
    return {
        table: {column["name"] for column in inspector.get_columns(table)}
        for table in inspector.get_table_names()
        if table != "alembic_version"
    }


def _foreign_keys(engine):
    """Every foreign key as (table, columns, referred table).

    Names are deliberately left out: the chain names its keys by the metadata
    convention (`fk_<table>_<column>_<referred>`), while `create_all` lets
    Postgres name them `<table>_<column>_fkey`. What has to agree is which
    column points at which table -- the loose-pointer migration (z3a4b5c6d7e8)
    is the reason: it drops two keys the chain created, and only comparing the
    sets says whether `create_all` agrees they are gone.
    """
    inspector = sa.inspect(engine)
    return {
        (table, tuple(fk["constrained_columns"]), fk["referred_table"])
        for table in inspector.get_table_names()
        if table != "alembic_version"
        for fk in inspector.get_foreign_keys(table)
    }


def _upgrade_head(url) -> None:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%"))
    command.upgrade(cfg, "head")


def test_upgrade_head_on_postgres_matches_create_all():
    migrated = _postgres.empty_database_url()
    _upgrade_head(migrated)  # must not raise: 42 migrations on a dialect they never ran on

    fresh = _postgres.empty_database_url()
    fresh_engine = sa.create_engine(fresh)
    migrated_engine = sa.create_engine(migrated)
    try:
        init_db(fresh_engine)
        assert _columns(migrated_engine) == _columns(fresh_engine)
        assert _foreign_keys(migrated_engine) == _foreign_keys(fresh_engine)
    finally:
        fresh_engine.dispose()
        migrated_engine.dispose()


def test_upgrade_head_stores_utc_whatever_the_server_zone_is():
    """The chain's one timestamp write -- b7c8d9e0f1a2 seeds the default
    organisation with CURRENT_TIMESTAMP into a naive column -- must land as
    UTC, which on Postgres means the migration session's zone must be UTC:
    Alembic's engine comes from `make_engine`, which pins it. The database's
    own default is moved off UTC first, because CI's container never is."""
    url = _postgres.empty_database_url()
    _postgres.set_default_timezone(url.database, "Asia/Tokyo")  # +09:00 all year
    _upgrade_head(url)

    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            seeded = conn.execute(
                sa.text("SELECT created_at FROM organizations WHERE name = 'default'")
            ).scalar_one()
    finally:
        engine.dispose()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs(now - seeded) < timedelta(minutes=5), f"seeded {seeded}, UTC now {now}"
