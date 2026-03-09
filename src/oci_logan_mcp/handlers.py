"""MCP request handlers for tool and resource operations."""

import json
import logging
from typing import Any, Dict, List

from .query_engine import QueryEngine
from .schema_manager import SchemaManager
from .visualization import VisualizationEngine, ChartType
from .validator import QueryValidator
from .saved_search import SavedSearchService
from .export import ExportService
from .client import OCILogAnalyticsClient
from .cache import CacheManager
from .query_logger import QueryLogger
from .context_manager import ContextManager
from .query_auto_saver import QueryAutoSaver
from .config import Settings, save_config
from .resources import get_query_templates, get_syntax_guide, get_reference_docs

logger = logging.getLogger(__name__)


class MCPHandlers:
    """Handlers for MCP tool and resource requests."""

    def __init__(
        self,
        settings: Settings,
        oci_client: OCILogAnalyticsClient,
        cache: CacheManager,
        query_logger: QueryLogger,
        context_manager: ContextManager,
    ):
        """Initialize MCP handlers."""
        self.settings = settings
        self.oci_client = oci_client
        self.cache = cache
        self.query_logger = query_logger
        self.context_manager = context_manager

        # Initialize services
        self.schema_manager = SchemaManager(oci_client, cache)
        self.query_engine = QueryEngine(oci_client, cache, query_logger)
        self.validator = QueryValidator(self.schema_manager)
        self.visualization = VisualizationEngine()
        self.saved_search = SavedSearchService(oci_client, cache)
        self.export_service = ExportService()
        self.auto_saver = QueryAutoSaver(context_manager)

    async def handle_tool_call(
        self, name: str, arguments: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Route tool calls to appropriate handlers."""
        handlers = {
            # Schema exploration
            "list_log_sources": self._list_log_sources,
            "list_fields": self._list_fields,
            "list_entities": self._list_entities,
            "list_parsers": self._list_parsers,
            "list_labels": self._list_labels,
            "list_saved_searches": self._list_saved_searches,
            "list_log_groups": self._list_log_groups,
            # Query execution
            "validate_query": self._validate_query,
            "run_query": self._run_query,
            "run_saved_search": self._run_saved_search,
            "run_batch_queries": self._run_batch_queries,
            # Visualization
            "visualize": self._visualize,
            # Export
            "export_results": self._export_results,
            # Configuration
            "set_compartment": self._set_compartment,
            "set_namespace": self._set_namespace,
            "get_current_context": self._get_current_context,
            "list_compartments": self._list_compartments,
            # Helper tools
            "test_connection": self._test_connection,
            "find_compartment": self._find_compartment,
            "get_query_examples": self._get_query_examples,
            "get_log_summary": self._get_log_summary,
            # Memory & context
            "save_learned_query": self._save_learned_query,
            "list_learned_queries": self._list_learned_queries,
            "update_tenancy_context": self._update_tenancy_context,
            "delete_learned_query": self._delete_learned_query,
        }

        handler = handlers.get(name)
        if not handler:
            return [{"type": "text", "text": f"Unknown tool: {name}"}]

        try:
            result = await handler(arguments)
            return result
        except Exception as e:
            logger.exception(f"Error in tool {name}")
            return [{"type": "text", "text": f"Error executing {name}: {str(e)}"}]

    async def handle_resource_read(self, uri: str) -> Any:
        """Handle resource read requests."""
        if uri == "loganalytics://schema":
            return await self.schema_manager.get_full_schema()
        elif uri == "loganalytics://query-templates":
            builtin = get_query_templates()
            return {
                "templates": self.context_manager.get_all_templates(
                    builtin.get("templates", [])
                )
            }
        elif uri == "loganalytics://syntax-guide":
            return get_syntax_guide()
        elif uri == "loganalytics://recent-queries":
            return self.query_logger.get_recent_queries(limit=10)
        elif uri == "loganalytics://tenancy-context":
            return self.context_manager.get_tenancy_context()
        elif uri == "loganalytics://reference-docs":
            return get_reference_docs()
        else:
            raise ValueError(f"Unknown resource: {uri}")

    # Tool implementations

    async def _list_log_sources(self, args: Dict) -> List[Dict]:
        """List log sources."""
        sources = await self.schema_manager.get_log_sources(
            compartment_id=args.get("compartment_id")
        )
        # Auto-capture to tenancy context
        self.context_manager.update_log_sources(sources)
        return [{"type": "text", "text": json.dumps(sources, indent=2)}]

    async def _list_fields(self, args: Dict) -> List[Dict]:
        """List fields."""
        fields = await self.schema_manager.get_fields(source_name=args.get("source_name"))
        field_dicts = [
            {
                "name": f.name,
                "data_type": f.data_type,
                "description": f.description,
                "possible_values": f.possible_values,
                "hint": f.hint,
            }
            for f in fields
        ]
        # Auto-capture to tenancy context
        self.context_manager.update_confirmed_fields(field_dicts)
        return [{"type": "text", "text": json.dumps(field_dicts, indent=2)}]

    async def _list_entities(self, args: Dict) -> List[Dict]:
        """List entities."""
        entities = await self.schema_manager.get_entities(
            entity_type=args.get("entity_type")
        )
        return [{"type": "text", "text": json.dumps(entities, indent=2)}]

    async def _list_parsers(self, args: Dict) -> List[Dict]:
        """List parsers."""
        parsers = await self.schema_manager.get_parsers()
        return [{"type": "text", "text": json.dumps(parsers, indent=2)}]

    async def _list_labels(self, args: Dict) -> List[Dict]:
        """List labels."""
        labels = await self.schema_manager.get_labels()
        return [{"type": "text", "text": json.dumps(labels, indent=2)}]

    async def _list_saved_searches(self, args: Dict) -> List[Dict]:
        """List saved searches."""
        searches = await self.saved_search.list_searches()
        return [{"type": "text", "text": json.dumps(searches, indent=2)}]

    async def _list_log_groups(self, args: Dict) -> List[Dict]:
        """List log groups."""
        groups = await self.oci_client.list_log_groups()
        return [{"type": "text", "text": json.dumps(groups, indent=2)}]

    async def _validate_query(self, args: Dict) -> List[Dict]:
        """Validate a query."""
        result = await self.validator.validate(
            query=args["query"],
            time_start=args.get("time_start"),
            time_end=args.get("time_end"),
        )
        result_dict = {
            "valid": result.valid,
            "errors": result.errors,
            "warnings": result.warnings,
            "suggestions": result.suggestions,
            "estimated_cost": result.estimated_cost,
            "suggested_fix": result.suggested_fix,
        }
        return [{"type": "text", "text": json.dumps(result_dict, indent=2)}]

    def _resolve_scope(self, args: Dict) -> tuple:
        """Resolve scope parameter to compartment_id and include_subcompartments."""
        scope = args.get("scope", "default")
        compartment_id = args.get("compartment_id")
        include_subs = args.get("include_subcompartments", True)

        if isinstance(include_subs, str):
            include_subs = include_subs.lower() in ("true", "yes", "1")

        if scope == "tenancy":
            tenancy_id = self.oci_client._config.get("tenancy")
            if tenancy_id:
                compartment_id = tenancy_id
                include_subs = True
                logger.info(f"Scope=tenancy: using tenancy OCID {tenancy_id[:50]}...")

        return compartment_id, include_subs

    async def _run_query(self, args: Dict) -> List[Dict]:
        """Execute a query."""
        compartment_id, include_subs = self._resolve_scope(args)

        logger.info(f"run_query: include_subcompartments={include_subs}, compartment_id={compartment_id}, args={args}")

        result = await self.query_engine.execute(
            query=args["query"],
            time_range=args.get("time_range"),
            time_start=args.get("time_start"),
            time_end=args.get("time_end"),
            max_results=args.get("max_results"),
            include_subcompartments=include_subs,
            compartment_id=compartment_id,
        )
        # Auto-save interesting queries / bump usage for existing ones
        self.auto_saver.process_successful_query(args["query"], result)

        # Use compact formatter for cluster queries
        if self._is_cluster_query(args["query"]):
            formatted = self._format_cluster_result(result)
            return [{"type": "text", "text": json.dumps(formatted, indent=2, default=str)}]

        return [{"type": "text", "text": json.dumps(result, indent=2, default=str)}]

    @staticmethod
    def _is_cluster_query(query: str) -> bool:
        """Check if query is a cluster command."""
        # Match "| cluster" as a pipe command, ignoring case
        import re
        return bool(re.search(r'\|\s*cluster\b', query, re.IGNORECASE))

    @staticmethod
    def _format_cluster_result(result: Dict) -> Dict:
        """Format cluster results into a compact summary with all real numbers.

        Strips verbose fields (Trend arrays, long samples) while preserving
        every cluster row and all numeric data.
        """
        data = result.get("data", {})
        rows = data.get("rows", [])
        columns = data.get("columns", [])
        metadata = result.get("metadata", {})

        # Build column index map
        col_idx = {col["name"]: i for i, col in enumerate(columns)}

        def _get(row, name, default=None):
            idx = col_idx.get(name)
            if idx is not None and idx < len(row):
                return row[idx]
            return default

        def _clean_sample(sample: str, max_len: int = 80) -> str:
            """Strip cluster template markup and truncate."""
            if not sample:
                return ""
            import re
            # Remove <#v ...>...</#v> markup, keep inner text
            cleaned = re.sub(r'<#v[^>]*>', '', sample)
            cleaned = cleaned.replace('</#v>', '')
            cleaned = ' '.join(cleaned.split())  # normalize whitespace
            if len(cleaned) > max_len:
                cleaned = cleaned[:max_len] + "..."
            return cleaned

        clusters = []
        for row in rows:
            cluster = {
                "id": _get(row, "ID"),
                "count": _get(row, "Count"),
                "log_source": _get(row, "Log Source"),
                "sample": _clean_sample(_get(row, "Cluster Sample", "")),
                "potential_issue": _get(row, "Potential Issue"),
                "problem_priority": _get(row, "Problem Priority"),
            }
            clusters.append(cluster)

        # Sort by count descending
        clusters.sort(key=lambda c: c.get("count") or 0, reverse=True)

        total_logs = sum(c.get("count") or 0 for c in clusters)

        return {
            "total_clusters": len(clusters),
            "total_log_records": total_logs,
            "metadata": metadata,
            "clusters": clusters,
        }

    async def _run_saved_search(self, args: Dict) -> List[Dict]:
        """Run a saved search."""
        search_id = args.get("id")
        search_name = args.get("name")

        if not search_id and search_name:
            search = await self.saved_search.get_search_by_name(search_name)
            if search:
                search_id = search.get("id")

        if not search_id:
            return [{"type": "text", "text": "Saved search not found"}]

        saved = await self.saved_search.get_search_by_id(search_id)
        query = saved.get("query", "")

        if not query:
            return [{"type": "text", "text": "Saved search has no query defined"}]

        result = await self.query_engine.execute(
            query=query, time_range="last_1_hour"
        )
        return [{"type": "text", "text": json.dumps(result, indent=2, default=str)}]

    async def _run_batch_queries(self, args: Dict) -> List[Dict]:
        """Run batch queries."""
        results = await self.query_engine.execute_batch(
            args["queries"],
            include_subcompartments=args.get("include_subcompartments", True),
            compartment_id=args.get("compartment_id"),
        )
        # Auto-save interesting queries from batch results
        for q_spec, r in zip(args["queries"], results):
            if isinstance(r, dict) and "error" not in r:
                self.auto_saver.process_successful_query(q_spec["query"], r)
        return [{"type": "text", "text": json.dumps(results, indent=2, default=str)}]

    async def _visualize(self, args: Dict) -> List[Dict]:
        """Generate visualization."""
        compartment_id, include_subs = self._resolve_scope(args)

        query_result = await self.query_engine.execute(
            query=args["query"],
            time_range=args.get("time_range", "last_1_hour"),
            time_start=args.get("time_start"),
            time_end=args.get("time_end"),
            include_subcompartments=include_subs,
            compartment_id=compartment_id,
        )

        data = query_result.get("data", {})
        row_count = len(data.get("rows", []))
        col_count = len(data.get("columns", []))
        logger.info(f"Visualize: Query returned {row_count} rows, {col_count} columns")

        # Auto-save interesting queries
        self.auto_saver.process_successful_query(args["query"], query_result)

        chart_type = ChartType(args["chart_type"])
        viz_result = self.visualization.generate(
            data=data,
            chart_type=chart_type,
            title=args.get("title"),
        )

        return [
            {
                "type": "image",
                "data": viz_result["image_base64"],
                "mimeType": "image/png",
            },
            {
                "type": "text",
                "text": f"Raw data ({len(viz_result['raw_data'])} records): "
                + json.dumps(viz_result["raw_data"][:10], indent=2, default=str),
            },
        ]

    async def _export_results(self, args: Dict) -> List[Dict]:
        """Export query results."""
        compartment_id, include_subs = self._resolve_scope(args)

        result = await self.query_engine.execute(
            query=args["query"],
            time_range=args.get("time_range", "last_1_hour"),
            time_start=args.get("time_start"),
            time_end=args.get("time_end"),
            include_subcompartments=include_subs,
            compartment_id=compartment_id,
        )

        # Auto-save interesting queries
        self.auto_saver.process_successful_query(args["query"], result)

        exported = self.export_service.export(
            data=result["data"], format=args["format"]
        )
        return [{"type": "text", "text": exported}]

    async def _set_compartment(self, args: Dict) -> List[Dict]:
        """Set compartment context and persist to config."""
        new_id = args["compartment_id"]
        self.oci_client.compartment_id = new_id
        self.settings.log_analytics.default_compartment_id = new_id
        self.cache.clear()

        try:
            save_config(self.settings)
            logger.info(f"Persisted default compartment to config: {new_id}")
        except Exception as e:
            logger.warning(f"Failed to persist compartment to config: {e}")

        return [
            {"type": "text", "text": f"Compartment set to: {new_id}"}
        ]

    async def _set_namespace(self, args: Dict) -> List[Dict]:
        """Set namespace context."""
        self.oci_client.namespace = args["namespace"]
        self.cache.clear()
        return [{"type": "text", "text": f"Namespace set to: {args['namespace']}"}]

    async def _get_current_context(self, args: Dict) -> List[Dict]:
        """Get current context."""
        context = {
            "namespace": self.oci_client.namespace,
            "compartment_id": self.oci_client.compartment_id,
            "default_time_range": self.settings.query.default_time_range,
            "max_results": self.settings.query.max_results,
        }
        return [{"type": "text", "text": json.dumps(context, indent=2)}]

    async def _list_compartments(self, args: Dict) -> List[Dict]:
        """List compartments."""
        compartments = await self.oci_client.list_compartments()
        # Auto-capture to tenancy context
        self.context_manager.update_compartments(compartments)
        return [{"type": "text", "text": json.dumps(compartments, indent=2)}]

    async def _test_connection(self, args: Dict) -> List[Dict]:
        """Test connection to OCI Log Analytics."""
        result = {
            "status": "unknown",
            "checks": [],
            "context": {},
            "sample_query": None,
        }

        try:
            result["context"] = {
                "namespace": self.oci_client.namespace,
                "compartment_id": self.oci_client.compartment_id,
                "tenancy_id": self.oci_client._config.get("tenancy", "unknown"),
            }
            result["checks"].append({"name": "Configuration loaded", "status": "OK"})

            try:
                compartments = await self.oci_client.list_compartments()
                result["checks"].append({
                    "name": "Identity API (list compartments)",
                    "status": f"OK - Found {len(compartments)} compartments"
                })
            except Exception as e:
                result["checks"].append({
                    "name": "Identity API",
                    "status": f"FAILED - {str(e)[:100]}"
                })

            try:
                sources = await self.oci_client.list_log_sources()
                result["checks"].append({
                    "name": "Log Analytics API (list sources)",
                    "status": f"OK - Found {len(sources)} log sources"
                })
            except Exception as e:
                result["checks"].append({
                    "name": "Log Analytics API",
                    "status": f"FAILED - {str(e)[:100]}"
                })

            try:
                query_result = await self.query_engine.execute(
                    query="* | stats count",
                    time_range="last_1_hour",
                    use_cache=False,
                )
                data = query_result.get("data", {})
                rows = data.get("rows", [])
                count = data.get("total_count") or (rows[0][0] if rows and rows[0] else 0)
                result["checks"].append({
                    "name": "Query execution",
                    "status": f"OK - {count:,} logs in last hour"
                })
                result["sample_query"] = {
                    "query": "* | stats count",
                    "time_range": "last_1_hour",
                    "result_count": count,
                }
            except Exception as e:
                result["checks"].append({
                    "name": "Query execution",
                    "status": f"FAILED - {str(e)[:100]}"
                })

            failed = [c for c in result["checks"] if "FAILED" in c["status"]]
            if not failed:
                result["status"] = "All systems operational"
            else:
                result["status"] = f"{len(failed)} check(s) failed"

        except Exception as e:
            result["status"] = f"Connection test failed: {str(e)}"

        return [{"type": "text", "text": json.dumps(result, indent=2)}]

    async def _find_compartment(self, args: Dict) -> List[Dict]:
        """Find compartment by name using fuzzy matching."""
        search_name = args.get("name", "").lower()

        if not search_name:
            return [{"type": "text", "text": json.dumps({
                "error": "Please provide a compartment name to search for"
            }, indent=2)}]

        try:
            compartments = await self.oci_client.list_compartments()

            matches = []
            for comp in compartments:
                name = comp.get("name", "").lower()
                score = 0

                if name == search_name:
                    score = 100
                elif name.startswith(search_name):
                    score = 80
                elif search_name in name:
                    score = 60
                elif any(word in name for word in search_name.split()):
                    score = 40

                if score > 0:
                    matches.append({
                        "name": comp.get("name"),
                        "id": comp.get("id"),
                        "description": comp.get("description", ""),
                        "match_score": score,
                    })

            matches.sort(key=lambda x: x["match_score"], reverse=True)

            if matches:
                result = {
                    "found": len(matches),
                    "matches": matches[:10],
                    "hint": "Use the 'id' field as compartment_id in your queries",
                }
            else:
                result = {
                    "found": 0,
                    "matches": [],
                    "suggestion": f"No compartments matching '{args.get('name')}'. Use list_compartments to see all available compartments.",
                }

            return [{"type": "text", "text": json.dumps(result, indent=2)}]

        except Exception as e:
            return [{"type": "text", "text": json.dumps({
                "error": f"Failed to search compartments: {str(e)}"
            }, indent=2)}]

    async def _get_query_examples(self, args: Dict) -> List[Dict]:
        """Get example queries for common use cases."""
        examples = {
            "basic": [
                {
                    "name": "Count all logs",
                    "query": "* | stats count",
                    "description": "Get total count of all logs",
                },
                {
                    "name": "Count by log source",
                    "query": "* | stats count by 'Log Source'",
                    "description": "Break down log count by source type",
                },
                {
                    "name": "Recent logs",
                    "query": "* | head 100",
                    "description": "Get the 100 most recent log entries",
                },
                {
                    "name": "Search by keyword",
                    "query": "* | where Message contains 'error'",
                    "description": "Find logs containing specific text",
                },
            ],
            "security": [
                {
                    "name": "Failed logins",
                    "query": "'Log Source' = 'Linux Secure Logs' | where Message contains 'Failed password'",
                    "description": "Find failed SSH login attempts",
                },
                {
                    "name": "Authentication events",
                    "query": "* | where Label = 'Authentication'",
                    "description": "All authentication-related events",
                },
                {
                    "name": "Sudo commands",
                    "query": "'Log Source' = 'Linux Secure Logs' | where Message contains 'sudo'",
                    "description": "Track sudo usage",
                },
            ],
            "errors": [
                {
                    "name": "All errors",
                    "query": "* | where Severity in ('ERROR', 'CRITICAL', 'FATAL')",
                    "description": "Find all error-level logs",
                },
                {
                    "name": "Errors by source",
                    "query": "* | where Severity = 'ERROR' | stats count by 'Log Source'",
                    "description": "Count errors per log source",
                },
                {
                    "name": "Error trends",
                    "query": "* | where Severity = 'ERROR' | timestats count by 'Log Source'",
                    "description": "Error count over time by source",
                },
                {
                    "name": "Exception traces",
                    "query": "* | where Message contains 'Exception' or Message contains 'Traceback'",
                    "description": "Find stack traces and exceptions",
                },
            ],
            "performance": [
                {
                    "name": "Slow operations",
                    "query": "* | where Message contains 'slow' or Message contains 'timeout'",
                    "description": "Find performance-related issues",
                },
                {
                    "name": "Response times",
                    "query": "* | where Message regex '\\\\d+ms' | head 100",
                    "description": "Logs mentioning millisecond timings",
                },
            ],
            "statistics": [
                {
                    "name": "Logs by entity",
                    "query": "* | stats count by Entity",
                    "description": "Count logs per monitored entity",
                },
                {
                    "name": "Logs by severity",
                    "query": "* | stats count by Severity",
                    "description": "Distribution of log severity levels",
                },
                {
                    "name": "Top log sources",
                    "query": "* | stats count by 'Log Source' | sort -count | head 10",
                    "description": "Top 10 log sources by volume",
                },
                {
                    "name": "Hourly volume",
                    "query": "* | timestats count span=1h",
                    "description": "Log volume per hour",
                },
            ],
        }

        category = args.get("category", "all")

        if category == "all":
            result = {
                "categories": list(examples.keys()),
                "examples": examples,
                "tip": "Use these as starting points. Modify the queries based on your specific log sources and fields.",
            }
        elif category in examples:
            result = {
                "category": category,
                "examples": examples[category],
            }
        else:
            result = {
                "error": f"Unknown category '{category}'",
                "available": list(examples.keys()),
            }

        return [{"type": "text", "text": json.dumps(result, indent=2)}]

    async def _get_log_summary(self, args: Dict) -> List[Dict]:
        """Get summary of available log data."""
        time_range = args.get("time_range", "last_24_hours")
        compartment_id, include_subs = self._resolve_scope(args)

        try:
            result = await self.query_engine.execute(
                query="* | stats count by 'Log Source' | sort -count",
                time_range=time_range,
                include_subcompartments=include_subs,
                compartment_id=compartment_id,
                use_cache=False,
            )

            data = result.get("data", {})
            rows = data.get("rows", [])
            columns = data.get("columns", [])

            sources_with_data = []
            total_logs = 0

            source_idx = 0
            count_idx = 1
            for i, col in enumerate(columns):
                if col.get("name") == "Log Source":
                    source_idx = i
                elif col.get("name") == "count":
                    count_idx = i

            for row in rows:
                if len(row) > max(source_idx, count_idx):
                    source_name = row[source_idx]
                    count = int(row[count_idx]) if row[count_idx] else 0
                    if count > 0:
                        sources_with_data.append({
                            "source": source_name,
                            "count": count,
                        })
                        total_logs += count

            summary = {
                "time_range": time_range,
                "scope": "tenancy" if args.get("scope") == "tenancy" else "default",
                "compartment_id": result.get("metadata", {}).get("compartment_id", "unknown"),
                "total_logs": total_logs,
                "sources_with_data": len(sources_with_data),
                "top_sources": sources_with_data[:10],
                "recommendation": self._get_summary_recommendation(sources_with_data, total_logs),
            }

            return [{"type": "text", "text": json.dumps(summary, indent=2)}]

        except Exception as e:
            return [{"type": "text", "text": json.dumps({
                "error": f"Failed to get log summary: {str(e)}",
                "suggestion": "Try running test_connection first to verify connectivity",
            }, indent=2)}]

    def _get_summary_recommendation(self, sources: list, total: int) -> str:
        """Generate recommendation based on log summary."""
        if total == 0:
            return "No logs found in this time range. Try a longer time range or check scope."
        elif len(sources) == 1:
            return f"Only one log source has data: {sources[0]['source']}. Queries will be focused on this source."
        elif len(sources) > 10:
            return f"You have {len(sources)} active log sources. Consider filtering by 'Log Source' for better performance."
        else:
            top_source = sources[0]['source'] if sources else "N/A"
            return f"Top log source is '{top_source}'. Use list_log_sources to see all available sources."

    # Memory & context tools

    async def _save_learned_query(self, args: Dict) -> List[Dict]:
        """Save a working query for future reference."""
        saved = self.context_manager.save_learned_query(
            name=args["name"],
            query=args["query"],
            description=args["description"],
            category=args.get("category", "general"),
            tags=args.get("tags"),
        )
        return [{"type": "text", "text": json.dumps({
            "status": "saved",
            "query": saved,
            "message": f"Query '{args['name']}' saved. It will be available in future sessions.",
        }, indent=2, default=str)}]

    async def _list_learned_queries(self, args: Dict) -> List[Dict]:
        """List previously saved learned queries."""
        queries = self.context_manager.list_learned_queries(
            category=args.get("category"),
            tag=args.get("tag"),
        )
        return [{"type": "text", "text": json.dumps({
            "count": len(queries),
            "queries": queries,
        }, indent=2, default=str)}]

    async def _update_tenancy_context(self, args: Dict) -> List[Dict]:
        """Update persistent tenancy context."""
        updated = []

        if notes := args.get("notes"):
            for note in notes:
                self.context_manager.add_note(note)
            updated.append(f"Added {len(notes)} note(s)")

        if fields := args.get("confirmed_fields"):
            summary = self.context_manager.update_confirmed_fields(fields)
            updated.append(f"Updated fields: {summary}")

        return [{"type": "text", "text": json.dumps({
            "status": "updated",
            "changes": updated,
            "message": "Tenancy context updated. Changes persist across sessions.",
        }, indent=2)}]

    async def _delete_learned_query(self, args: Dict) -> List[Dict]:
        """Delete a learned query by name."""
        deleted = self.context_manager.delete_learned_query(args["name"])
        if deleted:
            return [{"type": "text", "text": json.dumps({
                "status": "deleted",
                "message": f"Query '{args['name']}' deleted.",
            }, indent=2)}]
        else:
            return [{"type": "text", "text": json.dumps({
                "status": "not_found",
                "message": f"No learned query named '{args['name']}' found.",
            }, indent=2)}]
