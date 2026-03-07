#!/usr/bin/env python3
"""
Automated test suite for OCI Log Analytics MCP Server.
Run this directly on the VM to test all functionality.

Usage:
    cd /path/to/logan-mcp-server
    source venv/bin/activate
    python run_tests.py
"""

import asyncio
import json
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, Any, List, Tuple

# Add src to path
sys.path.insert(0, 'src')

from oci_logan_mcp.config import load_config, Settings
from oci_logan_mcp.client import OCILogAnalyticsClient
from oci_logan_mcp.cache import CacheManager
from oci_logan_mcp.query_logger import QueryLogger
from oci_logan_mcp.handlers import MCPHandlers


class TestResult:
    def __init__(self, test_id: str, name: str, priority: str):
        self.test_id = test_id
        self.name = name
        self.priority = priority
        self.passed = False
        self.error = None
        self.details = None
        self.duration = 0.0

    def __str__(self):
        status = "PASS" if self.passed else "FAIL"
        result = f"[{self.priority}] {self.test_id}: {self.name} - {status}"
        if not self.passed and self.error:
            result += f"\n    Error: {self.error}"
        if self.details:
            result += f"\n    Details: {self.details}"
        return result


class MCPServerTester:
    def __init__(self):
        self.results: List[TestResult] = []
        self.handlers: MCPHandlers = None
        self.settings: Settings = None

    async def setup(self):
        """Initialize the MCP handlers."""
        print("=" * 60)
        print("OCI Log Analytics MCP Server - Automated Test Suite")
        print("=" * 60)
        print("\nInitializing...")

        try:
            self.settings = load_config()
            oci_client = OCILogAnalyticsClient(self.settings)
            cache = CacheManager()
            query_logger = QueryLogger()
            self.handlers = MCPHandlers(self.settings, oci_client, cache, query_logger)
            print("MCP handlers initialized successfully\n")
            return True
        except Exception as e:
            print(f"Failed to initialize: {e}")
            return False

    async def run_test(self, test_id: str, name: str, priority: str,
                       tool_name: str, args: Dict[str, Any],
                       validator: callable = None) -> TestResult:
        """Run a single test case."""
        result = TestResult(test_id, name, priority)
        start_time = time.time()

        try:
            response = await self.handlers.handle_tool_call(tool_name, args)
            result.duration = time.time() - start_time

            # Parse response
            if response and len(response) > 0:
                text_content = response[0].get("text", "")
                try:
                    data = json.loads(text_content)
                except json.JSONDecodeError:
                    data = text_content

                # Run validator if provided
                if validator:
                    passed, details = validator(data)
                    result.passed = passed
                    result.details = details
                else:
                    result.passed = True
                    result.details = f"Response received ({len(text_content)} chars)"
            else:
                result.passed = False
                result.error = "Empty response"

        except Exception as e:
            result.duration = time.time() - start_time
            result.passed = False
            result.error = str(e)

        self.results.append(result)
        print(result)
        return result

    # ========== VALIDATORS ==========

    @staticmethod
    def validate_has_data(data: Any) -> Tuple[bool, str]:
        """Validate that response has data."""
        if isinstance(data, dict):
            if "data" in data or "metadata" in data:
                return True, "Has data/metadata"
            if len(data) > 0:
                return True, f"Dict with {len(data)} keys"
        if isinstance(data, list):
            return len(data) > 0, f"List with {len(data)} items"
        if isinstance(data, str):
            return len(data) > 0, f"String with {len(data)} chars"
        return False, "Unknown data type"

    @staticmethod
    def validate_has_metadata(data: Any) -> Tuple[bool, str]:
        """Validate that response has metadata with required fields."""
        if not isinstance(data, dict):
            return False, "Response is not a dict"
        metadata = data.get("metadata", {})
        if not metadata:
            return False, "No metadata in response"

        required = ["query", "compartment_id", "time_start", "time_end"]
        missing = [f for f in required if f not in metadata]
        if missing:
            return False, f"Missing metadata fields: {missing}"
        return True, f"Metadata OK: compartment={metadata['compartment_id'][:50]}..."

    @staticmethod
    def validate_tenancy_scope(data: Any) -> Tuple[bool, str]:
        """Validate that tenancy scope was used."""
        if not isinstance(data, dict):
            return False, "Response is not a dict"
        metadata = data.get("metadata", {})
        compartment_id = metadata.get("compartment_id", "")

        if compartment_id.startswith("ocid1.tenancy."):
            return True, f"Tenancy OCID used: {compartment_id[:50]}..."
        return False, f"Expected tenancy OCID, got: {compartment_id[:50]}..."

    @staticmethod
    def validate_log_count(data: Any) -> Tuple[bool, str]:
        """Validate that we got a log count."""
        if not isinstance(data, dict):
            return False, "Response is not a dict"

        result_data = data.get("data", {})
        total_count = result_data.get("total_count", 0)
        rows = result_data.get("rows", [])

        if total_count > 0 or len(rows) > 0:
            return True, f"total_count={total_count}, rows={len(rows)}"
        return False, "No logs found (may be OK if compartment is empty)"

    @staticmethod
    def validate_compartments_list(data: Any) -> Tuple[bool, str]:
        """Validate compartments list."""
        if isinstance(data, list):
            if len(data) > 0:
                first = data[0]
                if isinstance(first, dict) and "id" in first:
                    return True, f"Found {len(data)} compartments"
            return False, "Empty compartments list"
        return False, "Expected list of compartments"

    @staticmethod
    def validate_sources_list(data: Any) -> Tuple[bool, str]:
        """Validate log sources list."""
        if isinstance(data, list):
            return len(data) > 0, f"Found {len(data)} log sources"
        return False, "Expected list of sources"

    # ========== TEST CASES ==========

    async def run_all_tests(self):
        """Run all test cases."""

        print("\n" + "=" * 60)
        print("PHASE 1: Connectivity & Configuration (P0)")
        print("=" * 60 + "\n")

        # C1: Get current context
        await self.run_test(
            "C1", "Get current context", "P0",
            "get_current_context", {},
            self.validate_has_data
        )

        # C2: List compartments
        await self.run_test(
            "C2", "List compartments", "P0",
            "list_compartments", {},
            self.validate_compartments_list
        )

        # C3: List log sources
        await self.run_test(
            "C3", "List log sources", "P0",
            "list_log_sources", {},
            self.validate_sources_list
        )

        print("\n" + "=" * 60)
        print("PHASE 2: Basic Query Execution (P0)")
        print("=" * 60 + "\n")

        # Q1: Simple query with metadata
        await self.run_test(
            "Q1", "Simple query with metadata", "P0",
            "run_query", {
                "query": "* | stats count",
                "time_range": "last_1_hour"
            },
            self.validate_has_metadata
        )

        # Q2: Query by source
        await self.run_test(
            "Q2", "Query by log source", "P0",
            "run_query", {
                "query": "* | stats count by 'Log Source'",
                "time_range": "last_24_hours"
            },
            self.validate_has_metadata
        )

        print("\n" + "=" * 60)
        print("PHASE 3: Scope & Compartment Handling (P0 - CRITICAL)")
        print("=" * 60 + "\n")

        # SC1: Default compartment query
        default_result = await self.run_test(
            "SC1", "Default compartment query", "P0",
            "run_query", {
                "query": "* | stats count",
                "time_range": "last_7_days"
            },
            self.validate_has_metadata
        )

        # SC2: Tenancy-wide query (scope=tenancy)
        tenancy_result = await self.run_test(
            "SC2", "Tenancy-wide query (scope=tenancy)", "P0",
            "run_query", {
                "query": "* | stats count",
                "time_range": "last_7_days",
                "scope": "tenancy"
            },
            self.validate_tenancy_scope
        )

        # SC3: Compare counts
        print("\n[P0] SC3: Compare default vs tenancy counts")
        try:
            # Run fresh queries to get actual counts
            default_resp = await self.handlers.handle_tool_call("run_query", {
                "query": "* | stats count",
                "time_range": "last_7_days"
            })
            default_json = json.loads(default_resp[0]["text"])
            default_count = default_json.get("data", {}).get("total_count", 0)

            tenancy_resp = await self.handlers.handle_tool_call("run_query", {
                "query": "* | stats count",
                "time_range": "last_7_days",
                "scope": "tenancy"
            })
            tenancy_json = json.loads(tenancy_resp[0]["text"])
            tenancy_count = tenancy_json.get("data", {}).get("total_count", 0)

            result = TestResult("SC3", "Compare default vs tenancy counts", "P0")
            if tenancy_count >= default_count:
                result.passed = True
                result.details = f"Default: {default_count:,} | Tenancy: {tenancy_count:,} (OK - tenancy >= default)"
            else:
                result.passed = False
                result.error = f"Tenancy count ({tenancy_count:,}) < Default count ({default_count:,})"

            self.results.append(result)
            print(result)

        except Exception as e:
            result = TestResult("SC3", "Compare default vs tenancy counts", "P0")
            result.passed = False
            result.error = str(e)
            self.results.append(result)
            print(result)

        # SC4: Verify metadata shows correct compartment
        await self.run_test(
            "SC4", "Metadata shows compartment used", "P0",
            "run_query", {
                "query": "* | stats count",
                "time_range": "last_1_hour"
            },
            self.validate_has_metadata
        )

        print("\n" + "=" * 60)
        print("PHASE 4: Time Range Handling (P1)")
        print("=" * 60 + "\n")

        time_ranges = [
            ("last_15_min", "Last 15 minutes"),
            ("last_1_hour", "Last 1 hour"),
            ("last_24_hours", "Last 24 hours"),
            ("last_7_days", "Last 7 days"),
        ]

        for i, (time_range, desc) in enumerate(time_ranges):
            await self.run_test(
                f"T{i+1}", f"Time range: {desc}", "P1",
                "run_query", {
                    "query": "* | stats count",
                    "time_range": time_range
                },
                self.validate_has_metadata
            )

        # T5: Absolute time range
        now = datetime.utcnow()
        yesterday = now - timedelta(days=1)
        await self.run_test(
            "T5", "Absolute time range", "P1",
            "run_query", {
                "query": "* | stats count",
                "time_start": yesterday.isoformat() + "Z",
                "time_end": now.isoformat() + "Z"
            },
            self.validate_has_metadata
        )

        print("\n" + "=" * 60)
        print("PHASE 5: Schema Exploration (P1)")
        print("=" * 60 + "\n")

        # S1: List fields
        await self.run_test(
            "S1", "List fields", "P1",
            "list_fields", {},
            self.validate_has_data
        )

        # S2: List entities
        await self.run_test(
            "S2", "List entities", "P1",
            "list_entities", {},
            self.validate_has_data
        )

        # S3: List parsers
        await self.run_test(
            "S3", "List parsers", "P1",
            "list_parsers", {},
            self.validate_has_data
        )

        # S4: List labels
        await self.run_test(
            "S4", "List labels", "P1",
            "list_labels", {},
            self.validate_has_data
        )

        # S5: List log groups
        await self.run_test(
            "S5", "List log groups", "P1",
            "list_log_groups", {},
            self.validate_has_data
        )

        print("\n" + "=" * 60)
        print("PHASE 6: Visualization (P1)")
        print("=" * 60 + "\n")

        chart_types = ["pie", "bar", "line"]
        for i, chart_type in enumerate(chart_types):
            await self.run_test(
                f"V{i+1}", f"Visualization: {chart_type} chart", "P1",
                "visualize", {
                    "query": "* | stats count by 'Log Source'",
                    "chart_type": chart_type,
                    "time_range": "last_24_hours"
                },
                lambda d: (True, "Visualization generated") if d else (False, "No data")
            )

        # V4: Visualization with tenancy scope
        await self.run_test(
            "V4", "Visualization with tenancy scope", "P1",
            "visualize", {
                "query": "* | stats count by 'Log Source'",
                "chart_type": "pie",
                "time_range": "last_24_hours",
                "scope": "tenancy"
            },
            lambda d: (True, "Visualization generated") if d else (False, "No data")
        )

        print("\n" + "=" * 60)
        print("PHASE 7: Export (P1)")
        print("=" * 60 + "\n")

        # E1: Export to CSV
        await self.run_test(
            "E1", "Export to CSV", "P1",
            "export_results", {
                "query": "* | head 10",
                "format": "csv",
                "time_range": "last_1_hour"
            },
            lambda d: (True, f"CSV data ({len(str(d))} chars)") if d else (False, "No data")
        )

        # E2: Export to JSON
        await self.run_test(
            "E2", "Export to JSON", "P1",
            "export_results", {
                "query": "* | head 10",
                "format": "json",
                "time_range": "last_1_hour"
            },
            lambda d: (True, f"JSON data ({len(str(d))} chars)") if d else (False, "No data")
        )

        # E3: Export with tenancy scope
        await self.run_test(
            "E3", "Export with tenancy scope", "P1",
            "export_results", {
                "query": "* | stats count by 'Log Source'",
                "format": "json",
                "time_range": "last_24_hours",
                "scope": "tenancy"
            },
            lambda d: (True, f"JSON data ({len(str(d))} chars)") if d else (False, "No data")
        )

        print("\n" + "=" * 60)
        print("PHASE 8: Error Handling (P2)")
        print("=" * 60 + "\n")

        # ER1: Empty results (should not error)
        await self.run_test(
            "ER1", "Query with no results", "P2",
            "run_query", {
                "query": "* | where Message contains 'xyzzy123impossible456'",
                "time_range": "last_1_hour"
            },
            lambda d: (isinstance(d, dict), "Handled gracefully")
        )

        print("\n" + "=" * 60)
        print("PHASE 9: Real-World Scenarios (P1)")
        print("=" * 60 + "\n")

        # RW1: Error investigation
        await self.run_test(
            "RW1", "Error investigation query", "P1",
            "run_query", {
                "query": "* | where Severity in ('ERROR', 'CRITICAL') | stats count by Entity",
                "time_range": "last_24_hours"
            },
            self.validate_has_metadata
        )

        # RW2: Organization-wide summary
        await self.run_test(
            "RW2", "Organization-wide summary", "P1",
            "run_query", {
                "query": "* | stats count by 'Log Source'",
                "time_range": "last_24_hours",
                "scope": "tenancy"
            },
            self.validate_tenancy_scope
        )

        print("\n" + "=" * 60)
        print("PHASE 10: Helper Tools (P0-P1)")
        print("=" * 60 + "\n")

        # H1: Test connection (critical health check)
        await self.run_test(
            "H1", "Test connection (health check)", "P0",
            "test_connection", {},
            lambda d: (
                isinstance(d, dict) and "status" in d,
                f"Status: {d.get('status', 'unknown')}" if isinstance(d, dict) else "Invalid response"
            )
        )

        # H2: Find compartment by name
        await self.run_test(
            "H2", "Find compartment by name", "P1",
            "find_compartment", {"name": "prod"},
            lambda d: (isinstance(d, dict), f"Found {d.get('found', 0)} matches" if isinstance(d, dict) else "Error")
        )

        # H3: Get query examples
        await self.run_test(
            "H3", "Get query examples", "P1",
            "get_query_examples", {"category": "all"},
            lambda d: (
                isinstance(d, dict) and "examples" in d,
                f"Categories: {d.get('categories', [])}" if isinstance(d, dict) else "Error"
            )
        )

        # H4: Get log summary
        await self.run_test(
            "H4", "Get log summary", "P1",
            "get_log_summary", {"time_range": "last_24_hours"},
            lambda d: (
                isinstance(d, dict) and "total_logs" in d,
                f"Total: {d.get('total_logs', 0):,} logs, {d.get('sources_with_data', 0)} sources" if isinstance(d, dict) else "Error"
            )
        )

        # H5: Get log summary (tenancy scope)
        await self.run_test(
            "H5", "Get log summary (tenancy scope)", "P1",
            "get_log_summary", {"time_range": "last_24_hours", "scope": "tenancy"},
            lambda d: (
                isinstance(d, dict) and "total_logs" in d,
                f"Tenancy total: {d.get('total_logs', 0):,} logs" if isinstance(d, dict) else "Error"
            )
        )

    def print_summary(self):
        """Print test summary."""
        print("\n" + "=" * 60)
        print("TEST SUMMARY")
        print("=" * 60)

        # Group by priority
        priorities = {"P0": [], "P1": [], "P2": [], "P3": []}
        for r in self.results:
            if r.priority in priorities:
                priorities[r.priority].append(r)

        total_passed = 0
        total_failed = 0

        for priority, tests in priorities.items():
            if not tests:
                continue
            passed = sum(1 for t in tests if t.passed)
            failed = len(tests) - passed
            total_passed += passed
            total_failed += failed

            status = "PASS" if failed == 0 else "FAIL"
            print(f"\n{priority}: {passed}/{len(tests)} passed {status}")

            # Show failed tests
            for t in tests:
                if not t.passed:
                    print(f"  FAIL {t.test_id}: {t.name}")
                    if t.error:
                        print(f"    Error: {t.error}")

        print("\n" + "-" * 60)
        print(f"TOTAL: {total_passed}/{total_passed + total_failed} tests passed")

        if total_failed == 0:
            print("\nALL TESTS PASSED!")
        else:
            print(f"\n{total_failed} tests failed")

            # Critical failures
            p0_failures = [t for t in priorities["P0"] if not t.passed]
            if p0_failures:
                print("\nCRITICAL (P0) FAILURES:")
                for t in p0_failures:
                    print(f"  - {t.test_id}: {t.name}")

        print("\n" + "=" * 60)


async def main():
    tester = MCPServerTester()

    if not await tester.setup():
        print("\nFailed to initialize. Check your configuration.")
        sys.exit(1)

    await tester.run_all_tests()
    tester.print_summary()


if __name__ == "__main__":
    asyncio.run(main())
