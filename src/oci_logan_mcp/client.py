"""OCI Log Analytics client wrapper."""

import asyncio
import logging
from io import BytesIO
from typing import Optional, List, Dict, Any, Sequence, Tuple
from datetime import datetime
from pathlib import Path

import oci
from oci.pagination import list_call_get_all_results

from .auth import get_signer
from .rate_limiter import RateLimiter
from .config import Settings

logger = logging.getLogger(__name__)


def _get_items(response_data):
    """Extract items from paginated response data.

    list_call_get_all_results returns response.data as a flat list,
    while single-page responses have response.data.items.
    """
    if isinstance(response_data, list):
        return response_data
    return response_data.items

# Debug file logging (writes to ~/.oci-logan-mcp/debug.log)
DEBUG_LOG_PATH = Path.home() / ".oci-logan-mcp" / "debug.log"


def _debug(msg: str):
    """Write debug message to file for troubleshooting."""
    try:
        with open(DEBUG_LOG_PATH, "a") as f:
            f.write(f"{datetime.now().isoformat()} | {msg}\n")
    except Exception:
        pass


class OCILogAnalyticsClient:
    """Wrapper for OCI Log Analytics operations.

    This client provides async-compatible methods for interacting with
    OCI Log Analytics APIs, with built-in rate limiting and error handling.
    """

    def __init__(self, settings: Settings):
        """Initialize the OCI client.

        Args:
            settings: Application settings with OCI configuration.
        """
        self.settings = settings
        self._config, self._signer = get_signer(settings.oci)

        self._la_client = oci.log_analytics.LogAnalyticsClient(
            config=self._config, signer=self._signer
        )

        self._identity_client = oci.identity.IdentityClient(
            config=self._config, signer=self._signer
        )

        self._rate_limiter = RateLimiter()
        self._auth_type = settings.oci.auth_type

        # Runtime context (can be changed)
        self._namespace = settings.log_analytics.namespace
        self._compartment_id = settings.log_analytics.default_compartment_id

    @property
    def monitoring_client(self):
        """Lazy accessor for OCI Monitoring client."""
        if not hasattr(self, "_monitoring_client") or self._monitoring_client is None:
            self._monitoring_client = oci.monitoring.MonitoringClient(
                config=self._config, signer=self._signer
            )
        return self._monitoring_client

    @property
    def dashx_client(self):
        """Lazy accessor for OCI Management Dashboard client."""
        if not hasattr(self, "_dashx_client") or self._dashx_client is None:
            self._dashx_client = oci.management_dashboard.DashxApisClient(
                config=self._config, signer=self._signer
            )
        return self._dashx_client

    @property
    def ons_client(self):
        """Lazy accessor for OCI Notification Control Plane client."""
        if not hasattr(self, "_ons_client") or self._ons_client is None:
            self._ons_client = oci.ons.NotificationControlPlaneClient(
                config=self._config, signer=self._signer
            )
        return self._ons_client

    @property
    def ons_data_client(self):
        """Lazy accessor for OCI Notification data-plane client."""
        if not hasattr(self, "_ons_data_client") or self._ons_data_client is None:
            self._ons_data_client = oci.ons.NotificationDataPlaneClient(
                config=self._config, signer=self._signer
            )
        return self._ons_data_client

    @property
    def namespace(self) -> str:
        """Get current Log Analytics namespace."""
        return self._namespace

    @namespace.setter
    def namespace(self, value: str) -> None:
        """Set Log Analytics namespace."""
        self._namespace = value

    @property
    def tenancy_id(self) -> str:
        """Get tenancy ID from config or signer."""
        tid = self._config.get("tenancy", "")
        if not tid and hasattr(self._signer, "tenancy_id"):
            tid = self._signer.tenancy_id
        return tid or ""

    @property
    def compartment_id(self) -> str:
        """Get current compartment ID."""
        return self._compartment_id

    @compartment_id.setter
    def compartment_id(self, value: str) -> None:
        """Set compartment ID."""
        self._compartment_id = value

    async def query(
        self,
        query_string: str,
        time_start: str,
        time_end: str,
        max_results: Optional[int] = None,
        include_subcompartments: bool = True,
        compartment_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute a Log Analytics query.

        Args:
            query_string: The Log Analytics query to execute.
            time_start: Start time in ISO 8601 format.
            time_end: End time in ISO 8601 format.
            max_results: Maximum number of results to return.
            include_subcompartments: If True, include logs from sub-compartments.
            compartment_id: Optional compartment OCID override.

        Returns:
            Dictionary containing query results and metadata.

        Raises:
            oci.exceptions.ServiceError: If OCI API call fails.
        """
        effective_compartment = compartment_id or self._compartment_id

        _debug(f"=== QUERY START ===")
        _debug(f"compartment_id: {effective_compartment} (override: {compartment_id is not None})")
        _debug(f"include_subcompartments: {include_subcompartments}")

        # Always use a single API call with compartment_id_in_subtree.
        # The OCI Log Analytics Query API natively supports querying across
        # sub-compartments (including at tenancy root level), just like the
        # "Subcompartments" checkbox in the Log Explorer UI.
        _debug("TAKING PATH: _execute_single_query")
        return await self._execute_single_query(
            query_string, time_start, time_end, max_results,
            effective_compartment, include_subcompartments
        )

    @staticmethod
    def _is_cluster_query(query_string: str) -> bool:
        """Check if this is a cluster query (e.g. '* | cluster')."""
        import re
        return bool(re.search(r'\|\s*cluster\b', query_string, re.IGNORECASE))

    async def _execute_single_query(
        self,
        query_string: str,
        time_start: str,
        time_end: str,
        max_results: Optional[int],
        compartment_id: str,
        include_subcompartments: bool,
    ) -> Dict[str, Any]:
        """Execute a query against a single compartment."""
        await self._rate_limiter.acquire()

        max_results = max_results or self.settings.query.max_results
        is_cluster = self._is_cluster_query(query_string)

        time_start_dt = datetime.fromisoformat(time_start.replace("Z", "+00:00"))
        time_end_dt = datetime.fromisoformat(time_end.replace("Z", "+00:00"))

        time_range = oci.log_analytics.models.TimeRange(
            time_start=time_start_dt,
            time_end=time_end_dt,
            time_zone="UTC",
        )

        # For cluster queries, don't cap max_total_count — let the API
        # process all records so the cluster algorithm produces accurate results.
        query_details = oci.log_analytics.models.QueryDetails(
            compartment_id=compartment_id,
            compartment_id_in_subtree=include_subcompartments,
            query_string=query_string,
            sub_system=oci.log_analytics.models.QueryDetails.SUB_SYSTEM_LOG,
            time_filter=time_range,
            **({} if is_cluster else {"max_total_count": max_results}),
        )

        logger.info(
            f"OCI Query: compartment={compartment_id}, "
            f"include_subtree={include_subcompartments}, "
            f"namespace={self._namespace}"
            f"{', cluster_mode=True' if is_cluster else ''}"
        )

        # For cluster queries, use a high page limit to get all clusters.
        # For regular queries, use max_results as the page limit.
        page_limit = 10000 if is_cluster else max_results

        try:
            response = self._la_client.query(
                namespace_name=self._namespace,
                query_details=query_details,
                limit=page_limit,
            )
            self._rate_limiter.reset()

            # Parse first page of results
            result = self._parse_query_response(response.data)

            # Fetch additional pages if available
            row_cap = page_limit if is_cluster else max_results
            while response.has_next_page and len(result["rows"]) < row_cap:
                await self._rate_limiter.acquire()
                response = self._la_client.query(
                    namespace_name=self._namespace,
                    query_details=query_details,
                    limit=page_limit,
                    page=response.next_page,
                )
                self._rate_limiter.reset()
                page_result = self._parse_query_response(response.data)
                result["rows"].extend(page_result["rows"])

            # Trim to limit and update count (skip trim for cluster queries)
            if not is_cluster and len(result["rows"]) > max_results:
                result["rows"] = result["rows"][:max_results]
            result["total_count"] = len(result["rows"])

            return result
        except oci.exceptions.ServiceError as e:
            if e.status == 429:
                await self._rate_limiter.handle_rate_limit()
                return await self._execute_single_query(
                    query_string, time_start, time_end, max_results,
                    compartment_id, include_subcompartments
                )
            raise

    def _parse_query_response(self, data: Any) -> Dict[str, Any]:
        """Parse query response into a structured dictionary."""
        columns = []
        if hasattr(data, "columns") and data.columns:
            columns = [
                {
                    "name": col.display_name or col.internal_name,
                    "internal_name": col.internal_name,
                    "type": col.value_type,
                }
                for col in data.columns
            ]

        rows = []
        if hasattr(data, "items") and data.items:
            for item in data.items:
                if isinstance(item, dict):
                    if columns:
                        row = []
                        for col in columns:
                            name = col["name"]
                            internal_name = col["internal_name"]
                            row.append(item.get(name, item.get(internal_name)))
                        rows.append(row)
                    else:
                        rows.append(list(item.values()))
                elif hasattr(item, "values"):
                    values = item.values
                    if callable(values):
                        rows.append(list(values()))
                    elif isinstance(values, (list, tuple)):
                        rows.append(list(values))
                    else:
                        rows.append([values])

        return {
            "columns": columns,
            "rows": rows,
            "total_count": getattr(data, "total_count", len(rows)),
            "is_partial": getattr(data, "is_partial_result", False),
        }

    async def list_log_sources(self, compartment_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all log sources (auto-paginates across all pages)."""
        await self._rate_limiter.acquire()

        compartment = compartment_id or self._compartment_id

        response = list_call_get_all_results(
            self._la_client.list_sources,
            namespace_name=self._namespace,
            compartment_id=compartment,
        )
        self._rate_limiter.reset()

        return [
            {
                "name": s.name,
                "display_name": getattr(s, "display_name", s.name),
                "description": getattr(s, "description", ""),
                "entity_types": self._serialize_entity_types(getattr(s, "entity_types", None)),
                "is_system": getattr(s, "is_system", False),
            }
            for s in _get_items(response.data)
        ]

    def _serialize_entity_types(self, entity_types: Any) -> List[str]:
        """Serialize entity types to JSON-compatible list of strings."""
        if entity_types is None:
            return []

        result = []
        for et in entity_types:
            if hasattr(et, "name"):
                result.append(et.name)
            elif hasattr(et, "entity_type_name"):
                result.append(et.entity_type_name)
            elif isinstance(et, str):
                result.append(et)
            else:
                result.append(str(et))
        return result

    async def list_fields(self, source_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """List fields, optionally filtered by source (auto-paginates)."""
        await self._rate_limiter.acquire()

        kwargs = {"namespace_name": self._namespace}
        if source_name:
            kwargs["source_name"] = source_name

        response = list_call_get_all_results(
            self._la_client.list_fields,
            **kwargs,
        )
        self._rate_limiter.reset()

        return [
            {
                "name": f.name,
                "display_name": getattr(f, "display_name", f.name),
                "data_type": getattr(f, "data_type", "STRING"),
                "description": getattr(f, "description", ""),
            }
            for f in _get_items(response.data)
        ]

    async def import_custom_content(self, content_zip: bytes, *, overwrite: bool = True) -> Dict[str, Any]:
        """Import a Log Analytics custom-content zip."""
        await self._rate_limiter.acquire()

        response = await asyncio.to_thread(
            self._la_client.import_custom_content,
            namespace_name=self._namespace,
            import_custom_content_file_body=BytesIO(content_zip),
            is_overwrite=overwrite,
        )
        self._rate_limiter.reset()

        return {
            "data": oci.util.to_dict(response.data) if response.data is not None else None,
            "headers": dict(response.headers),
        }

    async def upsert_json_parser(
        self,
        *,
        parser_name: str,
        display_name: str,
        field_paths: Sequence[Tuple[str, str]],
        field_mappings: Dict[str, str],
        example_content: str,
        description: str = "Auto-generated parser from sample JSON/NDJSON logs.",
    ) -> Dict[str, Any]:
        """Create or update a JSON parser using the native Log Analytics API."""
        await self._rate_limiter.acquire()

        field_maps = []
        sequence = 1
        for key, json_path in field_paths:
            field_name = field_mappings.get(key)
            if not field_name:
                continue
            field_maps.append(
                oci.log_analytics.models.LogAnalyticsParserField(
                    parser_field_sequence=sequence,
                    parser_field_name=field_name,
                    structured_column_info=json_path,
                )
            )
            sequence += 1

        details = oci.log_analytics.models.UpsertLogAnalyticsParserDetails(
            name=parser_name,
            display_name=display_name,
            description=description,
            type="JSON",
            # Native JSON parser exports use multi-line mode; NDJSON still works
            # because each uploaded line is independently parsed as JSON content.
            is_single_line_content=False,
            header_content="$:0",
            example_content=example_content,
            is_system=False,
            encoding="UTF-8",
            language="en_US",
            field_maps=field_maps,
            is_parser_written_once=False,
            is_default=False,
            should_tokenize_original_text=True,
        )
        response = await asyncio.to_thread(
            self._la_client.upsert_parser,
            namespace_name=self._namespace,
            upsert_log_analytics_parser_details=details,
        )
        self._rate_limiter.reset()

        return {
            "data": oci.util.to_dict(response.data) if response.data is not None else None,
            "headers": dict(response.headers),
        }

    async def upsert_delimited_parser(
        self,
        *,
        parser_name: str,
        display_name: str,
        field_paths: Sequence[Tuple[str, str]],
        field_mappings: Dict[str, str],
        header_content: str,
        example_content: str,
        field_delimiter: str = ",",
        field_qualifier: str = '"',
        description: str = "Auto-generated parser from sample CSV logs.",
    ) -> Dict[str, Any]:
        """Create or update a delimited parser using the native Log Analytics API."""
        await self._rate_limiter.acquire()

        field_maps = []
        for key, column_number in field_paths:
            field_name = field_mappings.get(key)
            if not field_name:
                continue
            field_maps.append(
                oci.log_analytics.models.LogAnalyticsParserField(
                    parser_field_sequence=int(column_number),
                    parser_field_name=field_name,
                )
            )

        details = oci.log_analytics.models.UpsertLogAnalyticsParserDetails(
            name=parser_name,
            display_name=display_name,
            description=description,
            type="DELIMITED",
            # CSV uploads are prepared as one record per physical line.
            is_single_line_content=True,
            field_delimiter=field_delimiter,
            field_qualifier=field_qualifier,
            header_content=header_content,
            example_content=example_content,
            is_system=False,
            encoding="UTF-8",
            language="en_US",
            field_maps=field_maps,
            is_parser_written_once=False,
            is_default=False,
            should_tokenize_original_text=True,
        )
        response = await asyncio.to_thread(
            self._la_client.upsert_parser,
            namespace_name=self._namespace,
            upsert_log_analytics_parser_details=details,
        )
        self._rate_limiter.reset()

        return {
            "data": oci.util.to_dict(response.data) if response.data is not None else None,
            "headers": dict(response.headers),
        }

    async def upsert_log_source(
        self,
        *,
        source_name: str,
        parser_name: str,
        display_name: str,
        entity_type: str,
        description: str = "Auto-generated log source from sample logs.",
    ) -> Dict[str, Any]:
        """Create or update a log source and bind it to the parser/entity type."""
        await self._rate_limiter.acquire()

        details = oci.log_analytics.models.UpsertLogAnalyticsSourceDetails(
            name=source_name,
            display_name=display_name,
            description=description,
            type_name="os_file",
            warning_config=0,
            is_secure_content=True,
            is_system=False,
            parsers=[
                oci.log_analytics.models.LogAnalyticsParser(
                    name=parser_name,
                    parser_sequence=1,
                )
            ],
            entity_types=[
                oci.log_analytics.models.LogAnalyticsSourceEntityType(
                    entity_type=entity_type,
                )
            ],
        )
        response = await asyncio.to_thread(
            self._la_client.upsert_source,
            namespace_name=self._namespace,
            upsert_log_analytics_source_details=details,
            is_ignore_warning=True,
        )
        self._rate_limiter.reset()

        return {
            "data": oci.util.to_dict(response.data) if response.data is not None else None,
            "headers": dict(response.headers),
        }

    async def upload_log_file(
        self,
        *,
        source_name: str,
        filename: str,
        log_group_id: str,
        content: Any,
        upload_name: Optional[str] = None,
        entity_id: Optional[str] = None,
        timezone: Optional[str] = None,
        log_set: Optional[str] = None,
        char_encoding: Optional[str] = "UTF-8",
    ) -> Dict[str, Any]:
        """Upload a log file to Log Analytics for processing by a source."""
        await self._rate_limiter.acquire()

        body = content if isinstance(content, bytes) else str(content).encode("utf-8")
        kwargs = {
            "namespace_name": self._namespace,
            "log_source_name": source_name,
            "filename": filename,
            "opc_meta_loggrpid": log_group_id,
            "upload_log_file_body": BytesIO(body),
        }
        if upload_name:
            kwargs["upload_name"] = upload_name
        if entity_id:
            kwargs["entity_id"] = entity_id
        if timezone:
            kwargs["timezone"] = timezone
        if log_set:
            kwargs["log_set"] = log_set
        if char_encoding:
            kwargs["char_encoding"] = char_encoding

        response = await asyncio.to_thread(self._la_client.upload_log_file, **kwargs)
        self._rate_limiter.reset()

        return {
            "data": oci.util.to_dict(response.data) if response.data is not None else None,
            "headers": dict(response.headers),
        }

    async def list_upload_files(self, upload_reference: str) -> List[Dict[str, Any]]:
        """List file-level processing status for a Log Analytics upload."""
        await self._rate_limiter.acquire()

        response = await asyncio.to_thread(
            self._la_client.list_upload_files,
            namespace_name=self._namespace,
            upload_reference=upload_reference,
        )
        self._rate_limiter.reset()

        return [
            {
                "reference": getattr(f, "reference", None),
                "name": getattr(f, "name", None),
                "status": getattr(f, "status", None),
                "total_chunks": getattr(f, "total_chunks", None),
                "chunks_consumed": getattr(f, "chunks_consumed", None),
                "chunks_success": getattr(f, "chunks_success", None),
                "chunks_fail": getattr(f, "chunks_fail", None),
                "time_started": getattr(f, "time_started", None),
                "source_name": getattr(f, "source_name", None),
                "entity_type": getattr(f, "entity_type", None),
                "entity_name": getattr(f, "entity_name", None),
                "log_group_id": getattr(f, "log_group_id", None),
                "log_group_name": getattr(f, "log_group_name", None),
                "failure_details": getattr(f, "failure_details", None),
            }
            for f in _get_items(response.data)
        ]

    async def list_entities(self, entity_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """List monitored entities (auto-paginates across all pages)."""
        await self._rate_limiter.acquire()

        kwargs = {
            "namespace_name": self._namespace,
            "compartment_id": self._compartment_id,
        }
        if entity_type:
            kwargs["entity_type_name"] = [entity_type]

        response = list_call_get_all_results(
            self._la_client.list_log_analytics_entities,
            **kwargs,
        )
        self._rate_limiter.reset()

        return [
            {
                "name": e.name,
                "entity_type": getattr(e, "entity_type_name", ""),
                "management_agent_id": getattr(e, "management_agent_id", None),
                "lifecycle_state": getattr(e, "lifecycle_state", ""),
            }
            for e in _get_items(response.data)
        ]

    async def list_parsers(self) -> List[Dict[str, Any]]:
        """List available parsers (auto-paginates across all pages)."""
        await self._rate_limiter.acquire()

        response = list_call_get_all_results(
            self._la_client.list_parsers,
            namespace_name=self._namespace,
        )
        self._rate_limiter.reset()

        return [
            {
                "name": p.name,
                "type": getattr(p, "type", ""),
                "description": getattr(p, "description", ""),
                "is_system": getattr(p, "is_system", False),
            }
            for p in _get_items(response.data)
        ]

    async def list_labels(self) -> List[Dict[str, Any]]:
        """List label definitions (auto-paginates across all pages)."""
        await self._rate_limiter.acquire()

        response = list_call_get_all_results(
            self._la_client.list_labels,
            namespace_name=self._namespace,
        )
        self._rate_limiter.reset()

        return [
            {
                "name": label.name,
                "display_name": getattr(label, "display_name", label.name),
                "description": getattr(label, "description", ""),
                "priority": getattr(label, "priority", ""),
            }
            for label in _get_items(response.data)
        ]

    async def list_saved_searches(self) -> List[Dict[str, Any]]:
        """List saved searches (auto-paginates across all pages)."""
        await self._rate_limiter.acquire()

        try:
            response = list_call_get_all_results(
                self._la_client.list_scheduled_tasks,
                namespace_name=self._namespace,
                compartment_id=self._compartment_id,
                task_type="SAVED_SEARCH",
            )
            self._rate_limiter.reset()

            return [
                {
                    "id": s.id,
                    "display_name": getattr(s, "display_name", ""),
                    "task_type": getattr(s, "task_type", ""),
                    "lifecycle_state": getattr(s, "lifecycle_state", ""),
                    "freeform_tags": getattr(s, "freeform_tags", {}) or {},
                }
                for s in _get_items(response.data)
            ]
        except Exception:
            self._rate_limiter.reset()
            return []

    async def get_saved_search(self, saved_search_id: str) -> Dict[str, Any]:
        """Get a specific saved search."""
        await self._rate_limiter.acquire()

        response = self._la_client.get_scheduled_task(
            namespace_name=self._namespace,
            scheduled_task_id=saved_search_id,
        )
        self._rate_limiter.reset()

        data = response.data
        return {
            "id": data.id,
            "display_name": getattr(data, "display_name", ""),
            "query": getattr(data, "saved_search_query", ""),
            "lifecycle_state": getattr(data, "lifecycle_state", ""),
            "_action": getattr(data, "action", None),  # expose for backing MSS lookup
        }

    async def list_compartments(self) -> List[Dict[str, Any]]:
        """List accessible compartments."""
        await self._rate_limiter.acquire()

        tenancy_id = self._config.get("tenancy")
        if not tenancy_id:
            # For instance/resource principal, try getting tenancy from signer
            if hasattr(self._signer, "tenancy_id"):
                tenancy_id = self._signer.tenancy_id
            else:
                self._rate_limiter.reset()
                logger.warning("Cannot list compartments: tenancy ID not available")
                return []

        response = list_call_get_all_results(
            self._identity_client.list_compartments,
            compartment_id=tenancy_id,
            compartment_id_in_subtree=True,
            access_level="ACCESSIBLE",
        )
        self._rate_limiter.reset()

        return [
            {
                "id": c.id,
                "name": c.name,
                "description": getattr(c, "description", ""),
                "lifecycle_state": c.lifecycle_state,
            }
            for c in response.data
        ]

    async def list_log_groups(self) -> List[Dict[str, Any]]:
        """List log groups (auto-paginates across all pages)."""
        await self._rate_limiter.acquire()

        response = list_call_get_all_results(
            self._la_client.list_log_analytics_log_groups,
            namespace_name=self._namespace,
            compartment_id=self._compartment_id,
        )
        self._rate_limiter.reset()

        return [
            {
                "id": g.id,
                "display_name": getattr(g, "display_name", ""),
                "description": getattr(g, "description", ""),
                "compartment_id": g.compartment_id,
            }
            for g in _get_items(response.data)
        ]

    async def get_namespace(self) -> str:
        """Get the Log Analytics namespace for the tenancy."""
        await self._rate_limiter.acquire()

        response = self._la_client.get_namespace(namespace_name=self._namespace)
        self._rate_limiter.reset()

        return response.data.namespace_name

    # ── Scheduled Task methods ─────────────────────────────────────────────

    async def create_scheduled_task(self, details) -> Dict[str, Any]:
        """Create a scheduled task in Log Analytics."""
        await self._rate_limiter.acquire()
        try:
            response = self._la_client.create_scheduled_task(
                namespace_name=self._namespace,
                create_scheduled_task_details=details,
            )
            self._rate_limiter.reset()
            data = response.data
            return {"id": data.id, "display_name": getattr(data, "display_name", ""),
                    "freeform_tags": getattr(data, "freeform_tags", {}) or {}}
        except oci.exceptions.ServiceError as e:
            if e.status == 429:
                await self._rate_limiter.handle_rate_limit()
                return await self.create_scheduled_task(details)
            raise

    async def update_scheduled_task(self, task_id: str, details) -> Dict[str, Any]:
        """Update a scheduled task in Log Analytics."""
        await self._rate_limiter.acquire()
        try:
            response = self._la_client.update_scheduled_task(
                namespace_name=self._namespace,
                scheduled_task_id=task_id,
                update_scheduled_task_details=details,
            )
            self._rate_limiter.reset()
            data = response.data
            return {"id": data.id, "display_name": getattr(data, "display_name", "")}
        except oci.exceptions.ServiceError as e:
            if e.status == 429:
                await self._rate_limiter.handle_rate_limit()
                return await self.update_scheduled_task(task_id, details)
            raise

    async def delete_scheduled_task(self, task_id: str) -> None:
        """Delete a scheduled task from Log Analytics."""
        await self._rate_limiter.acquire()
        try:
            self._la_client.delete_scheduled_task(
                namespace_name=self._namespace,
                scheduled_task_id=task_id,
            )
            self._rate_limiter.reset()
        except oci.exceptions.ServiceError as e:
            if e.status == 429:
                await self._rate_limiter.handle_rate_limit()
                return await self.delete_scheduled_task(task_id)
            raise

    # ── Alarm methods ──────────────────────────────────────────────────────

    async def create_alarm(self, details) -> Dict[str, Any]:
        """Create an OCI Monitoring alarm."""
        await self._rate_limiter.acquire()
        try:
            response = self.monitoring_client.create_alarm(create_alarm_details=details)
            self._rate_limiter.reset()
            data = response.data
            return {"id": data.id, "display_name": getattr(data, "display_name", ""),
                    "lifecycle_state": getattr(data, "lifecycle_state", ""),
                    "freeform_tags": getattr(data, "freeform_tags", {}) or {}}
        except oci.exceptions.ServiceError as e:
            if e.status == 429:
                await self._rate_limiter.handle_rate_limit()
                return await self.create_alarm(details)
            raise

    async def get_alarm(self, alarm_id: str) -> Dict[str, Any]:
        """Get a specific OCI Monitoring alarm."""
        await self._rate_limiter.acquire()
        try:
            response = self.monitoring_client.get_alarm(alarm_id=alarm_id)
            self._rate_limiter.reset()
            data = response.data
            return {"id": data.id, "display_name": getattr(data, "display_name", ""),
                    "lifecycle_state": getattr(data, "lifecycle_state", ""),
                    "severity": getattr(data, "severity", ""),
                    "is_enabled": getattr(data, "is_enabled", True),
                    "destinations": getattr(data, "destinations", []),
                    "query": getattr(data, "query", ""),
                    "pending_duration": getattr(data, "pending_duration", None),
                    "compartment_id": getattr(data, "compartment_id", None),
                    "freeform_tags": getattr(data, "freeform_tags", {}) or {}}
        except oci.exceptions.ServiceError as e:
            if e.status == 429:
                await self._rate_limiter.handle_rate_limit()
                return await self.get_alarm(alarm_id)
            raise

    async def list_alarms(self, compartment_id=None) -> List[Dict[str, Any]]:
        """List OCI Monitoring alarms (auto-paginates)."""
        await self._rate_limiter.acquire()
        cid = compartment_id or self._compartment_id
        try:
            response = list_call_get_all_results(
                self.monitoring_client.list_alarms,
                compartment_id=cid,
            )
            self._rate_limiter.reset()
            return [
                {"id": a.id, "display_name": getattr(a, "display_name", ""),
                 "lifecycle_state": getattr(a, "lifecycle_state", ""),
                 "severity": getattr(a, "severity", ""),
                 "freeform_tags": getattr(a, "freeform_tags", {}) or {}}
                for a in _get_items(response.data)
            ]
        except oci.exceptions.ServiceError as e:
            if e.status == 429:
                await self._rate_limiter.handle_rate_limit()
                return await self.list_alarms(compartment_id)
            raise

    async def update_alarm(self, alarm_id: str, details) -> Dict[str, Any]:
        """Update an OCI Monitoring alarm."""
        await self._rate_limiter.acquire()
        try:
            response = self.monitoring_client.update_alarm(
                alarm_id=alarm_id, update_alarm_details=details
            )
            self._rate_limiter.reset()
            data = response.data
            return {"id": data.id, "display_name": getattr(data, "display_name", ""),
                    "freeform_tags": getattr(data, "freeform_tags", {}) or {}}
        except oci.exceptions.ServiceError as e:
            if e.status == 429:
                await self._rate_limiter.handle_rate_limit()
                return await self.update_alarm(alarm_id, details)
            raise

    async def delete_alarm(self, alarm_id: str) -> None:
        """Delete an OCI Monitoring alarm."""
        await self._rate_limiter.acquire()
        try:
            self.monitoring_client.delete_alarm(alarm_id=alarm_id)
            self._rate_limiter.reset()
        except oci.exceptions.ServiceError as e:
            if e.status == 429:
                await self._rate_limiter.handle_rate_limit()
                return await self.delete_alarm(alarm_id)
            raise

    async def get_topic(self, topic_id: str) -> Dict[str, Any]:
        """Get an ONS notification topic."""
        await self._rate_limiter.acquire()
        try:
            response = self.ons_client.get_topic(topic_id=topic_id)
            self._rate_limiter.reset()
            data = response.data
            return {"id": data.topic_id, "name": getattr(data, "name", ""),
                    "lifecycle_state": getattr(data, "lifecycle_state", "")}
        except oci.exceptions.ServiceError as e:
            if e.status == 429:
                await self._rate_limiter.handle_rate_limit()
                return await self.get_topic(topic_id)
            raise

    async def publish_notification(
        self,
        topic_id: str,
        title: str,
        body: str,
    ) -> Dict[str, Any]:
        """Publish a message to an OCI Notifications topic."""
        await self._rate_limiter.acquire()
        try:
            details = oci.ons.models.MessageDetails(title=title, body=body)
            response = await asyncio.to_thread(
                self.ons_data_client.publish_message,
                topic_id=topic_id,
                message_details=details,
            )
            self._rate_limiter.reset()
            data = response.data
            return {
                "message_id": getattr(data, "message_id", None),
                "status": "sent",
                "topic_id": topic_id,
            }
        except oci.exceptions.ServiceError as e:
            if e.status == 429:
                await self._rate_limiter.handle_rate_limit()
                return await self.publish_notification(topic_id, title, body)
            raise

    # ── Management Saved Search methods ───────────────────────────────────

    async def create_management_saved_search(self, details) -> Dict[str, Any]:
        """Create a Management Dashboard saved search."""
        await self._rate_limiter.acquire()
        try:
            response = self.dashx_client.create_management_saved_search(
                create_management_saved_search_details=details
            )
            self._rate_limiter.reset()
            data = response.data
            return {"id": data.id, "display_name": getattr(data, "display_name", ""),
                    "freeform_tags": getattr(data, "freeform_tags", {}) or {}}
        except oci.exceptions.ServiceError as e:
            if e.status == 429:
                await self._rate_limiter.handle_rate_limit()
                return await self.create_management_saved_search(details)
            raise

    async def update_management_saved_search(self, search_id: str, details) -> Dict[str, Any]:
        """Update a Management Dashboard saved search."""
        await self._rate_limiter.acquire()
        try:
            response = self.dashx_client.update_management_saved_search(
                management_saved_search_id=search_id,
                update_management_saved_search_details=details,
            )
            self._rate_limiter.reset()
            data = response.data
            return {"id": data.id, "display_name": getattr(data, "display_name", "")}
        except oci.exceptions.ServiceError as e:
            if e.status == 429:
                await self._rate_limiter.handle_rate_limit()
                return await self.update_management_saved_search(search_id, details)
            raise

    async def delete_management_saved_search(self, search_id: str) -> None:
        """Delete a Management Dashboard saved search."""
        await self._rate_limiter.acquire()
        try:
            self.dashx_client.delete_management_saved_search(
                management_saved_search_id=search_id
            )
            self._rate_limiter.reset()
        except oci.exceptions.ServiceError as e:
            if e.status == 429:
                await self._rate_limiter.handle_rate_limit()
                return await self.delete_management_saved_search(search_id)
            raise

    # ── Dashboard methods ──────────────────────────────────────────────────

    async def create_management_dashboard(self, details) -> Dict[str, Any]:
        """Create a Management Dashboard."""
        await self._rate_limiter.acquire()
        try:
            response = self.dashx_client.create_management_dashboard(
                create_management_dashboard_details=details
            )
            self._rate_limiter.reset()
            data = response.data
            return {"id": data.id, "display_name": getattr(data, "display_name", "")}
        except oci.exceptions.ServiceError as e:
            if e.status == 429:
                await self._rate_limiter.handle_rate_limit()
                return await self.create_management_dashboard(details)
            raise

    async def list_management_dashboards(self, compartment_id=None) -> List[Dict[str, Any]]:
        """List Management Dashboards (auto-paginates)."""
        await self._rate_limiter.acquire()
        cid = compartment_id or self._compartment_id
        try:
            response = list_call_get_all_results(
                self.dashx_client.list_management_dashboards,
                compartment_id=cid,
            )
            self._rate_limiter.reset()
            return [
                {"id": d.id, "display_name": getattr(d, "display_name", ""),
                 "description": getattr(d, "description", ""),
                 "lifecycle_state": getattr(d, "lifecycle_state", "")}
                for d in _get_items(response.data)
            ]
        except oci.exceptions.ServiceError as e:
            if e.status == 429:
                await self._rate_limiter.handle_rate_limit()
                return await self.list_management_dashboards(compartment_id)
            raise

    async def get_management_dashboard(self, dashboard_id: str) -> Dict[str, Any]:
        """Get a specific Management Dashboard including its tiles."""
        await self._rate_limiter.acquire()
        try:
            response = self.dashx_client.get_management_dashboard(
                management_dashboard_id=dashboard_id
            )
            self._rate_limiter.reset()
            data = response.data
            tiles = []
            for t in getattr(data, "tiles", []) or []:
                tiles.append({
                    "display_name": getattr(t, "display_name", ""),
                    "saved_search_id": getattr(t, "saved_search_id", ""),
                    "row": getattr(t, "row", 0),
                    "column": getattr(t, "column", 0),
                    "height": getattr(t, "height", 4),
                    "width": getattr(t, "width", 6),
                })
            return {"id": data.id, "display_name": getattr(data, "display_name", ""),
                    "description": getattr(data, "description", ""),
                    "tiles": tiles,
                    "_etag": getattr(response, "etag", None)}
        except oci.exceptions.ServiceError as e:
            if e.status == 429:
                await self._rate_limiter.handle_rate_limit()
                return await self.get_management_dashboard(dashboard_id)
            raise

    async def update_management_dashboard(self, dashboard_id: str, details, if_match=None) -> Dict[str, Any]:
        """Update a Management Dashboard."""
        await self._rate_limiter.acquire()
        kwargs = {"management_dashboard_id": dashboard_id,
                  "update_management_dashboard_details": details}
        if if_match:
            kwargs["if_match"] = if_match
        try:
            response = self.dashx_client.update_management_dashboard(**kwargs)
            self._rate_limiter.reset()
            data = response.data
            return {"id": data.id, "display_name": getattr(data, "display_name", "")}
        except oci.exceptions.ServiceError as e:
            if e.status == 429:
                await self._rate_limiter.handle_rate_limit()
                return await self.update_management_dashboard(dashboard_id, details, if_match)
            raise

    async def delete_management_dashboard(self, dashboard_id: str) -> None:
        """Delete a Management Dashboard."""
        await self._rate_limiter.acquire()
        try:
            self.dashx_client.delete_management_dashboard(
                management_dashboard_id=dashboard_id
            )
            self._rate_limiter.reset()
        except oci.exceptions.ServiceError as e:
            if e.status == 429:
                await self._rate_limiter.handle_rate_limit()
                return await self.delete_management_dashboard(dashboard_id)
            raise
