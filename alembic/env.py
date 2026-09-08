from logging.config import fileConfig

from alembic import context

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ui.backend.db.database import make_engine, resolve_database_url
from ui.backend.db.models import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# The URL comes from the same resolver the backend uses (BESTTEAM_DATABASE_URL,
# else BESTTEAM_DB_PATH) -- unless the caller already set `sqlalchemy.url` on
# the Config (the migration tests, `admin migrate-db`), which wins. `%` is
# doubled because ConfigParser interpolates it.
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", resolve_database_url(os.environ).replace("%", "%%"))

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    The engine comes from the backend's own factory, so a migration runs
    with every connection setting a deployment runs with -- above all the
    Postgres session pinned to UTC, which decides what `CURRENT_TIMESTAMP`
    stores in a naive column (b7c8d9e0f1a2 seeds one). Disposed at the end,
    so no pooled connection outlives the run.
    """
    connectable = make_engine(config.get_main_option("sqlalchemy.url"))
    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection, target_metadata=target_metadata
            )

            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
