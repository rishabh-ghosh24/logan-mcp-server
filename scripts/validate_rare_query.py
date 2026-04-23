#!/usr/bin/env python3
"""Validate live native `rare` query payloads against the current parser."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Sequence

import oci


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib-logan-mcp")

from oci_logan_mcp.auth import get_signer
from oci_logan_mcp.client import OCILogAnalyticsClient
from oci_logan_mcp.config import load_config


RELATIVE_WINDOWS = {
    "last_15_min": timedelta(minutes=15),
    "last_1_hour": timedelta(hours=1),
    "last_24_hours": timedelta(hours=24),
    "last_2_days": timedelta(days=2),
    "last_7_days": timedelta(days=7),
    "last_14_days": timedelta(days=14),
    "last_30_days": timedelta(days=30),
}


@dataclass(frozen=True)
class ProbeCase:
    label: str
    query: str
    time_range: str
    required_columns: Sequence[str]
    required_non_null_columns: Sequence[str] = ()


PROBE_CASES = (
    ProbeCase(
        label="grouped-stats-log-source",
        query="* | stats count by 'Log Source'",
        time_range="last_24_hours",
        required_columns=("Log Source", "Count"),
        required_non_null_columns=("Log Source", "Count"),
    ),
    ProbeCase(
        label="raw-rare-log-source",
        query="* | rare limit = 5 showcount = true showpercent = true 'Log Source'",
        time_range="last_24_hours",
        required_columns=(
            "Log Source",
            "Rare Count(Log Source)",
            "Rare Percent(Log Source)",
        ),
        required_non_null_columns=(
            "Log Source",
            "Rare Count(Log Source)",
            "Rare Percent(Log Source)",
        ),
    ),
    ProbeCase(
        label="grouped-stats-source-severity",
        query="'Log Source' = 'Linux Syslog Logs' | stats count by Severity",
        time_range="last_24_hours",
        required_columns=("Severity", "Count"),
        required_non_null_columns=("Count",),
    ),
    ProbeCase(
        label="raw-rare-source-severity",
        query=(
            "'Log Source' = 'Linux Syslog Logs' "
            "| rare limit = 5 showcount = true showpercent = true Severity"
        ),
        time_range="last_24_hours",
        required_columns=(
            "Severity",
            "Rare Count(Severity)",
            "Rare Percent(Severity)",
        ),
    ),
)


def _window_bounds(time_range: str) -> tuple[str, str]:
    delta = RELATIVE_WINDOWS.get(time_range)
    if delta is None:
        raise ValueError(f"Unsupported time_range for probe: {time_range}")

    end = datetime.now(timezone.utc)
    start = end - delta
    return start.isoformat(), end.isoformat()


def _make_query_details(
    query: str,
    compartment_id: str,
    time_range: str,
) -> Any:
    time_start, time_end = _window_bounds(time_range)
    return oci.log_analytics.models.QueryDetails(
        compartment_id=compartment_id,
        compartment_id_in_subtree=True,
        query_string=query,
        sub_system=oci.log_analytics.models.QueryDetails.SUB_SYSTEM_LOG,
        time_filter=oci.log_analytics.models.TimeRange(
            time_start=datetime.fromisoformat(time_start),
            time_end=datetime.fromisoformat(time_end),
            time_zone="UTC",
        ),
        max_total_count=1000,
    )


def _collect_local_payloads(cases: Sequence[ProbeCase]) -> Dict[str, Any]:
    settings = load_config()
    oci_config, signer = get_signer(settings.oci)
    client = oci.log_analytics.LogAnalyticsClient(config=oci_config, signer=signer)

    payload = {
        "mode": "local",
        "namespace": settings.log_analytics.namespace,
        "compartment_id": settings.log_analytics.default_compartment_id,
        "cases": [],
    }
    for case in cases:
        response = client.query(
            namespace_name=settings.log_analytics.namespace,
            query_details=_make_query_details(
                query=case.query,
                compartment_id=settings.log_analytics.default_compartment_id,
                time_range=case.time_range,
            ),
            limit=1000,
        )
        payload["cases"].append(
            {
                "label": case.label,
                "query": case.query,
                "time_range": case.time_range,
                "raw": _serialize_response_data(response.data),
            }
        )
    return payload


def _build_remote_collector_program(cases: Sequence[ProbeCase], remote_repo: str) -> str:
    serialized_cases = [
        {
            "label": case.label,
            "query": case.query,
            "time_range": case.time_range,
        }
        for case in cases
    ]
    return textwrap.dedent(
        f"""
        import json
        import sys
        from datetime import datetime, timedelta, timezone

        import oci

        sys.path.insert(0, {str(Path(remote_repo) / 'src')!r})
        from oci_logan_mcp.auth import get_signer
        from oci_logan_mcp.config import load_config

        RELATIVE_WINDOWS = {{
            "last_15_min": timedelta(minutes=15),
            "last_1_hour": timedelta(hours=1),
            "last_24_hours": timedelta(hours=24),
            "last_2_days": timedelta(days=2),
            "last_7_days": timedelta(days=7),
            "last_14_days": timedelta(days=14),
            "last_30_days": timedelta(days=30),
        }}
        CASES = json.loads({json.dumps(json.dumps(serialized_cases))})

        def window_bounds(time_range):
            delta = RELATIVE_WINDOWS[time_range]
            end = datetime.now(timezone.utc)
            start = end - delta
            return start.isoformat(), end.isoformat()

        def serialize_value(value):
            if isinstance(value, (str, int, float, bool)) or value is None:
                return value
            if isinstance(value, dict):
                return {{str(k): serialize_value(v) for k, v in value.items()}}
            if isinstance(value, list):
                return [serialize_value(v) for v in value]
            if hasattr(value, "items") and callable(value.items):
                try:
                    return {{str(k): serialize_value(v) for k, v in value.items()}}
                except Exception:
                    pass
            if hasattr(value, "values"):
                values = value.values() if callable(value.values) else value.values
                if isinstance(values, (list, tuple)):
                    return [serialize_value(v) for v in values]
            return str(value)

        def serialize_data(data):
            return {{
                "columns": [
                    {{
                        "display_name": getattr(column, "display_name", None),
                        "internal_name": getattr(column, "internal_name", None),
                        "value_type": getattr(column, "value_type", None),
                    }}
                    for column in (getattr(data, "columns", None) or [])
                ],
                "items": [serialize_value(item) for item in (getattr(data, "items", None) or [])],
                "total_count": getattr(data, "total_count", None),
                "total_group_count": getattr(data, "total_group_count", None),
                "total_matched_count": getattr(data, "total_matched_count", None),
                "are_partial_results": getattr(data, "are_partial_results", None),
                "is_partial_result": getattr(data, "is_partial_result", None),
            }}

        settings = load_config()
        oci_config, signer = get_signer(settings.oci)
        client = oci.log_analytics.LogAnalyticsClient(config=oci_config, signer=signer)

        out = {{
            "mode": "ssh",
            "namespace": settings.log_analytics.namespace,
            "compartment_id": settings.log_analytics.default_compartment_id,
            "cases": [],
        }}

        for case in CASES:
            start, end = window_bounds(case["time_range"])
            query_details = oci.log_analytics.models.QueryDetails(
                compartment_id=settings.log_analytics.default_compartment_id,
                compartment_id_in_subtree=True,
                query_string=case["query"],
                sub_system=oci.log_analytics.models.QueryDetails.SUB_SYSTEM_LOG,
                time_filter=oci.log_analytics.models.TimeRange(
                    time_start=datetime.fromisoformat(start),
                    time_end=datetime.fromisoformat(end),
                    time_zone="UTC",
                ),
                max_total_count=1000,
            )
            response = client.query(
                namespace_name=settings.log_analytics.namespace,
                query_details=query_details,
                limit=1000,
            )
            out["cases"].append(
                {{
                    "label": case["label"],
                    "query": case["query"],
                    "time_range": case["time_range"],
                    "raw": serialize_data(response.data),
                }}
            )

        print(json.dumps(out))
        """
    ).strip()


def _collect_remote_payloads(
    cases: Sequence[ProbeCase],
    ssh_host: str,
    remote_repo: str,
    remote_python: str,
) -> Dict[str, Any]:
    remote_command = f"cd {shlex.quote(remote_repo)} && {remote_python} -"
    program = _build_remote_collector_program(cases, remote_repo)
    result = subprocess.run(
        ["ssh", ssh_host, remote_command],
        input=program,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"remote probe failed with exit code {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "remote probe did not return JSON\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        ) from exc


def _serialize_response_data(data: Any) -> Dict[str, Any]:
    return {
        "columns": [
            {
                "display_name": getattr(column, "display_name", None),
                "internal_name": getattr(column, "internal_name", None),
                "value_type": getattr(column, "value_type", None),
            }
            for column in (getattr(data, "columns", None) or [])
        ],
        "items": [
            _serialize_item(item)
            for item in (getattr(data, "items", None) or [])
        ],
        "total_count": getattr(data, "total_count", None),
        "total_group_count": getattr(data, "total_group_count", None),
        "total_matched_count": getattr(data, "total_matched_count", None),
        "are_partial_results": getattr(data, "are_partial_results", None),
        "is_partial_result": getattr(data, "is_partial_result", None),
    }


def _serialize_item(item: Any) -> Any:
    if isinstance(item, dict):
        return item
    if hasattr(item, "items") and callable(item.items):
        try:
            return dict(item.items())
        except Exception:
            pass
    if hasattr(item, "values"):
        values = item.values() if callable(item.values) else item.values
        if isinstance(values, (list, tuple)):
            return list(values)
    return str(item)


def _rebuild_data(raw: Dict[str, Any]) -> Any:
    columns = [
        SimpleNamespace(
            display_name=column.get("display_name"),
            internal_name=column.get("internal_name"),
            value_type=column.get("value_type"),
        )
        for column in raw.get("columns", [])
    ]
    return SimpleNamespace(
        columns=columns,
        items=raw.get("items", []),
        total_count=raw.get("total_count"),
        total_group_count=raw.get("total_group_count"),
        total_matched_count=raw.get("total_matched_count"),
        are_partial_results=raw.get("are_partial_results"),
        is_partial_result=raw.get("is_partial_result") or raw.get("are_partial_results"),
    )


def _parse_with_current_branch(raw: Dict[str, Any]) -> Dict[str, Any]:
    data = _rebuild_data(raw)
    parser = object.__new__(OCILogAnalyticsClient)
    return OCILogAnalyticsClient._parse_query_response(parser, data)


def _case_by_label(label: str) -> ProbeCase:
    for case in PROBE_CASES:
        if case.label == label:
            return case
    raise KeyError(label)


def _column_index(columns: Sequence[Dict[str, Any]], name: str) -> int | None:
    for index, column in enumerate(columns):
        if column.get("name") == name:
            return index
    return None


def _has_row_with_non_null_values(
    rows: Sequence[Sequence[Any]],
    columns: Sequence[Dict[str, Any]],
    required_names: Sequence[str],
) -> bool:
    indices = []
    for name in required_names:
        index = _column_index(columns, name)
        if index is None:
            return False
        indices.append(index)

    for row in rows:
        if all(index < len(row) and row[index] is not None for index in indices):
            return True
    return False


def _validate_case(case: ProbeCase, parsed: Dict[str, Any]) -> List[str]:
    issues: List[str] = []
    columns = parsed.get("columns", [])
    rows = parsed.get("rows", [])

    available_columns = {column.get("name") for column in columns}
    missing_columns = [name for name in case.required_columns if name not in available_columns]
    if missing_columns:
        issues.append(f"missing columns: {missing_columns}")

    expected_width = len(columns)
    misaligned = [len(row) for row in rows if len(row) != expected_width]
    if misaligned:
        issues.append(
            f"row width mismatch: expected {expected_width}, saw {misaligned}"
        )

    if case.required_non_null_columns and not _has_row_with_non_null_values(
        rows, columns, case.required_non_null_columns
    ):
        issues.append(
            "no row had non-null values for "
            f"{list(case.required_non_null_columns)}"
        )

    return issues


def _preview_rows(rows: Iterable[Sequence[Any]], limit: int = 3) -> List[List[Any]]:
    preview: List[List[Any]] = []
    for row in rows:
        preview.append(list(row))
        if len(preview) >= limit:
            break
    return preview


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate live native rare-query payloads against the current branch "
            "parser. Use --ssh-host to collect the raw SDK payloads on a remote VM "
            "that has OCI auth."
        )
    )
    parser.add_argument(
        "--ssh-host",
        help="Optional SSH host alias to run the raw SDK collector on remotely.",
    )
    parser.add_argument(
        "--remote-repo",
        default="/home/opc/logan-mcp-server",
        help="Remote repo path used when --ssh-host is set.",
    )
    parser.add_argument(
        "--remote-python",
        default=".venv/bin/python",
        help="Remote Python executable used when --ssh-host is set.",
    )
    args = parser.parse_args()

    if args.ssh_host:
        payload = _collect_remote_payloads(
            PROBE_CASES,
            ssh_host=args.ssh_host,
            remote_repo=args.remote_repo,
            remote_python=args.remote_python,
        )
    else:
        payload = _collect_local_payloads(PROBE_CASES)

    print(
        f"Probe mode: {payload['mode']}\n"
        f"Namespace: {payload['namespace']}\n"
        f"Compartment: {payload['compartment_id']}"
    )

    failures = 0
    for case_payload in payload["cases"]:
        case = _case_by_label(case_payload["label"])
        parsed = _parse_with_current_branch(case_payload["raw"])
        issues = _validate_case(case, parsed)

        status = "PASS" if not issues else "FAIL"
        print(f"\n[{status}] {case.label}")
        print(f"query: {case.query}")
        print(f"columns: {[column.get('name') for column in parsed.get('columns', [])]}")
        print(f"row_preview: {json.dumps(_preview_rows(parsed.get('rows', [])), default=str)}")
        print(
            "raw_counts: "
            f"items={len(case_payload['raw'].get('items', []))}, "
            f"total_count={case_payload['raw'].get('total_count')}, "
            f"total_group_count={case_payload['raw'].get('total_group_count')}"
        )
        if issues:
            failures += 1
            for issue in issues:
                print(f"  - {issue}")

    if failures:
        print(f"\nValidation failed: {failures} case(s) were not usable.")
        return 1

    print("\nValidation passed: live rare-query payloads are usable with the current parser.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
