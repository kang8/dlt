from typing import Any, Generator, Optional, Union, List
from dlt.common.json import json
from copy import deepcopy

from dlt.common.normalizers.naming.naming import NamingConvention

from contextlib import contextmanager
from dlt.common.destination.reference import (
    SupportsReadableRelation,
    SupportsReadableDataset,
    TDatasetType,
    TDestinationReferenceArg,
    Destination,
    JobClientBase,
    WithStateSync,
    DestinationClientDwhConfiguration,
)

from dlt.common.schema.typing import TTableSchemaColumns
from dlt.destinations.sql_client import SqlClientBase, WithSqlClient
from dlt.common.schema import Schema


class ReadableDBAPIRelation(SupportsReadableRelation):
    def __init__(
        self,
        *,
        client: SqlClientBase[Any],
        naming: NamingConvention,
        provided_query: Any = None,
        table_name: str = None,
        schema_columns: TTableSchemaColumns = None,
        limit: int = None,
        selected_columns: List[str] = None,
    ) -> None:
        """Create a lazy evaluated relation to for the dataset of a destination"""

        assert bool(table_name) != bool(
            provided_query
        ), "Please provide either an sql query OR a table_name"

        self._client = client
        self._naming = naming
        self._schema_columns = schema_columns
        self._provided_query = provided_query
        self._table_name = table_name
        self._limit = limit
        self._selected_columns = selected_columns

        # wire protocol functions
        self.df = self._wrap_func("df")  # type: ignore
        self.arrow = self._wrap_func("arrow")  # type: ignore
        self.fetchall = self._wrap_func("fetchall")  # type: ignore
        self.fetchmany = self._wrap_func("fetchmany")  # type: ignore
        self.fetchone = self._wrap_func("fetchone")  # type: ignore

        self.iter_df = self._wrap_iter("iter_df")  # type: ignore
        self.iter_arrow = self._wrap_iter("iter_arrow")  # type: ignore
        self.iter_fetch = self._wrap_iter("iter_fetch")  # type: ignore

    @property
    def query(self) -> Any:
        """build the query"""
        if self._provided_query:
            return self._provided_query

        table_name = self._client.make_qualified_table_name(self._table_name)

        maybe_limit_clause = ""
        if self._limit:
            maybe_limit_clause = f"LIMIT {self._limit}"

        selector = "*"
        if self._selected_columns:
            selector = ",".join(
                [
                    self._client.escape_column_name(self._naming.normalize_identifier(c))
                    for c in self._selected_columns
                ]
            )

        return f"SELECT {selector} FROM {table_name} {maybe_limit_clause}"

    @property
    def computed_schema_columns(self) -> TTableSchemaColumns:
        """provide schema columns for the cursor, may be filtered by selected columns"""
        if not self._schema_columns:
            return None
        if not self._selected_columns:
            return self._schema_columns
        filtered_columns: TTableSchemaColumns = {}
        for sc in self._selected_columns:
            sc = self._naming.normalize_identifier(sc)
            assert (
                sc in self._schema_columns.keys()
            ), f"Could not find column {sc} in provided dlt schema"
            filtered_columns[sc] = self._schema_columns[sc]

        return filtered_columns

    @contextmanager
    def cursor(self) -> Generator[SupportsReadableRelation, Any, Any]:
        """Gets a DBApiCursor for the current relation"""
        with self._client as client:
            # this hacky code is needed for mssql to disable autocommit, read iterators
            # will not work otherwise. in the future we should be able to create a readony
            # client which will do this automatically
            if hasattr(self._client, "_conn") and hasattr(self._client._conn, "autocommit"):
                self._client._conn.autocommit = False
            with client.execute_query(self.query) as cursor:
                if schema_columns := self.computed_schema_columns:
                    cursor.schema_columns = schema_columns
                yield cursor

    def _wrap_iter(self, func_name: str) -> Any:
        """wrap SupportsReadableRelation generators in cursor context"""

        def _wrap(*args: Any, **kwargs: Any) -> Any:
            with self.cursor() as cursor:
                yield from getattr(cursor, func_name)(*args, **kwargs)

        return _wrap

    def _wrap_func(self, func_name: str) -> Any:
        """wrap SupportsReadableRelation functions in cursor context"""

        def _wrap(*args: Any, **kwargs: Any) -> Any:
            with self.cursor() as cursor:
                return getattr(cursor, func_name)(*args, **kwargs)

        return _wrap

    def __copy__(self) -> "ReadableDBAPIRelation":
        return self.__class__(
            client=self._client,
            naming=self._naming,
            provided_query=self._provided_query,
            schema_columns=self._schema_columns,
            table_name=self._table_name,
            limit=self._limit,
            selected_columns=self._selected_columns,
        )

    def limit(self, limit: int) -> "ReadableDBAPIRelation":
        assert not self._provided_query, "Cannot change limit on relation with provided query"
        rel = self.__copy__()
        rel._limit = limit
        return rel

    def select(self, selected_columns: List[str]) -> "ReadableDBAPIRelation":
        assert (
            not self._provided_query
        ), "Cannot change selected columns on relation with provided query"
        rel = self.__copy__()
        rel._selected_columns = selected_columns
        # NOTE: the line below will ensure that no unknown columns are selected if
        # schema is known
        rel.computed_schema_columns
        return rel

    def head(self) -> "ReadableDBAPIRelation":
        assert not self._provided_query, "Cannot fetch head on relation with provided query"
        return self.limit(5)


class ReadableDBAPIDataset(SupportsReadableDataset):
    """Access to dataframes and arrowtables in the destination dataset via dbapi"""

    def __init__(
        self,
        destination: TDestinationReferenceArg,
        dataset_name: str,
        schema: Union[Schema, str, None] = None,
    ) -> None:
        self._destination = Destination.from_reference(destination)
        self._provided_schema = schema
        self._dataset_name = dataset_name
        self._sql_client: SqlClientBase[Any] = None
        self._schema: Schema = None

    @property
    def schema(self) -> Schema:
        self._ensure_client_and_schema()
        return self._schema

    @property
    def sql_client(self) -> SqlClientBase[Any]:
        self._ensure_client_and_schema()
        return self._sql_client

    def _destination_client(self, schema: Schema) -> JobClientBase:
        client_spec = self._destination.spec()
        if isinstance(client_spec, DestinationClientDwhConfiguration):
            client_spec._bind_dataset_name(
                dataset_name=self._dataset_name, default_schema_name=schema.name
            )
        return self._destination.client(schema, client_spec)

    def _ensure_client_and_schema(self) -> None:
        """Lazy load schema and client"""
        # full schema given, nothing to do
        if not self._schema and isinstance(self._provided_schema, Schema):
            self._schema = self._provided_schema

        # schema name given, resolve it from destination by name
        elif not self._schema and isinstance(self._provided_schema, str):
            with self._destination_client(Schema(self._provided_schema)) as client:
                if isinstance(client, WithStateSync):
                    stored_schema = client.get_stored_schema(self._provided_schema)
                    if stored_schema:
                        self._schema = Schema.from_stored_schema(json.loads(stored_schema.schema))

        # no schema name given, load newest schema from destination
        elif not self._schema:
            with self._destination_client(Schema(self._dataset_name)) as client:
                if isinstance(client, WithStateSync):
                    stored_schema = client.get_stored_schema()
                    if stored_schema:
                        self._schema = Schema.from_stored_schema(json.loads(stored_schema.schema))

        # default to empty schema with dataset name if nothing found
        if not self._schema:
            self._schema = Schema(self._dataset_name)

        # here we create the client bound to the resolved schema
        if not self._sql_client:
            destination_client = self._destination_client(self._schema)
            if isinstance(destination_client, WithSqlClient):
                self._sql_client = destination_client.sql_client
            else:
                raise Exception(
                    f"Destination {destination_client.config.destination_type} does not support"
                    " SqlClient."
                )

    def __call__(
        self, query: Any, schema_columns: TTableSchemaColumns = None
    ) -> ReadableDBAPIRelation:
        schema_columns = schema_columns or {}
        return ReadableDBAPIRelation(client=self.sql_client, naming=self.schema.naming, provided_query=query, schema_columns=schema_columns)  # type: ignore[abstract]

    def table(self, table_name: str) -> SupportsReadableRelation:
        # prepare query for table relation
        schema_columns = (
            self.schema.tables.get(table_name, {}).get("columns", {}) if self.schema else {}
        )
        return ReadableDBAPIRelation(
            client=self.sql_client,
            naming=self.schema.naming,
            table_name=table_name,
            schema_columns=schema_columns,
        )  # type: ignore[abstract]

    def __getitem__(self, table_name: str) -> SupportsReadableRelation:
        """access of table via dict notation"""
        return self.table(table_name)

    def __getattr__(self, table_name: str) -> SupportsReadableRelation:
        """access of table via property notation"""
        return self.table(table_name)


def dataset(
    destination: TDestinationReferenceArg,
    dataset_name: str,
    schema: Union[Schema, str, None] = None,
    dataset_type: TDatasetType = "dbapi",
) -> SupportsReadableDataset:
    if dataset_type == "dbapi":
        return ReadableDBAPIDataset(destination, dataset_name, schema)
    raise NotImplementedError(f"Dataset of type {dataset_type} not implemented")
