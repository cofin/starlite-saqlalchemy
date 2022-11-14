"""Application ORM configuration."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID, uuid4

from sqlalchemy import MetaData
from sqlalchemy.dialects import postgresql as pg
from sqlalchemy.event import listens_for
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    declared_attr,
    mapped_column,
    registry,
)

from starlite_saqlalchemy import dto, settings

if TYPE_CHECKING:
    from pydantic import BaseModel

BaseT = TypeVar("BaseT", bound="Base")

DTO_KEY = settings.api.DTO_INFO_KEY
"""The key we use to reference `dto.Attrib` in the SQLAlchemy info dict."""

convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
"""
Templates for automated constraint name generation.
"""


@listens_for(Session, "before_flush")
def touch_updated_timestamp(session: Session, *_: Any) -> None:
    """Called from SQLAlchemy's.

    [`before_flush`][sqlalchemy.orm.SessionEvents.before_flush] event to bump
    the `updated` timestamp on modified instances.

    Args:
        session: The sync [`Session`][sqlalchemy.orm.Session] instance that underlies the async
            session.
    """
    for instance in session.dirty:
        instance.updated = datetime.now()


class Base(DeclarativeBase):
    """Base for all SQLAlchemy declarative models."""

    registry = registry(
        metadata=MetaData(naming_convention=convention),
        type_annotation_map={UUID: pg.UUID, dict: pg.JSONB},
    )

    id: Mapped[UUID] = mapped_column(
        default=uuid4, primary_key=True, info={DTO_KEY: dto.Attrib(mark=dto.Mark.READ_ONLY)}
    )
    """Primary key column."""
    created: Mapped[datetime] = mapped_column(
        default=datetime.now, info={DTO_KEY: dto.Attrib(mark=dto.Mark.READ_ONLY)}
    )
    """Date/time of instance creation."""
    updated: Mapped[datetime] = mapped_column(
        default=datetime.now, info={DTO_KEY: dto.Attrib(mark=dto.Mark.READ_ONLY)}
    )
    """Date/time of instance update."""

    # noinspection PyMethodParameters
    @declared_attr.directive
    def __tablename__(cls) -> str:  # pylint: disable=no-self-argument
        return cls.__name__.lower()

    @classmethod
    def from_dto(cls: type[BaseT], dto_instance: BaseModel) -> BaseT:
        """Construct an instance of the SQLAlchemy model from the Pydantic DTO.

        Args:
            dto_instance: A pydantic model

        Returns:
            An instance of the SQLAlchemy model.
        """
        return cls(**dto_instance.dict(exclude_unset=True))