from types import TracebackType
from typing import (
    List,
    Any,
    cast,
    Union,
    Tuple,
    Iterable,
    Type,
    Optional,
    Dict,
    Sequence,
    TYPE_CHECKING,
    Set,
)

import lancedb  # type: ignore
import lancedb.table  # type: ignore
import pyarrow as pa
import pyarrow.parquet as pq
from lancedb import DBConnection
from lancedb.common import DATA  # type: ignore
from lancedb.embeddings import EmbeddingFunctionRegistry, TextEmbeddingFunction  # type: ignore
from lancedb.query import LanceQueryBuilder  # type: ignore
from numpy import ndarray
from pyarrow import Array, ChunkedArray, ArrowInvalid

from dlt.common import json, pendulum, logger
from dlt.common.destination import DestinationCapabilitiesContext
from dlt.common.destination.exceptions import (
    DestinationUndefinedEntity,
    DestinationTransientException,
    DestinationTerminalException,
)
from dlt.common.destination.reference import (
    JobClientBase,
    WithStateSync,
    RunnableLoadJob,
    StorageSchemaInfo,
    StateInfo,
    LoadJob,
    HasFollowupJobs,
    FollowupJobRequest,
)
from dlt.common.pendulum import timedelta
from dlt.common.schema import Schema, TTableSchema, TSchemaTables
from dlt.common.schema.typing import (
    TColumnType,
    TTableFormat,
    TTableSchemaColumns,
    TWriteDisposition,
    TColumnSchema,
)
from dlt.common.schema.utils import get_columns_names_with_prop
from dlt.common.storages import FileStorage, LoadJobInfo, ParsedLoadJobFileName
from dlt.destinations.impl.lancedb.configuration import (
    LanceDBClientConfiguration,
)
from dlt.destinations.impl.lancedb.exceptions import (
    lancedb_error,
)
from dlt.destinations.impl.lancedb.lancedb_adapter import (
    VECTORIZE_HINT,
)
from dlt.destinations.impl.lancedb.schema import (
    make_arrow_field_schema,
    make_arrow_table_schema,
    TArrowSchema,
    NULL_SCHEMA,
    TArrowField,
    arrow_datatype_to_fusion_datatype,
    TTableLineage,
)
from dlt.destinations.impl.lancedb.utils import (
    get_unique_identifiers_from_table_schema,
    set_non_standard_providers_environment_variables,
    generate_arrow_uuid_column,
    get_default_arrow_value,
)
from dlt.destinations.job_impl import ReferenceFollowupJobRequest
from dlt.destinations.type_mapping import TypeMapper

if TYPE_CHECKING:
    NDArray = ndarray[Any, Any]
else:
    NDArray = ndarray

TIMESTAMP_PRECISION_TO_UNIT: Dict[int, str] = {0: "s", 3: "ms", 6: "us", 9: "ns"}
UNIT_TO_TIMESTAMP_PRECISION: Dict[str, int] = {v: k for k, v in TIMESTAMP_PRECISION_TO_UNIT.items()}
BATCH_PROCESS_CHUNK_SIZE = 10_000


class LanceDBTypeMapper(TypeMapper):
    sct_to_unbound_dbt = {
        "text": pa.string(),
        "double": pa.float64(),
        "bool": pa.bool_(),
        "bigint": pa.int64(),
        "binary": pa.binary(),
        "date": pa.date32(),
        "complex": pa.string(),
    }

    sct_to_dbt = {}

    dbt_to_sct = {
        pa.string(): "text",
        pa.float64(): "double",
        pa.bool_(): "bool",
        pa.int64(): "bigint",
        pa.binary(): "binary",
        pa.date32(): "date",
    }

    def to_db_decimal_type(
        self, precision: Optional[int], scale: Optional[int]
    ) -> pa.Decimal128Type:
        precision, scale = self.decimal_precision(precision, scale)
        return pa.decimal128(precision, scale)

    def to_db_datetime_type(
        self, precision: Optional[int], table_format: TTableFormat = None
    ) -> pa.TimestampType:
        unit: str = TIMESTAMP_PRECISION_TO_UNIT[self.capabilities.timestamp_precision]
        return pa.timestamp(unit, "UTC")

    def to_db_time_type(
        self, precision: Optional[int], table_format: TTableFormat = None
    ) -> pa.Time64Type:
        unit: str = TIMESTAMP_PRECISION_TO_UNIT[self.capabilities.timestamp_precision]
        return pa.time64(unit)

    def from_db_type(
        self,
        db_type: pa.DataType,
        precision: Optional[int] = None,
        scale: Optional[int] = None,
    ) -> TColumnType:
        if isinstance(db_type, pa.TimestampType):
            return dict(
                data_type="timestamp",
                precision=UNIT_TO_TIMESTAMP_PRECISION[db_type.unit],
                scale=scale,
            )
        if isinstance(db_type, pa.Time64Type):
            return dict(
                data_type="time",
                precision=UNIT_TO_TIMESTAMP_PRECISION[db_type.unit],
                scale=scale,
            )
        if isinstance(db_type, pa.Decimal128Type):
            precision, scale = db_type.precision, db_type.scale
            if (precision, scale) == self.capabilities.wei_precision:
                return cast(TColumnType, dict(data_type="wei"))
            return dict(data_type="decimal", precision=precision, scale=scale)
        return super().from_db_type(cast(str, db_type), precision, scale)


def write_records(
    records: DATA,
    /,
    *,
    db_client: DBConnection,
    table_name: str,
    write_disposition: Optional[TWriteDisposition] = "append",
    id_field_name: Optional[str] = None,
    remove_orphans: Optional[bool] = False,
) -> None:
    """Inserts records into a LanceDB table with automatic embedding computation.

    Args:
        records: The data to be inserted as payload.
        db_client: The LanceDB client connection.
        table_name: The name of the table to insert into.
        id_field_name: The name of the ID field for update/merge operations.
        write_disposition: The write disposition - one of 'skip', 'append', 'replace', 'merge'.
        remove_orphans (bool): Whether to remove orphans after insertion or not (only merge disposition).

    Raises:
        ValueError: If the write disposition is unsupported, or `id_field_name` is not
            provided for update/merge operations.
    """

    try:
        tbl = db_client.open_table(table_name)
        tbl.checkout_latest()
    except FileNotFoundError as e:
        raise DestinationTransientException(
            "Couldn't open lancedb database. Batch WILL BE RETRIED"
        ) from e

    try:
        if write_disposition in ("append", "skip"):
            tbl.add(records)
        elif write_disposition == "replace":
            tbl.add(records, mode="overwrite")
        elif write_disposition == "merge":
            if not id_field_name:
                raise ValueError("To perform a merge update, 'id_field_name' must be specified.")
            if remove_orphans:
                tbl.merge_insert(id_field_name).when_not_matched_by_source_delete().execute(records)
            else:
                tbl.merge_insert(
                    id_field_name
                ).when_matched_update_all().when_not_matched_insert_all().execute(records)
        else:
            raise DestinationTerminalException(
                f"Unsupported write disposition {write_disposition} for LanceDB Destination - batch"
                " failed AND WILL **NOT** BE RETRIED."
            )
    except ArrowInvalid as e:
        raise DestinationTerminalException(
            "Python and Arrow datatype mismatch - batch failed AND WILL **NOT** BE RETRIED."
        ) from e


class LanceDBClient(JobClientBase, WithStateSync):
    """LanceDB destination handler."""

    model_func: TextEmbeddingFunction
    """The embedder callback used for each chunk."""
    dataset_name: str

    def __init__(
        self,
        schema: Schema,
        config: LanceDBClientConfiguration,
        capabilities: DestinationCapabilitiesContext,
    ) -> None:
        super().__init__(schema, config, capabilities)
        self.config: LanceDBClientConfiguration = config
        self.db_client: DBConnection = lancedb.connect(
            uri=self.config.credentials.uri,
            api_key=self.config.credentials.api_key,
            read_consistency_interval=timedelta(0),
        )
        self.registry = EmbeddingFunctionRegistry.get_instance()
        self.type_mapper = LanceDBTypeMapper(self.capabilities)
        self.sentinel_table_name = config.sentinel_table_name
        self.dataset_name = self.config.normalize_dataset_name(self.schema)

        embedding_model_provider = self.config.embedding_model_provider

        # LanceDB doesn't provide a standardized way to set API keys across providers.
        # Some use ENV variables and others allow passing api key as an argument.
        # To account for this, we set provider environment variable as well.
        set_non_standard_providers_environment_variables(
            embedding_model_provider,
            self.config.credentials.embedding_model_provider_api_key,
        )
        # Use the monkey-patched implementation if openai was chosen.
        if embedding_model_provider == "openai":
            from dlt.destinations.impl.lancedb.models import PatchedOpenAIEmbeddings

            self.model_func = PatchedOpenAIEmbeddings(
                max_retries=self.config.options.max_retries,
                api_key=self.config.credentials.api_key,
            )
        else:
            self.model_func = self.registry.get(embedding_model_provider).create(
                name=self.config.embedding_model,
                max_retries=self.config.options.max_retries,
                api_key=self.config.credentials.api_key,
            )

        self.vector_field_name = self.config.vector_field_name
        self.id_field_name = self.config.id_field_name

    @property
    def sentinel_table(self) -> str:
        return self.make_qualified_table_name(self.sentinel_table_name)

    def make_qualified_table_name(self, table_name: str) -> str:
        return (
            f"{self.dataset_name}{self.config.dataset_separator}{table_name}"
            if self.dataset_name
            else table_name
        )

    def get_table_schema(self, table_name: str) -> TArrowSchema:
        schema_table: "lancedb.table.Table" = self.db_client.open_table(table_name)
        schema_table.checkout_latest()
        schema = schema_table.schema
        return cast(
            TArrowSchema,
            schema,
        )

    @lancedb_error
    def create_table(
        self, table_name: str, schema: TArrowSchema, mode: str = "create"
    ) -> "lancedb.table.Table":
        """Create a LanceDB Table from the provided LanceModel or PyArrow schema.

        Args:
            schema: The table schema to create.
            table_name: The name of the table to create.
            mode (): The mode to use when creating the table. Can be either "create" or "overwrite".
                By default, if the table already exists, an exception is raised.
                If you want to overwrite the table, use mode="overwrite".
        """
        return self.db_client.create_table(table_name, schema=schema, mode=mode)

    def delete_table(self, table_name: str) -> None:
        """Delete a LanceDB table.

        Args:
            table_name: The name of the table to delete.
        """
        self.db_client.drop_table(table_name)

    def query_table(
        self,
        table_name: str,
        query: Union[List[Any], NDArray, Array, ChunkedArray, str, Tuple[Any], None] = None,
    ) -> LanceQueryBuilder:
        """Query a LanceDB table.

        Args:
            table_name: The name of the table to query.
            query: The targeted vector to search for.

        Returns:
            A LanceDB query builder.
        """
        query_table: "lancedb.table.Table" = self.db_client.open_table(table_name)
        query_table.checkout_latest()
        return query_table.search(query=query)

    @lancedb_error
    def _get_table_names(self) -> List[str]:
        """Return all tables in the dataset, excluding the sentinel table."""
        if self.dataset_name:
            prefix = f"{self.dataset_name}{self.config.dataset_separator}"
            table_names = [
                table_name
                for table_name in self.db_client.table_names()
                if table_name.startswith(prefix)
            ]
        else:
            table_names = self.db_client.table_names()

        return [table_name for table_name in table_names if table_name != self.sentinel_table]

    @lancedb_error
    def drop_storage(self) -> None:
        """Drop the dataset from the LanceDB instance.

        Deletes all tables in the dataset and all data, as well as sentinel table associated with them.

        If the dataset name wasn't provided, it deletes all the tables in the current schema.
        """
        for table_name in self._get_table_names():
            self.db_client.drop_table(table_name)

        self._delete_sentinel_table()

    @lancedb_error
    def initialize_storage(self, truncate_tables: Iterable[str] = None) -> None:
        if not self.is_storage_initialized():
            self._create_sentinel_table()
        elif truncate_tables:
            for table_name in truncate_tables:
                fq_table_name = self.make_qualified_table_name(table_name)
                if not self.table_exists(fq_table_name):
                    continue
                schema = self.get_table_schema(fq_table_name)
                self.db_client.drop_table(fq_table_name)
                self.create_table(
                    table_name=fq_table_name,
                    schema=schema,
                )

    @lancedb_error
    def is_storage_initialized(self) -> bool:
        return self.table_exists(self.sentinel_table)

    def _create_sentinel_table(self) -> "lancedb.table.Table":
        """Create an empty table to indicate that the storage is initialized."""
        return self.create_table(schema=NULL_SCHEMA, table_name=self.sentinel_table)

    def _delete_sentinel_table(self) -> None:
        """Delete the sentinel table."""
        self.db_client.drop_table(self.sentinel_table)

    @lancedb_error
    def update_stored_schema(
        self,
        only_tables: Iterable[str] = None,
        expected_update: TSchemaTables = None,
    ) -> Optional[TSchemaTables]:
        super().update_stored_schema(only_tables, expected_update)
        applied_update: TSchemaTables = {}

        try:
            schema_info = self.get_stored_schema_by_hash(self.schema.stored_version_hash)
        except DestinationUndefinedEntity:
            schema_info = None

        if schema_info is None:
            logger.info(
                f"Schema with hash {self.schema.stored_version_hash} "
                "not found in the storage. upgrading"
            )
            self._execute_schema_update(only_tables)
        else:
            logger.info(
                f"Schema with hash {self.schema.stored_version_hash} "
                f"inserted at {schema_info.inserted_at} found "
                "in storage, no upgrade required"
            )
        return applied_update

    def get_storage_table(self, table_name: str) -> Tuple[bool, TTableSchemaColumns]:
        table_schema: TTableSchemaColumns = {}

        try:
            fq_table_name = self.make_qualified_table_name(table_name)

            table: "lancedb.table.Table" = self.db_client.open_table(fq_table_name)
            table.checkout_latest()
            arrow_schema: TArrowSchema = table.schema
        except FileNotFoundError:
            return False, table_schema

        field: TArrowField
        for field in arrow_schema:
            name = self.schema.naming.normalize_identifier(field.name)
            table_schema[name] = {
                "name": name,
                **self.type_mapper.from_db_type(field.type),
            }
        return True, table_schema

    @lancedb_error
    def extend_lancedb_table_schema(self, table_name: str, field_schemas: List[pa.Field]) -> None:
        """Extend LanceDB table schema with empty columns.

        Args:
        table_name: The name of the table to create the fields on.
        field_schemas: The list of PyArrow Fields to create in the target LanceDB table.
        """
        table: "lancedb.table.Table" = self.db_client.open_table(table_name)
        table.checkout_latest()

        try:
            # Use DataFusion SQL syntax to alter fields without loading data into client memory.
            # Currently, the most efficient way to modify column values is in LanceDB.
            new_fields = {
                field.name: f"CAST(NULL AS {arrow_datatype_to_fusion_datatype(field.type)})"
                for field in field_schemas
            }
            table.add_columns(new_fields)

            # Make new columns nullable in the Arrow schema.
            # Necessary because the Datafusion SQL API doesn't set new columns as nullable by default.
            for field in field_schemas:
                table.alter_columns({"path": field.name, "nullable": field.nullable})

                # TODO: Update method below doesn't work for bulk NULL assignments, raise with LanceDB developers.
                # table.update(values={field.name: None})

        except OSError:
            # Error occurred while creating the table, skip.
            return None

    def _execute_schema_update(self, only_tables: Iterable[str]) -> None:
        for table_name in only_tables or self.schema.tables:
            exists, existing_columns = self.get_storage_table(table_name)
            new_columns: List[TColumnSchema] = self.schema.get_new_table_columns(
                table_name,
                existing_columns,
                self.capabilities.generates_case_sensitive_identifiers(),
            )
            logger.info(f"Found {len(new_columns)} updates for {table_name} in {self.schema.name}")
            if new_columns:
                if exists:
                    field_schemas: List[TArrowField] = [
                        make_arrow_field_schema(column["name"], column, self.type_mapper)
                        for column in new_columns
                    ]
                    fq_table_name = self.make_qualified_table_name(table_name)
                    self.extend_lancedb_table_schema(fq_table_name, field_schemas)
                else:
                    if table_name not in self.schema.dlt_table_names():
                        embedding_fields = get_columns_names_with_prop(
                            self.schema.get_table(table_name=table_name), VECTORIZE_HINT
                        )
                        vector_field_name = self.vector_field_name
                        id_field_name = self.id_field_name
                        embedding_model_func = self.model_func
                        embedding_model_dimensions = self.config.embedding_model_dimensions
                    else:
                        embedding_fields = None
                        vector_field_name = None
                        id_field_name = None
                        embedding_model_func = None
                        embedding_model_dimensions = None

                    table_schema: TArrowSchema = make_arrow_table_schema(
                        table_name,
                        schema=self.schema,
                        type_mapper=self.type_mapper,
                        embedding_fields=embedding_fields,
                        embedding_model_func=embedding_model_func,
                        embedding_model_dimensions=embedding_model_dimensions,
                        vector_field_name=vector_field_name,
                        id_field_name=id_field_name,
                    )
                    fq_table_name = self.make_qualified_table_name(table_name)
                    self.create_table(fq_table_name, table_schema)

        self.update_schema_in_storage()

    @lancedb_error
    def update_schema_in_storage(self) -> None:
        records = [
            {
                self.schema.naming.normalize_identifier("version"): self.schema.version,
                self.schema.naming.normalize_identifier(
                    "engine_version"
                ): self.schema.ENGINE_VERSION,
                self.schema.naming.normalize_identifier("inserted_at"): str(pendulum.now()),
                self.schema.naming.normalize_identifier("schema_name"): self.schema.name,
                self.schema.naming.normalize_identifier(
                    "version_hash"
                ): self.schema.stored_version_hash,
                self.schema.naming.normalize_identifier("schema"): json.dumps(
                    self.schema.to_dict()
                ),
            }
        ]
        fq_version_table_name = self.make_qualified_table_name(self.schema.version_table_name)
        write_disposition = self.schema.get_table(self.schema.version_table_name).get(
            "write_disposition"
        )

        write_records(
            records,
            db_client=self.db_client,
            table_name=fq_version_table_name,
            write_disposition=write_disposition,
        )

    @lancedb_error
    def get_stored_state(self, pipeline_name: str) -> Optional[StateInfo]:
        """Retrieves the latest completed state for a pipeline."""
        fq_state_table_name = self.make_qualified_table_name(self.schema.state_table_name)
        fq_loads_table_name = self.make_qualified_table_name(self.schema.loads_table_name)

        state_table_: "lancedb.table.Table" = self.db_client.open_table(fq_state_table_name)
        state_table_.checkout_latest()

        loads_table_: "lancedb.table.Table" = self.db_client.open_table(fq_loads_table_name)
        loads_table_.checkout_latest()

        # normalize property names
        p_load_id = self.schema.naming.normalize_identifier("load_id")
        p_dlt_load_id = self.schema.naming.normalize_identifier("_dlt_load_id")
        p_pipeline_name = self.schema.naming.normalize_identifier("pipeline_name")
        p_status = self.schema.naming.normalize_identifier("status")
        p_version = self.schema.naming.normalize_identifier("version")
        p_engine_version = self.schema.naming.normalize_identifier("engine_version")
        p_state = self.schema.naming.normalize_identifier("state")
        p_created_at = self.schema.naming.normalize_identifier("created_at")
        p_version_hash = self.schema.naming.normalize_identifier("version_hash")

        # Read the tables into memory as Arrow tables, with pushdown predicates, so we pull as less
        # data into memory as possible.
        state_table = (
            state_table_.search()
            .where(f"`{p_pipeline_name}` = '{pipeline_name}'", prefilter=True)
            .to_arrow()
        )
        loads_table = loads_table_.search().where(f"`{p_status}` = 0", prefilter=True).to_arrow()

        # Join arrow tables in-memory.
        joined_table: pa.Table = state_table.join(
            loads_table, keys=p_dlt_load_id, right_keys=p_load_id, join_type="inner"
        ).sort_by([(p_dlt_load_id, "descending")])

        if joined_table.num_rows == 0:
            return None

        state = joined_table.take([0]).to_pylist()[0]
        return StateInfo(
            version=state[p_version],
            engine_version=state[p_engine_version],
            pipeline_name=state[p_pipeline_name],
            state=state[p_state],
            created_at=pendulum.instance(state[p_created_at]),
            version_hash=state[p_version_hash],
            _dlt_load_id=state[p_dlt_load_id],
        )

    @lancedb_error
    def get_stored_schema_by_hash(self, schema_hash: str) -> Optional[StorageSchemaInfo]:
        fq_version_table_name = self.make_qualified_table_name(self.schema.version_table_name)

        version_table: "lancedb.table.Table" = self.db_client.open_table(fq_version_table_name)
        version_table.checkout_latest()
        p_version_hash = self.schema.naming.normalize_identifier("version_hash")
        p_inserted_at = self.schema.naming.normalize_identifier("inserted_at")
        p_schema_name = self.schema.naming.normalize_identifier("schema_name")
        p_version = self.schema.naming.normalize_identifier("version")
        p_engine_version = self.schema.naming.normalize_identifier("engine_version")
        p_schema = self.schema.naming.normalize_identifier("schema")

        try:
            schemas = (
                version_table.search().where(
                    f'`{p_version_hash}` = "{schema_hash}"', prefilter=True
                )
            ).to_list()

            most_recent_schema = sorted(schemas, key=lambda x: x[p_inserted_at], reverse=True)[0]
            return StorageSchemaInfo(
                version_hash=most_recent_schema[p_version_hash],
                schema_name=most_recent_schema[p_schema_name],
                version=most_recent_schema[p_version],
                engine_version=most_recent_schema[p_engine_version],
                inserted_at=most_recent_schema[p_inserted_at],
                schema=most_recent_schema[p_schema],
            )
        except IndexError:
            return None

    @lancedb_error
    def get_stored_schema(self) -> Optional[StorageSchemaInfo]:
        """Retrieves newest schema from destination storage."""
        fq_version_table_name = self.make_qualified_table_name(self.schema.version_table_name)

        version_table: "lancedb.table.Table" = self.db_client.open_table(fq_version_table_name)
        version_table.checkout_latest()
        p_version_hash = self.schema.naming.normalize_identifier("version_hash")
        p_inserted_at = self.schema.naming.normalize_identifier("inserted_at")
        p_schema_name = self.schema.naming.normalize_identifier("schema_name")
        p_version = self.schema.naming.normalize_identifier("version")
        p_engine_version = self.schema.naming.normalize_identifier("engine_version")
        p_schema = self.schema.naming.normalize_identifier("schema")

        try:
            schemas = (
                version_table.search().where(
                    f'`{p_schema_name}` = "{self.schema.name}"', prefilter=True
                )
            ).to_list()

            most_recent_schema = sorted(schemas, key=lambda x: x[p_inserted_at], reverse=True)[0]
            return StorageSchemaInfo(
                version_hash=most_recent_schema[p_version_hash],
                schema_name=most_recent_schema[p_schema_name],
                version=most_recent_schema[p_version],
                engine_version=most_recent_schema[p_engine_version],
                inserted_at=most_recent_schema[p_inserted_at],
                schema=most_recent_schema[p_schema],
            )
        except IndexError:
            return None

    def __exit__(
        self,
        exc_type: Type[BaseException],
        exc_val: BaseException,
        exc_tb: TracebackType,
    ) -> None:
        pass

    def __enter__(self) -> "LanceDBClient":
        return self

    @lancedb_error
    def complete_load(self, load_id: str) -> None:
        records = [
            {
                self.schema.naming.normalize_identifier("load_id"): load_id,
                self.schema.naming.normalize_identifier("schema_name"): self.schema.name,
                self.schema.naming.normalize_identifier("status"): 0,
                self.schema.naming.normalize_identifier("inserted_at"): str(pendulum.now()),
                self.schema.naming.normalize_identifier("schema_version_hash"): None,
            }
        ]
        fq_loads_table_name = self.make_qualified_table_name(self.schema.loads_table_name)
        write_disposition = self.schema.get_table(self.schema.loads_table_name).get(
            "write_disposition"
        )
        write_records(
            records,
            db_client=self.db_client,
            table_name=fq_loads_table_name,
            write_disposition=write_disposition,
        )

    def create_load_job(
        self, table: TTableSchema, file_path: str, load_id: str, restore: bool = False
    ) -> LoadJob:
        if ReferenceFollowupJobRequest.is_reference_job(file_path):
            return LanceDBRemoveOrphansJob(file_path)
        else:
            return LanceDBLoadJob(file_path, table)

    def create_table_chain_completed_followup_jobs(self, table_chain: Sequence[TTableSchema], completed_table_chain_jobs: Optional[Sequence[LoadJobInfo]] = None, ) -> List[FollowupJobRequest]:
        jobs = super().create_table_chain_completed_followup_jobs(table_chain, completed_table_chain_jobs)
        if table_chain[0].get("write_disposition")=="merge":
            all_job_paths_ordered = [job.file_path for table in table_chain for job in completed_table_chain_jobs if job.job_file_info.table_name==table.get("name")]
            root_table_file_name = FileStorage.get_file_name_from_file_path(all_job_paths_ordered[0])
            jobs.append(ReferenceFollowupJobRequest(root_table_file_name, all_job_paths_ordered))
        return jobs

    def table_exists(self, table_name: str) -> bool:
        return table_name in self.db_client.table_names()


class LanceDBLoadJob(RunnableLoadJob, HasFollowupJobs):
    arrow_schema: TArrowSchema

    def __init__(self, file_path: str, table_schema: TTableSchema, ) -> None:
        super().__init__(file_path)
        self._job_client: "LanceDBClient" = None
        self._table_schema: TTableSchema = table_schema

    def run(self) -> None:
        db_client: DBConnection = self._job_client.db_client
        fq_table_name: str = self._job_client.make_qualified_table_name(self._table_schema["name"])
        id_field_name: str = self._job_client.config.id_field_name
        unique_identifiers: Sequence[str] = get_unique_identifiers_from_table_schema(self._load_table)
        write_disposition: TWriteDisposition = cast(TWriteDisposition, self._load_table.get("write_disposition", "append"))

        with FileStorage.open_zipsafe_ro(self._file_path, mode="rb") as f:
            arrow_table: pa.Table = pq.read_table(f)

        if self._load_table["name"] not in self._schema.dlt_table_names():
            arrow_table = generate_arrow_uuid_column(arrow_table, unique_identifiers=unique_identifiers, table_name=fq_table_name, id_field_name=id_field_name, )

        write_records(arrow_table, db_client=db_client, table_name=fq_table_name, write_disposition=write_disposition, id_field_name=id_field_name, )


class LanceDBRemoveOrphansJob(RunnableLoadJob):
    orphaned_ids: Set[str]

    def __init__(self, file_path: str, ) -> None:
        super().__init__(file_path)
        self._job_client: "LanceDBClient" = None
        self.references = ReferenceFollowupJobRequest.resolve_references(file_path)

    def run(self) -> None:
        db_client: DBConnection = self._job_client.db_client
        table_lineage: List[Tuple[TTableSchema, str, str]] = [
            (
                self._schema.get_table(ParsedLoadJobFileName.parse(file_path_).table_name),
                ParsedLoadJobFileName.parse(file_path_).table_name,
                file_path_,
            )
            for file_path_ in self.references
        ]

        for target_table, target_table_name, target_table_path in table_lineage:
            target_is_root_table = "parent" not in target_table
            fq_table_name = self._job_client.make_qualified_table_name(target_table_name)

            if target_is_root_table:
                target_table_id_field_name = "_dlt_id"
                file_path = target_table_path
            else:
                target_table_id_field_name = "_dlt_parent_id"
                file_path = self.get_parent_path(table_lineage, target_table.get("parent"))

            with FileStorage.open_zipsafe_ro(file_path, mode="rb") as f:
                payload_arrow_table: pa.Table = pq.read_table(f)

            # Get target table schema
            with FileStorage.open_zipsafe_ro(target_table_path, mode="rb") as f:
                target_table_schema: pa.Schema = pq.read_schema(f)

            # LanceDB requires the payload to have all fields populated, even if we don't intend to use them in our merge operation.
            # Unfortunately, we can't just create NULL fields; else LanceDB always truncates the target using `when_not_matched_by_source_delete`.
            schema_difference = pa.schema(
                set(target_table_schema) - set(payload_arrow_table.schema)
            )
            for field in schema_difference:
                try:
                    default_value = get_default_arrow_value(field.type)
                    default_array = pa.array(
                        [default_value] * payload_arrow_table.num_rows, type=field.type
                    )
                    payload_arrow_table = payload_arrow_table.append_column(field, default_array)
                except ValueError as e:
                    logger.warn(f"{e}. Using null values for field '{field.name}'.")
                    null_array = pa.array([None] * payload_arrow_table.num_rows, type=field.type)
                    payload_arrow_table = payload_arrow_table.append_column(field, null_array)

            # TODO: Remove special field, we don't need it.
            payload_arrow_table = payload_arrow_table.append_column(
                pa.field(self._job_client.id_field_name, pa.string()),
                pa.array([""] * payload_arrow_table.num_rows, type=pa.string()),
            )

            write_records(
                payload_arrow_table,
                db_client=db_client,
                id_field_name=target_table_id_field_name,
                table_name=fq_table_name,
                write_disposition="merge",
                remove_orphans=True,
            )

    @staticmethod
    def get_parent_path(table_lineage: TTableLineage, table: str) -> Optional[str]:
        return next(entry[2] for entry in table_lineage if entry[1] == table)
