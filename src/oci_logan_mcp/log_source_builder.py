"""Create Log Analytics parsers and sources from sample log lines."""

import asyncio
import csv
import json
import re
import time
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from io import BytesIO, StringIO
from typing import Any, Dict, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape

import oci


FieldPath = Tuple[str, str]

CONTENT_VERSION = "3.119.2.0.0"
DEFAULT_MAX_FIELDS = 40
FORMAT_JSON_NDJSON = "json_ndjson"
FORMAT_CSV = "csv"
FORMAT_REGEX_TEXT = "regex_text"
SUPPORTED_SAMPLE_FORMATS = frozenset({
    FORMAT_JSON_NDJSON,
    FORMAT_CSV,
    FORMAT_REGEX_TEXT,
})
ALLOWED_VERIFICATION_TIME_RANGES = frozenset({
    "last_15_min",
    "last_1_hour",
    "last_24_hours",
    "last_7_days",
    "last_30_days",
})
DATA_WARNING = (
    "Only provide logs you are allowed to upload to OCI Log Analytics. "
    "Remove secrets, tokens, PII, and customer-sensitive values before continuing."
)

COMMON_FIELD_MAP = {
    "event": "event",
    "eventtype": "event",
    "action": "action",
    "message": "msg",
    "msg": "msg",
    "sourceaddress": "clnthostip",
    "sourceip": "clnthostip",
    "clientip": "clnthostip",
    "clientaddress": "clnthostip",
    "srcip": "clnthostip",
    "sourceport": "port",
    "srcport": "port",
    "port": "port",
    "domain": "domain",
    "domainname": "domain",
    "parentdomain": "domainclnt",
    "querytype": "querytype",
    "questiontype": "querytype",
    "result": "result",
    "status": "status",
    "rcodename": "result",
    "fullrcode": "returncode",
    "returncode": "returncode",
    "recordtype": "rcrdtype",
    "rdata": "ansrecord",
    "latency": "latency",
    "namespace": "namespace",
    "responsesize": "contszout",
}


@dataclass(frozen=True)
class PreparedSample:
    format: str
    field_paths: List[FieldPath]
    sample_content: str
    expected_row_count: int
    truncated_at_max_fields: bool
    header_content: Optional[str] = None
    regex_pattern: Optional[str] = None


def normalize_sample_logs(
    sample_logs: Any,
    *,
    max_lines: int = 1000,
    max_bytes: int = 1_000_000,
) -> List[str]:
    """Normalize a string or list of strings into non-empty log lines."""
    if isinstance(sample_logs, str):
        lines = sample_logs.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    elif isinstance(sample_logs, Iterable):
        lines = []
        for item in sample_logs:
            lines.extend(str(item).replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    else:
        raise ValueError("sample_logs must be a string or array of strings")

    normalized = [str(line).strip() for line in lines if str(line).strip()]
    if not normalized:
        raise ValueError("sample_logs must contain at least one non-empty line")
    if len(normalized) > max_lines:
        raise ValueError(f"sample_logs must contain at most {max_lines} lines")
    sample_bytes = ("\n".join(normalized) + "\n").encode("utf-8")
    if len(sample_bytes) > max_bytes:
        raise ValueError(f"sample_logs must contain at most {max_bytes} bytes")
    return normalized


def infer_json_field_paths(sample_lines: Sequence[str], *, max_fields: int = DEFAULT_MAX_FIELDS) -> List[FieldPath]:
    """Infer JSON leaf fields and JSONPath expressions from sample lines."""
    return _infer_json_field_paths(sample_lines, max_fields=max_fields)[0]


def infer_csv_field_paths(
    sample_logs: Any,
    *,
    max_fields: int = DEFAULT_MAX_FIELDS,
) -> Tuple[List[FieldPath], int, str, str, bool]:
    """Infer CSV header fields and column positions from sample content."""
    prepared = _prepare_csv_sample(sample_logs, max_fields=max_fields)
    return (
        prepared.field_paths,
        prepared.expected_row_count,
        prepared.sample_content,
        prepared.header_content or "",
        prepared.truncated_at_max_fields,
    )


def infer_regex_text_field_paths(
    sample_logs: Any,
    *,
    regex_pattern: str,
    regex_field_keys: Sequence[str],
    max_fields: int = DEFAULT_MAX_FIELDS,
) -> Tuple[List[FieldPath], int, str, str, bool]:
    """Infer regex capture-group fields from explicit group names."""
    prepared = _prepare_regex_text_sample(
        sample_logs,
        regex_pattern=regex_pattern,
        regex_field_keys=regex_field_keys,
        max_fields=max_fields,
    )
    return (
        prepared.field_paths,
        prepared.expected_row_count,
        prepared.sample_content,
        prepared.regex_pattern or "",
        prepared.truncated_at_max_fields,
    )


def _infer_json_field_paths(
    sample_lines: Sequence[str],
    *,
    max_fields: int = DEFAULT_MAX_FIELDS,
) -> Tuple[List[FieldPath], bool]:
    """Infer JSON leaf fields and whether inference exceeded the configured cap."""
    discovered: List[FieldPath] = []
    seen_paths: set = set()
    used_names: Dict[str, str] = {}
    truncated = False

    for line in sample_lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("create_log_source_from_sample currently supports JSON/NDJSON samples only") from exc
        if not isinstance(record, dict):
            raise ValueError("JSON sample lines must be objects")

        for leaf, path in _walk_json(record, "$"):
            if path in seen_paths:
                continue
            seen_paths.add(path)
            if len(discovered) >= max_fields:
                truncated = True
                continue
            field_key = _dedupe_field_key(leaf, path, used_names)
            discovered.append((field_key, path))

    if not discovered:
        raise ValueError("No JSON leaf fields found in sample logs")
    return discovered, truncated


def _prepare_sample(
    sample_logs: Any,
    *,
    format: str,
    regex_pattern: Optional[str] = None,
    regex_field_keys: Optional[Sequence[str]] = None,
    max_fields: int = DEFAULT_MAX_FIELDS,
) -> PreparedSample:
    if format == FORMAT_JSON_NDJSON:
        lines = normalize_sample_logs(sample_logs)
        field_paths, truncated = _infer_json_field_paths(lines, max_fields=max_fields)
        return PreparedSample(
            format=FORMAT_JSON_NDJSON,
            field_paths=field_paths,
            sample_content="\n".join(lines) + "\n",
            expected_row_count=len(lines),
            truncated_at_max_fields=truncated,
        )
    if format == FORMAT_CSV:
        return _prepare_csv_sample(sample_logs, max_fields=max_fields)
    if format == FORMAT_REGEX_TEXT:
        return _prepare_regex_text_sample(
            sample_logs,
            regex_pattern=regex_pattern,
            regex_field_keys=regex_field_keys,
            max_fields=max_fields,
        )
    valid_formats = ", ".join(sorted(SUPPORTED_SAMPLE_FORMATS))
    raise ValueError(f"format must be one of: {valid_formats}")


def _prepare_csv_sample(
    sample_logs: Any,
    *,
    max_fields: int = DEFAULT_MAX_FIELDS,
    max_lines: int = 1000,
    max_bytes: int = 1_000_000,
) -> PreparedSample:
    raw_text = _sample_logs_to_text(sample_logs)
    raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    non_empty_lines = [line for line in raw_text.split("\n") if line.strip()]
    if not non_empty_lines:
        raise ValueError("CSV sample must contain a header row and at least one data row")
    if len(non_empty_lines) > max_lines:
        raise ValueError(f"sample_logs must contain at most {max_lines} lines")

    csv_text = "\n".join(non_empty_lines) + "\n"
    if len(csv_text.encode("utf-8")) > max_bytes:
        raise ValueError(f"sample_logs must contain at most {max_bytes} bytes")

    rows = list(csv.reader(StringIO(csv_text)))
    if len(rows) != len(non_empty_lines):
        raise ValueError("CSV cells with embedded newlines are not supported in v1")
    if len(rows) < 2:
        raise ValueError("CSV sample must include a header row and at least one data row")

    header = [cell.strip() for cell in rows[0]]
    if header:
        header[0] = header[0].lstrip("\ufeff")
    if len(header) < 2:
        raise ValueError("CSV sample must contain at least two columns")
    if any(not cell for cell in header):
        raise ValueError("CSV header cells must be non-empty")

    data_rows = rows[1:]
    for row in data_rows:
        if len(row) != len(header):
            raise ValueError("CSV data rows must have the same number of columns as the header")

    used_names: Dict[str, str] = {}
    field_paths: List[FieldPath] = []
    truncated = False
    for index, header_cell in enumerate(header, start=1):
        if len(field_paths) >= max_fields:
            truncated = True
            continue
        key = _dedupe_csv_field_key(header_cell, str(index), used_names)
        field_paths.append((key, str(index)))

    if not field_paths:
        raise ValueError("No CSV fields found in sample logs")

    mapped_column_count = min(len(header), max_fields)
    prepared_header = header[:mapped_column_count]
    prepared_rows = [row[:mapped_column_count] for row in data_rows]

    return PreparedSample(
        format=FORMAT_CSV,
        field_paths=field_paths,
        sample_content="".join(_csv_line(row) for row in prepared_rows),
        expected_row_count=len(data_rows),
        truncated_at_max_fields=truncated,
        header_content=_csv_line(prepared_header).rstrip("\n"),
    )


def _prepare_regex_text_sample(
    sample_logs: Any,
    *,
    regex_pattern: Optional[str],
    regex_field_keys: Optional[Sequence[str]],
    max_fields: int = DEFAULT_MAX_FIELDS,
) -> PreparedSample:
    if not regex_pattern:
        raise ValueError("regex_pattern is required when format is regex_text")
    if not regex_field_keys or isinstance(regex_field_keys, (str, bytes)):
        raise ValueError("regex_field_keys must be a non-empty array of field keys")

    try:
        compiled = re.compile(regex_pattern)
    except re.error as exc:
        raise ValueError(f"regex_pattern is invalid: {exc}") from exc

    if compiled.groups <= 0:
        raise ValueError("regex_pattern must contain at least one capture group")
    if compiled.groups != len(regex_field_keys):
        raise ValueError(
            "regex_field_keys length must match regex_pattern capture group count"
        )

    lines = normalize_sample_logs(sample_logs)
    for line_number, line in enumerate(lines, start=1):
        if not compiled.search(line):
            raise ValueError(
                f"sample line {line_number} does not match regex_pattern"
            )

    used_names: Dict[str, str] = {}
    field_paths: List[FieldPath] = []
    truncated = False
    for index, field_key in enumerate(regex_field_keys, start=1):
        if len(field_paths) >= max_fields:
            truncated = True
            continue
        key = _dedupe_csv_field_key(str(field_key), str(index), used_names)
        field_paths.append((key, str(index)))

    if not field_paths:
        raise ValueError("No regex capture fields found in sample logs")

    return PreparedSample(
        format=FORMAT_REGEX_TEXT,
        field_paths=field_paths,
        sample_content="\n".join(lines) + "\n",
        expected_row_count=len(lines),
        truncated_at_max_fields=truncated,
        regex_pattern=regex_pattern,
    )


def _sample_logs_to_text(sample_logs: Any) -> str:
    if isinstance(sample_logs, str):
        return sample_logs
    if isinstance(sample_logs, Iterable):
        return "\n".join(str(item) for item in sample_logs)
    raise ValueError("sample_logs must be a string or array of strings")


def _csv_line(row: Sequence[str]) -> str:
    stream = StringIO()
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(row)
    return stream.getvalue()


def _walk_json(value: Any, path: str) -> Iterable[FieldPath]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            yield from _walk_json(child, child_path)
    elif isinstance(value, list):
        if value:
            for item in value:
                yield from _walk_json(item, f"{path}[*]")
    elif value is not None:
        leaf = path.rsplit(".", 1)[-1]
        if "[" in leaf:
            leaf = re.sub(r"\[[^\]]+\]", "", leaf)
        yield leaf, path


def _dedupe_field_key(leaf: str, path: str, used_names: Dict[str, str]) -> str:
    clean = _clean_key(leaf)
    if clean not in used_names:
        used_names[clean] = path
        return clean

    parts = [_clean_key(part) for part in re.split(r"[.\[\]]+", path) if part and part != "$"]
    candidate = "_".join(parts[-3:]) or clean
    base = candidate
    suffix = 2
    while candidate in used_names:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used_names[candidate] = path
    return candidate


def _dedupe_csv_field_key(leaf: str, path: str, used_names: Dict[str, str]) -> str:
    clean = _clean_key(leaf)
    candidate = clean
    suffix = 2
    while candidate in used_names:
        candidate = f"{clean}_{suffix}"
        suffix += 1
    used_names[candidate] = path
    return candidate


def _clean_key(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    return cleaned or "field"


def _common_field_target(key: str) -> Optional[str]:
    lowered = key.lower()
    return COMMON_FIELD_MAP.get(lowered) or COMMON_FIELD_MAP.get(
        re.sub(r"[^a-z0-9]+", "", lowered)
    )


def build_field_mappings(
    field_paths: Sequence[FieldPath],
    available_fields: Sequence[Dict[str, Any]],
    explicit_mappings: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, str], List[str]]:
    """Map inferred JSON keys onto existing Log Analytics fields."""
    explicit_mappings = explicit_mappings or {}
    available = {field["name"] for field in available_fields if field.get("name")}
    fallback_fields = _sorted_udf_fields(available)
    fallback_index = 0
    mappings: Dict[str, str] = {}
    skipped: List[str] = []

    for key, _ in field_paths:
        target = explicit_mappings.get(key)
        if target and target in available:
            mappings[key] = target
            continue

        common_target = _common_field_target(key)
        if common_target and common_target in available:
            mappings[key] = common_target
            continue

        if fallback_index < len(fallback_fields):
            mappings[key] = fallback_fields[fallback_index]
            fallback_index += 1
        else:
            skipped.append(key)

    return mappings, skipped


def _sorted_udf_fields(available: set) -> List[str]:
    family_order = {"udfs": 0, "udff": 1, "udfl": 2, "udfd": 3}

    def sort_key(name: str) -> Tuple[int, int, str]:
        match = re.match(r"^(udfs|udff|udfd|udfl)(\d+)$", name)
        if not match:
            return (10**9, 10**9, name)
        family = match.group(1)
        return (family_order.get(family, 10**9), int(match.group(2)), name)

    return sorted((name for name in available if re.match(r"^udf[sdfl]\d+$", name)), key=sort_key)


def build_custom_content_zip(
    *,
    source_name: str,
    parser_name: str,
    parser_display_name: str,
    field_paths: Sequence[FieldPath],
    field_mappings: Dict[str, str],
    entity_type: str = "omc_host_linux",
) -> bytes:
    """Build a Log Analytics custom-content zip containing parser and source XML."""
    parser_fields = []
    sequence = 1
    for key, json_path in field_paths:
        field_name = field_mappings.get(key)
        if not field_name:
            continue
        parser_fields.append(
            f"""         <ParserField>
            <FieldSeq>{sequence}</FieldSeq>
            <FieldName>{escape(field_name)}</FieldName>
            <StructuredColInfo>{escape(json_path)}</StructuredColInfo>
         </ParserField>"""
        )
        sequence += 1

    if not parser_fields:
        raise ValueError("No fields could be mapped to Log Analytics fields")

    content_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<LoganContent xmlns="http://www.oracle.com/DataCenter/LogAnalyticsStd" content_version="{CONTENT_VERSION}" name="{escape(parser_name)}_content" oms_version="{CONTENT_VERSION}">
   <Parser oms_version="{CONTENT_VERSION}" tokenize_orig_text="1" type="6">
      <Name>{escape(parser_name)}</Name>
      <DisplayName>{escape(parser_display_name)}</DisplayName>
      <Description>Auto-generated parser from sample JSON/NDJSON logs.</Description>
      <IsSingleLineContent>0</IsSingleLineContent>
      <HeaderContent>$:0</HeaderContent>
      <IsSystem>0</IsSystem>
      <Encoding>UTF-8</Encoding>
      <Language>en_US</Language>
      <ParserFields>
{chr(10).join(parser_fields)}
      </ParserFields>
      <WrittenOnce>0</WrittenOnce>
      <IsDefaultParser>0</IsDefaultParser>
   </Parser>
   <Source configWarningSettings="0" name="{escape(source_name)}" oms_version="{CONTENT_VERSION}">
      <SourceType>os_file</SourceType>
      <TargetTypes>
         <TargetType>{escape(entity_type)}</TargetType>
      </TargetTypes>
      <DisplayName>{escape(source_name)}</DisplayName>
      <Description>Auto-generated log source from sample logs.</Description>
      <IsSystem>0</IsSystem>
      <IsSecureContent>1</IsSecureContent>
      <Parsers>
         <Parser>
            <ParserSeq>1</ParserSeq>
            <ParserName>{escape(parser_name)}</ParserName>
         </Parser>
      </Parsers>
   </Source>
</LoganContent>
"""
    readme = (
        "Content\n"
        f"Sources: [{source_name}]\n"
        f"Parsers: [{parser_name}]\n\n"
        "Reference\n"
        f"Fields: [{', '.join(field_mappings.values())}]\n"
    )

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("content.xml", content_xml)
        zf.writestr("README.txt", readme)
    return buffer.getvalue()


def default_parser_name(source_name: str, *, format: str = FORMAT_JSON_NDJSON) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "_", source_name).strip("_")
    if not base:
        base = "custom_log_source"
    if not base[0].isalpha():
        base = f"p_{base}"
    if format == FORMAT_CSV:
        suffix = "CSV"
    elif format == FORMAT_REGEX_TEXT:
        suffix = "REGEX"
    else:
        suffix = "JSON"
    return f"{base}_{suffix}"


def _quote_lql(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _quote_lql_field(field_name: str) -> str:
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", field_name):
        return field_name
    return f"'{_quote_lql(field_name)}'"


def _extract_count(result: Dict[str, Any]) -> int:
    rows = ((result or {}).get("data") or {}).get("rows") or []
    if not rows or not rows[0]:
        return 0
    try:
        return int(rows[0][0] or 0)
    except (TypeError, ValueError):
        return 0


def _extract_upload_reference(result: Dict[str, Any]) -> Optional[str]:
    if not isinstance(result, dict):
        return None
    data = result.get("data")
    if isinstance(data, dict) and data.get("reference") is not None:
        return str(data["reference"])
    if result.get("reference") is not None:
        return str(result["reference"])
    return None


def _upload_file_status(file_info: Dict[str, Any]) -> str:
    return str(file_info.get("status") or "").upper()


def _upload_processing_complete(upload_files: Sequence[Dict[str, Any]]) -> bool:
    terminal = {"FAILED", "SUCCESS", "SUCCESSFUL", "SUCCEEDED"}
    return bool(upload_files) and all(_upload_file_status(f) in terminal for f in upload_files)


def _upload_processing_failed(upload_files: Sequence[Dict[str, Any]]) -> bool:
    return any(_upload_file_status(f) == "FAILED" for f in upload_files)


def _is_retryable_upload_status_error(exc: Exception) -> bool:
    return (
        isinstance(exc, oci.exceptions.ServiceError)
        and getattr(exc, "status", None) in {404, 503}
    )


def _summarize_upload_status_error(exc: Exception) -> Dict[str, Any]:
    if isinstance(exc, oci.exceptions.ServiceError):
        return {
            "type": type(exc).__name__,
            "status": getattr(exc, "status", None),
            "code": getattr(exc, "code", None),
            "message": getattr(exc, "message", str(exc)),
        }
    return {
        "type": type(exc).__name__,
        "message": str(exc),
    }


def _item_name(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("name") or item.get("display_name") or "")
    return str(getattr(item, "name", "") or getattr(item, "display_name", "") or "")


def _contains_name(items: Sequence[Any], name: str) -> bool:
    return any(_item_name(item) == name for item in items)


def _safe_oci_result(
    result: Dict[str, Any],
    *,
    redact_data_keys: Sequence[str] = (),
) -> Dict[str, Any]:
    allowed_headers = {"opc-request-id", "opc-work-request-id"}
    safe = dict(result or {})
    headers = safe.get("headers")
    if isinstance(headers, dict):
        safe["headers"] = {
            key: value
            for key, value in headers.items()
            if key.lower() in allowed_headers
        }
    data = safe.get("data")
    if isinstance(data, dict) and redact_data_keys:
        safe_data = dict(data)
        for key in redact_data_keys:
            if key in safe_data:
                safe_data[key] = "<redacted>"
        safe["data"] = safe_data
    return safe


def _format_mapped_fields_for_result(
    field_paths: Sequence[FieldPath],
    mappings: Dict[str, str],
    *,
    format: str,
) -> List[Dict[str, Any]]:
    mapped_fields = []
    for key, path in field_paths:
        if key not in mappings:
            continue
        item = {"sample_key": key, "logan_field": mappings[key]}
        if format == FORMAT_CSV:
            item["csv_column"] = int(path)
        elif format == FORMAT_REGEX_TEXT:
            item["regex_group"] = int(path)
        else:
            item["json_path"] = path
        mapped_fields.append(item)
    return mapped_fields


def _format_label(format: str) -> str:
    if format == FORMAT_CSV:
        return "CSV"
    if format == FORMAT_REGEX_TEXT:
        return "REGEX_TEXT"
    return "JSON_NDJSON"


class LogSourceFromSampleTool:
    """Create a Log Analytics parser/source, upload samples, and verify parsing."""

    def __init__(self, *, oci_client: Any, query_engine: Any):
        self.oci_client = oci_client
        self.query_engine = query_engine

    async def create_from_sample(
        self,
        *,
        source_name: str,
        sample_logs: Any,
        log_group_id: str,
        parser_name: Optional[str] = None,
        parser_display_name: Optional[str] = None,
        field_mappings: Optional[Dict[str, str]] = None,
        entity_type: str = "omc_host_linux",
        filename: Optional[str] = None,
        upload_name: Optional[str] = None,
        entity_id: Optional[str] = None,
        timezone: Optional[str] = None,
        log_set: Optional[str] = None,
        char_encoding: str = "UTF-8",
        acknowledge_data_review: bool = False,
        overwrite: bool = False,
        format: str = FORMAT_JSON_NDJSON,
        regex_pattern: Optional[str] = None,
        regex_field_keys: Optional[Sequence[str]] = None,
        verification_time_range: str = "last_30_days",
        field_check_limit: int = 20,
        poll_attempts: int = 6,
        poll_interval_seconds: float = 10,
    ) -> Dict[str, Any]:
        if not acknowledge_data_review:
            raise ValueError(
                "acknowledge_data_review must be true before sample logs are uploaded"
            )
        if verification_time_range not in ALLOWED_VERIFICATION_TIME_RANGES:
            valid_ranges = ", ".join(sorted(ALLOWED_VERIFICATION_TIME_RANGES))
            raise ValueError(
                f"verification_time_range must be one of: {valid_ranges}"
            )

        if format not in SUPPORTED_SAMPLE_FORMATS:
            valid_formats = ", ".join(sorted(SUPPORTED_SAMPLE_FORMATS))
            raise ValueError(f"format must be one of: {valid_formats}")
        prepared = _prepare_sample(
            sample_logs,
            format=format,
            regex_pattern=regex_pattern,
            regex_field_keys=regex_field_keys,
        )

        parser_name = parser_name or default_parser_name(source_name, format=format)
        parser_display_name = parser_display_name or source_name
        existing_parsers = await self.oci_client.list_parsers()
        existing_sources = await self.oci_client.list_log_sources()
        conflicts = {
            "parser_exists": _contains_name(existing_parsers, parser_name),
            "source_exists": _contains_name(existing_sources, source_name),
        }
        if not overwrite and any(conflicts.values()):
            return {
                "status": "CONFLICT",
                "data_warning": DATA_WARNING,
                "conflicts": conflicts,
                "created": {
                    "source_name": source_name,
                    "parser_name": parser_name,
                    "parser_display_name": parser_display_name,
                    "entity_type": entity_type,
                },
                "next_steps": [
                    "Choose a different source/parser name, or re-run with overwrite=true.",
                ],
            }

        available_fields = await self.oci_client.list_fields()
        mappings, skipped = build_field_mappings(prepared.field_paths, available_fields, field_mappings)

        if format == FORMAT_CSV:
            parser_result = await self.oci_client.upsert_delimited_parser(
                parser_name=parser_name,
                display_name=parser_display_name,
                field_paths=prepared.field_paths,
                field_mappings=mappings,
                header_content=prepared.header_content or "",
                example_content=prepared.sample_content,
            )
        elif format == FORMAT_REGEX_TEXT:
            parser_result = await self.oci_client.upsert_regex_parser(
                parser_name=parser_name,
                display_name=parser_display_name,
                field_paths=prepared.field_paths,
                field_mappings=mappings,
                regex_pattern=prepared.regex_pattern or "",
                example_content=prepared.sample_content,
            )
        else:
            parser_result = await self.oci_client.upsert_json_parser(
                parser_name=parser_name,
                display_name=parser_display_name,
                field_paths=prepared.field_paths,
                field_mappings=mappings,
                example_content=prepared.sample_content,
            )
        source_result = await self.oci_client.upsert_log_source(
            source_name=source_name,
            parser_name=parser_name,
            display_name=source_name,
            entity_type=entity_type,
        )
        effective_upload_name = upload_name or f"{parser_name}_sample_{time.time_ns()}"
        if filename:
            effective_filename = filename
        elif format == FORMAT_CSV:
            effective_filename = "sample.csv"
        elif format == FORMAT_REGEX_TEXT:
            effective_filename = "sample.log"
        else:
            effective_filename = "sample.ndjson"
        upload_result = await self.oci_client.upload_log_file(
            source_name=source_name,
            filename=effective_filename,
            log_group_id=log_group_id,
            content=prepared.sample_content,
            upload_name=effective_upload_name,
            entity_id=entity_id,
            timezone=timezone,
            log_set=log_set,
            char_encoding=char_encoding,
        )

        attempts = max(1, int(poll_attempts or 1))
        upload_reference = _extract_upload_reference(upload_result)
        upload_files = []
        upload_status_errors = []
        if upload_reference:
            for attempt in range(attempts):
                try:
                    upload_files = await self.oci_client.list_upload_files(upload_reference)
                except Exception as exc:
                    if not _is_retryable_upload_status_error(exc):
                        raise
                    upload_status_errors.append(_summarize_upload_status_error(exc))
                    if attempt == attempts - 1:
                        break
                    if poll_interval_seconds:
                        await asyncio.sleep(poll_interval_seconds)
                    continue
                if _upload_processing_complete(upload_files) or attempt == attempts - 1:
                    break
                if poll_interval_seconds:
                    await asyncio.sleep(poll_interval_seconds)

        upload_filter_field = "Upload Name"
        upload_filter = (
            f"{_quote_lql_field(upload_filter_field)} = '{_quote_lql(effective_upload_name)}'"
        )
        verification_filter = f"* AND {upload_filter}"
        count_query = f"{verification_filter} | stats count"
        parse_failed_query = f"{verification_filter} AND 'Parse Failed' = 1 | stats count"

        ingested_count = 0
        for attempt in range(attempts):
            count_result = await self.query_engine.execute(
                query=count_query,
                time_range=verification_time_range,
                max_results=10,
                use_cache=False,
            )
            ingested_count = _extract_count(count_result)
            if ingested_count > 0 or attempt == attempts - 1:
                break
            if poll_interval_seconds:
                await asyncio.sleep(poll_interval_seconds)

        parse_failed_result = await self.query_engine.execute(
            query=parse_failed_query,
            time_range=verification_time_range,
            max_results=10,
            use_cache=False,
        )
        parse_failed_count = _extract_count(parse_failed_result)

        field_checks = []
        checked_fields = []
        for field_name in mappings.values():
            if field_name not in checked_fields:
                checked_fields.append(field_name)
            if len(checked_fields) >= max(0, field_check_limit):
                break

        if ingested_count > 0:
            for field_name in checked_fields:
                field_query = (
                    f"{verification_filter} | stats count({_quote_lql_field(field_name)})"
                )
                field_result = await self.query_engine.execute(
                    query=field_query,
                    time_range=verification_time_range,
                    max_results=10,
                    use_cache=False,
                )
                field_checks.append({
                    "field": field_name,
                    "populated_count": _extract_count(field_result),
                    "query": field_query,
                })

        if _upload_processing_failed(upload_files):
            status = "FAIL"
        elif parse_failed_count > 0:
            status = "FAIL"
        elif ingested_count <= 0:
            status = "INDETERMINATE"
        elif ingested_count < prepared.expected_row_count:
            status = "PASS_WITH_WARNINGS"
        elif any(check["populated_count"] <= 0 for check in field_checks):
            status = "PASS_WITH_WARNINGS"
        else:
            status = "PASS"

        return {
            "status": status,
            "data_warning": DATA_WARNING,
            "created": {
                "source_name": source_name,
                "parser_name": parser_name,
                "parser_display_name": parser_display_name,
                "entity_type": entity_type,
            },
            "inference": {
                "format": _format_label(format),
                "sample_line_count": prepared.expected_row_count,
                "mapped_field_count": len(mappings),
                "max_inferred_fields": DEFAULT_MAX_FIELDS,
                "truncated_at_max_fields": prepared.truncated_at_max_fields,
                "mapped_fields": _format_mapped_fields_for_result(
                    prepared.field_paths,
                    mappings,
                    format=format,
                ),
                "skipped_fields": skipped,
            },
            "oci": {
                "parser": _safe_oci_result(parser_result, redact_data_keys=("example_content",)),
                "source": _safe_oci_result(source_result),
                "upload": _safe_oci_result(upload_result),
            },
            "verification": {
                "time_range": verification_time_range,
                "upload_name": effective_upload_name,
                "upload_reference": upload_reference,
                "upload_files": upload_files,
                "upload_status_errors": upload_status_errors,
                "upload_filter_field": upload_filter_field,
                "timestamp_configured": False,
                "timestamp_warning": (
                    "No parser timestamp configuration is generated yet; "
                    "Log Analytics may use ingestion/default time behavior."
                ),
                "uploaded_line_count": prepared.expected_row_count,
                "ingested_count": ingested_count,
                "parse_failed_count": parse_failed_count,
                "field_checks": field_checks,
                "queries": {
                    "count": count_query,
                    "parse_failed": parse_failed_query,
                },
            },
        }
