from pathlib import Path

from sqlalchemy import inspect

from aosp_migrator import db
from aosp_migrator.app import create_app
from aosp_migrator.config import Config


def test_migration_runs_on_fresh_data_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / "fresh"
    create_app(Config(data_dir))
    assert (data_dir / db.DB_FILENAME).is_file()
    engine = db.create_db_engine(data_dir)
    tables = inspect(engine).get_table_names()
    assert "workspace" in tables
    assert "alembic_version" in tables


def test_upgrade_is_idempotent(tmp_path: Path) -> None:
    engine = db.create_db_engine(tmp_path)
    db.upgrade(engine)
    db.upgrade(engine)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == "0016"


def test_foreign_keys_enabled(tmp_path: Path) -> None:
    engine = db.create_db_engine(tmp_path)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_models_match_migrations(tmp_path: Path) -> None:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from aosp_migrator.db.models import Base

    engine = db.create_db_engine(tmp_path)
    db.upgrade(engine)
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection, opts={"include_name": db.include_name}), Base.metadata)
    assert diff == [], "models changed without a migration; run `alembic revision --autogenerate`"
