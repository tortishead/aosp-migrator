from alembic import context
from sqlalchemy import create_engine

from aosp_migrator.config import resolve_data_dir
from aosp_migrator.db import DB_FILENAME, include_name
from aosp_migrator.db.models import Base

target_metadata = Base.metadata


def run_migrations(connection) -> None:
    # batch mode lets ALTER TABLE work on SQLite.
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True, include_name=include_name)
    with context.begin_transaction():
        context.run_migrations()


connection = context.config.attributes.get("connection")
if connection is not None:
    # Called from `aosp_migrator.db.upgrade` at app start.
    run_migrations(connection)
else:
    # Called from the alembic CLI, e.g. `uv run alembic revision --autogenerate`.
    engine = create_engine(f"sqlite:///{resolve_data_dir() / DB_FILENAME}")
    with engine.begin() as connection:
        run_migrations(connection)
