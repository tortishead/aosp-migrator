"""SQLite engine, sessions and Alembic upgrade on startup."""

from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

DB_FILENAME = "db.sqlite"
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def create_db_engine(data_dir: Path) -> Engine:
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Import jobs run in background threads, so connections cross threads.
    engine = create_engine(
        f"sqlite:///{data_dir / DB_FILENAME}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


def include_name(name: str | None, type_: str, parent_names: dict) -> bool:
    """Autogenerate filter: the FTS5 table and its shadow tables are not in the models."""
    return not (type_ == "table" and name is not None and name.startswith("issue_fts"))


def alembic_config() -> AlembicConfig:
    config = AlembicConfig()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return config


def upgrade(engine: Engine) -> None:
    """Bring the schema to the latest revision. Safe to call on every start."""
    config = alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def create_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
