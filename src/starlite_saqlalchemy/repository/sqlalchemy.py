"""SQLAlchemy-based implementation of the repository protocol."""
from __future__ import annotations

import random
import string
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Generic, Literal, TypeVar, cast
from uuid import UUID, uuid4

from sqlalchemy import delete, insert, over, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.sql import func as sql_func

from starlite_saqlalchemy.exceptions import ConflictError, StarliteSaqlalchemyError
from starlite_saqlalchemy.repository.abc import AbstractRepository
from starlite_saqlalchemy.repository.filters import (
    BeforeAfter,
    CollectionFilter,
    LimitOffset,
)
from starlite_saqlalchemy.utils import slugify

if TYPE_CHECKING:
    from collections import abc
    from datetime import datetime

    from sqlalchemy import Select
    from sqlalchemy.engine import Result
    from sqlalchemy.ext.asyncio import AsyncSession

    from starlite_saqlalchemy.db import orm
    from starlite_saqlalchemy.repository.types import FilterTypes

__all__ = (
    "SQLAlchemyRepository",
    "ModelT",
)

T = TypeVar("T")
ModelT = TypeVar("ModelT", bound="orm.Base | orm.AuditBase | orm.SlugBase")
SlugModelT = TypeVar("SlugModelT", bound="orm.SlugBase")
SQLARepoT = TypeVar("SQLARepoT", bound="SQLAlchemyRepository")
SelectT = TypeVar("SelectT", bound="Select[Any]")
RowT = TypeVar("RowT", bound=tuple[Any, ...])


@contextmanager
def wrap_sqlalchemy_exception() -> Any:
    """Do something within context to raise a `RepositoryError` chained from an
    original `SQLAlchemyError`.

    >>> try:
    ...     with wrap_sqlalchemy_exception():
    ...         raise SQLAlchemyError("Original Exception")
    ... except RepositoryError as exc:
    ...     print(f"caught repository exception from {type(exc.__context__)}")
    ...
    caught repository exception from <class 'sqlalchemy.exc.SQLAlchemyError'>
    """
    try:
        yield
    except IntegrityError as exc:
        raise ConflictError from exc
    except SQLAlchemyError as exc:
        raise StarliteSaqlalchemyError(f"An exception occurred: {exc}") from exc


class SQLAlchemyRepository(AbstractRepository[ModelT], Generic[ModelT]):
    """SQLAlchemy based implementation of the repository interface."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        base_select: Select[tuple[ModelT]] | None = None,
        **kwargs: Any,
    ) -> None:
        """Repository pattern for SQLAlchemy models.

        Args:
            session: Session managing the unit-of-work for the operation.
            base_select: To facilitate customization of the underlying select query.
            **kwargs: Any additional arguments
        """
        super().__init__(**kwargs)
        self.session = session
        self.statement = base_select or select(self.model_type)

    async def add(self, data: ModelT) -> ModelT:
        """Add `data` to the collection.

        Args:
            data: Instance to be added to the collection.

        Returns:
            The added instance.
        """
        with wrap_sqlalchemy_exception():
            instance = await self._attach_to_session(data)
            await self.session.flush()
            await self.session.refresh(instance)
            self.session.expunge(instance)
            return instance

    async def add_many(self, data: list[ModelT]) -> list[ModelT]:
        """Add Many `data` to the collection.

        Args:
            data: list of Instances to be added to the collection.

        This function has an optimized bulk insert based on the configured SQL dialect:
        - For backends supporting `RETURNING` with `executemany`, a single bulk insert with returning clause is executed.
        - For other backends, it does a bulk insert and then selects the inserted records

        Returns:
            The added instances.
        """
        data_to_insert: list[dict[str, Any]] = [v.to_dict() if isinstance(v, self.model_type) else v for v in data]  # type: ignore
        with wrap_sqlalchemy_exception():
            if self.session.bind.dialect.insert_executemany_returning:
                instances = list(
                    await self.session.scalars(  # type: ignore
                        insert(self.model_type).returning(self.model_type),
                        data_to_insert,  # pyright: reportGeneralTypeIssues=false
                    ),
                )
                for instance in instances:
                    self.session.expunge(instance)
                return instances
            # when bulk insert with returning isn't supported, we ensure there is a unique ID on each record so that we can refresh the records after insert.
            # ensure we have a UUID ID on each record so that we can refresh the data before returning
            new_primary_keys: list[UUID] = []
            for datum in data_to_insert:
                if datum.get(self.id_attribute, None) is None:
                    datum.update({self.id_attribute: uuid4()})
                new_primary_keys.append(datum.get(self.id_attribute))  # type: ignore[arg-type]

            await self.session.execute(
                insert(self.model_type),
                data_to_insert,
            )
            # select the records we just inserted.
            return await self.list(
                CollectionFilter(field_name=self.id_attribute, values=new_primary_keys),
            )

    async def delete(self, item_id: Any) -> ModelT:
        """Delete instance identified by `item_id`.

        Args:
            item_id: Identifier of instance to be deleted.

        Returns:
            The deleted instance.

        Raises:
            RepositoryNotFoundException: If no instance found identified by `item_id`.
        """
        with wrap_sqlalchemy_exception():
            instance = await self.get(item_id)
            await self.session.delete(instance)
            await self.session.flush()
            self.session.expunge(instance)
            return instance

    async def delete_many(self, item_ids: list[Any]) -> list[ModelT]:
        """Delete instance identified by `item_id`.

        Args:
            item_id: Identifier of instance to be deleted.

        Returns:
            The deleted instance.
        """
        with wrap_sqlalchemy_exception():
            if self.session.bind.dialect.delete_executemany_returning:
                instances = list(
                    await self.session.scalars(
                        delete(self.model_type)
                        .where(getattr(self.model_type, self.id_attribute).in_(item_ids))
                        .returning(self.model_type),
                    ),
                )
                await self.session.flush()
                for instance in instances:
                    self.session.expunge(instance)
                return instances

            instances = await self.list(
                CollectionFilter(field_name=self.id_attribute, values=item_ids),
            )
            await self.session.execute(
                delete(self.model_type).where(
                    getattr(self.model_type, self.id_attribute).in_(item_ids),
                ),
            )
            await self.session.flush()
            # no need to expunge.  The list did this already.
            return instances  # noqa: R504

    async def get(self, item_id: Any, **kwargs: Any) -> ModelT:
        """Get instance identified by `item_id`.

        Args:
            item_id: Identifier of the instance to be retrieved.

        Returns:
            The retrieved instance.

        Raises:
            RepositoryNotFoundException: If no instance found identified by `item_id`.
        """
        with wrap_sqlalchemy_exception():
            statement = self._filter_select_by_kwargs(
                statement=self.statement,
                **{self.id_attribute: item_id},
            )
            instance = (await self._execute(statement)).scalar_one_or_none()
            instance = self.check_not_found(instance)
            self.session.expunge(instance)
            return instance

    async def get_one(self, **kwargs: Any) -> ModelT:
        """Get instance identified by ``kwargs``.

        Args:
            **kwargs: Identifier of the instance to be retrieved.

        Returns:
            The retrieved instance.

        Raises:
            RepositoryNotFoundException: If no instance found identified by `item_id`.
        """
        with wrap_sqlalchemy_exception():
            statement = self._filter_select_by_kwargs(statement=self.statement, **kwargs)
            instance = (await self._execute(statement)).scalar_one_or_none()
            instance = self.check_not_found(instance)
            self.session.expunge(instance)
            return instance

    async def get_one_or_none(self, **kwargs: Any) -> ModelT | None:
        """Get instance identified by ``kwargs`` or None if not found.

        Args:
            **kwargs: Identifier of the instance to be retrieved.

        Returns:
            The retrieved instance or None
        """
        with wrap_sqlalchemy_exception():
            statement = self._filter_select_by_kwargs(statement=self.statement, **kwargs)
            instance = (await self._execute(statement)).scalar_one_or_none()
            if instance:
                self.session.expunge(instance)
            return instance or None

    async def get_or_create(self, **kwargs: Any) -> tuple[ModelT, bool]:
        """Get instance identified by ``kwargs`` or create if it doesn't exist.

        Args:
            **kwargs: Identifier of the instance to be retrieved.

        Returns:
            a tuple that includes the instance and whether or not it needed to be created.
        """
        existing = await self.get_one_or_none(**kwargs)
        if existing:
            return (existing, False)
        return (await self.add(self.model_type(**kwargs)), True)  # type: ignore[arg-type]

    async def count(self, *filters: FilterTypes, **kwargs: Any) -> int:
        """Get the count of records returned by a query.

        Args:
            *filters: Types for specific filtering operations.
            **kwargs: Instance attribute value filters.

        Returns:
            Count of records returned by query, ignoring pagination.
        """
        statement = self.statement.with_only_columns(
            sql_func.count(  # pylint: disable=not-callable
                self.model_type.id,  # type:ignore[attr-defined]
            ),
            maintain_column_froms=True,
        ).order_by(None)
        statement = self._apply_filters(*filters, apply_pagination=False, statement=statement)
        statement = self._filter_select_by_kwargs(statement, **kwargs)
        results = await self._execute(statement)
        return results.scalar_one()  # type: ignore

    async def update(self, data: ModelT) -> ModelT:
        """Update instance with the attribute values present on `data`.

        Args:
            data: An instance that should have a value for `self.id_attribute` that exists in the
                collection.

        Returns:
            The updated instance.

        Raises:
            RepositoryNotFoundException: If no instance found with same identifier as `data`.
        """
        with wrap_sqlalchemy_exception():
            item_id = self.get_id_attribute_value(data)
            # this will raise for not found, and will put the item in the session
            await self.get(item_id)
            # this will merge the inbound data to the instance we just put in the session
            instance = await self._attach_to_session(data, strategy="merge")
            await self.session.flush()
            await self.session.refresh(instance)
            self.session.expunge(instance)
            return instance

    async def update_many(self, data: list[ModelT]) -> list[ModelT]:
        """Update one or more instances with the attribute values present
        on`data`.

        This function has an optimized bulk insert based on the configured SQL dialect:
        - For backends supporting `RETURNING` with `executemany`, a single bulk insert with returning clause is executed.
        - For other backends, it does a bulk insert and then selects the inserted records

        Args:
            data: A list of instances to update.  Each should have a value for `self.id_attribute` that exists in the
                collection.

        Returns:
            The updated instances.

        Raises:
            RepositoryNotFoundException: If no instance found with same identifier as `data`.
        """
        data_to_update: list[dict[str, Any]] = [v.to_dict() if isinstance(v, self.model_type) else v for v in data]  # type: ignore
        with wrap_sqlalchemy_exception():
            if self.session.bind.dialect.update_executemany_returning:
                instances = list(
                    await self.session.scalars(  # type: ignore
                        update(self.model_type).returning(self.model_type),
                        data_to_update,  # pyright: reportGeneralTypeIssues=false
                    ),
                )
                await self.session.flush()
                for instance in instances:
                    self.session.expunge(instance)
                return instances
            updated_primary_keys: list[UUID] = []
            for datum in data_to_update:
                updated_primary_keys.append(datum.get("id"))  # type: ignore[arg-type]

            await self.session.execute(
                update(self.model_type),
                data_to_update,
            )
            await self.session.flush()
            # select the records we just updated.
            return await self.list(CollectionFilter(field_name="id", values=updated_primary_keys))

    async def list_and_count(
        self,
        *filters: FilterTypes,
        **kwargs: Any,
    ) -> tuple[list[ModelT], int]:
        """List records with total count.

        Args:
            *filters: Types for specific filtering operations.
            **kwargs: Instance attribute value filters.

        Returns:
            Count of records returned by query, ignoring pagination.
        """
        statement = self.statement.add_columns(
            over(
                sql_func.count(  # pylint: disable=not-callable
                    self.model_type.id,  # type:ignore[attr-defined]
                ),
            ),
        )
        statement = self._apply_filters(*filters, statement=statement)
        statement = self._filter_select_by_kwargs(statement, **kwargs)
        with wrap_sqlalchemy_exception():
            result = await self._execute(statement)
            count: int = 0
            instances: list[ModelT] = []
            for i, (instance, count_value) in enumerate(result):
                self.session.expunge(instance)
                instances.append(instance)
                if i == 0:
                    count = count_value
            return instances, count

    async def list(self, *filters: FilterTypes, **kwargs: Any) -> list[ModelT]:
        """Get a list of instances, optionally filtered.

        Args:
            *filters: Types for specific filtering operations.
            **kwargs: Instance attribute value filters.

        Returns:
            The list of instances, after filtering applied.
        """
        statement = self._apply_filters(*filters, statement=self.statement)
        statement = self._filter_select_by_kwargs(statement, **kwargs)

        with wrap_sqlalchemy_exception():
            result = await self._execute(statement)
            instances = list(result.scalars())
            for instance in instances:
                self.session.expunge(instance)
            return instances

    async def upsert(self, data: ModelT) -> ModelT:
        """Update or create instance.

        Updates instance with the attribute values present on `data`, or creates a new instance if
        one doesn't exist.

        Args:
            data: Instance to update existing, or be created. Identifier used to determine if an
                existing instance exists is the value of an attribute on `data` named as value of
                `self.id_attribute`.

        Returns:
            The updated or created instance.

        Raises:
            RepositoryNotFoundException: If no instance found with same identifier as `data`.
        """
        with wrap_sqlalchemy_exception():
            instance = await self._attach_to_session(data, strategy="merge")
            await self.session.flush()
            await self.session.refresh(instance)
            self.session.expunge(instance)
            return instance

    def filter_collection_by_kwargs(  # type:ignore[override]
        self,
        collection: SelectT,
        /,
        **kwargs: Any,
    ) -> SelectT:
        """Filter the collection by kwargs.

        Args:
            collection: the statement to filter.
            **kwargs: key/value pairs such that objects remaining in the collection after filtering
                have the property that their attribute named `key` has value equal to `value`.
        """
        with wrap_sqlalchemy_exception():
            return collection.filter_by(**kwargs)

    @classmethod
    async def check_health(cls, session: AsyncSession, query: str | None) -> bool:
        """Perform a health check on the database.

        Args:
            session: through which we run a check statement
            query: override the default health check SQL statement

        Returns:
            `True` if healthy.
        """
        query = query or "SELECT 1"
        return (  # type:ignore[no-any-return]  # pragma: no cover
            await session.execute(text(query))
        ).scalar_one() == 1

    # the following is all sqlalchemy implementation detail, and shouldn't be directly accessed

    def _apply_limit_offset_pagination(
        self,
        limit: int,
        offset: int,
        statement: SelectT,
    ) -> SelectT:
        return statement.limit(limit).offset(offset)

    async def _attach_to_session(
        self,
        model: ModelT,
        strategy: Literal["add", "merge"] = "add",
    ) -> ModelT:
        """Attach detached instance to the session.

        Args:
            model: The instance to be attached to the session.
            strategy: How the instance should be attached.
                - "add": New instance added to session
                - "merge": Instance merged with existing, or new one added.

        Returns:
            Instance attached to the session - if `"merge"` strategy, may not be same instance
            that was provided.
        """
        if strategy == "add":
            self.session.add(model)
            return model
        if strategy == "merge":
            return await self.session.merge(model)
        raise ValueError("Unexpected value for `strategy`, must be `'add'` or `'merge'`")

    async def _execute(self, statement: Select[RowT]) -> Result[RowT]:
        return cast("Result[RowT]", await self.session.execute(statement))

    def _apply_filters(
        self,
        *filters: FilterTypes,
        apply_pagination: bool = True,
        statement: SelectT,
    ) -> SelectT:
        """Apply filters to a select statement.

        Args:
            *filters: filter types to apply to the query
            apply_pagination: applies pagination filters if true
            select: select statement to apply filters

        Keyword Args:
            select: select to apply filters against

        Returns:
            The select with filters applied.
        """
        for filter_ in filters:
            if isinstance(filter_, LimitOffset):
                if apply_pagination:
                    statement = self._apply_limit_offset_pagination(
                        filter_.limit,
                        filter_.offset,
                        statement=statement,
                    )
                else:
                    pass
            elif isinstance(filter_, BeforeAfter):
                statement = self._filter_on_datetime_field(
                    filter_.field_name,
                    filter_.before,
                    filter_.after,
                    statement=statement,
                )
            elif isinstance(filter_, CollectionFilter):
                statement = self._filter_in_collection(
                    filter_.field_name,
                    filter_.values,
                    statement=statement,
                )
            else:
                raise StarliteSaqlalchemyError(f"Unexpected filter: {filter_}")
        return statement

    def _filter_in_collection(
        self,
        field_name: str,
        values: abc.Collection[Any],
        statement: SelectT,
    ) -> SelectT:
        if not values:
            return statement
        return statement.where(getattr(self.model_type, field_name).in_(values))

    def _filter_on_datetime_field(
        self,
        field_name: str,
        before: datetime | None,
        after: datetime | None,
        statement: SelectT,
    ) -> SelectT:
        field = getattr(self.model_type, field_name)
        if before is not None:
            statement = statement.where(field < before)
        if after is not None:
            statement = statement.where(field > before)
        return statement  # noqa: R504

    def _filter_select_by_kwargs(self, statement: SelectT, **kwargs: Any) -> SelectT:
        for key, val in kwargs.items():
            statement = statement.where(getattr(self.model_type, key) == val)
        return statement


class SQLAlchemyRepositorySlugMixin(
    SQLAlchemyRepository[SlugModelT],
):
    """Slug Repository Protocol."""

    async def get_by_slug(
        self,
        slug: str,
        **kwargs: Any,
    ) -> SlugModelT | None:
        """Select record by slug value."""
        return await self.get_one_or_none(slug=slug)

    async def get_available_slug(
        self,
        value_to_slugify: str,
        **kwargs: Any,
    ) -> str:
        """Get a unique slug for the supplied value.

        If the value is found to exist, a random 4 digit character is appended to the end.  There may be a better way to do this, but I wanted to limit the number of additional database calls.

        Args:
            value_to_slugify (str): A string that should be converted to a unique slug.

        Returns:
            str: a unique slug for the supplied value.  This is safe for URLs and other unique identifiers.
        """
        slug = slugify(value_to_slugify)
        if await self._is_slug_unique(slug):
            return slug
        random_string = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
        return f"{slug}-{random_string}"

    async def _is_slug_unique(
        self,
        slug: str,
        **kwargs: Any,
    ) -> bool:
        return await self.get_one_or_none(slug=slug) is None
