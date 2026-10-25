from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import DateTime, String, Text, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TreeType = Literal["repo", "git", "plain"]


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """SQLite drops tz info: store naive UTC, hand back aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime}


class Workspace(Base):
    __tablename__ = "workspace"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    jql: Mapped[str] = mapped_column(Text)
    target_tree: Mapped[str] = mapped_column(String)
    target_tree_type: Mapped[str] = mapped_column(String)
    source_tree: Mapped[str | None] = mapped_column(String)
    source_tree_type: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
