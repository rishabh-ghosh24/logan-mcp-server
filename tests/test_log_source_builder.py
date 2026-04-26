"""Tests for creating Log Analytics sources from sample logs."""

import csv
import json
import zipfile
from io import BytesIO, StringIO
from unittest.mock import AsyncMock

import oci
import pytest

import oci_logan_mcp.log_source_builder as log_source_builder
from oci_logan_mcp.log_source_builder import (
    LogSourceFromSampleTool,
    build_custom_content_zip,
    build_field_mappings,
    infer_csv_field_paths,
    infer_json_field_paths,
    infer_regex_text_field_paths,
    normalize_sample_logs,
)


def test_normalize_sample_logs_accepts_string_and_arrays():
    assert normalize_sample_logs("one\ntwo\n") == ["one", "two"]
    assert normalize_sample_logs(["one", "two"]) == ["one", "two"]


def test_normalize_sample_logs_rejects_empty_and_oversized_samples():
    with pytest.raises(ValueError, match="at least one"):
        normalize_sample_logs(" \n\t")
    with pytest.raises(ValueError, match="at most 2 lines"):
        normalize_sample_logs(["a", "b", "c"], max_lines=2)
    with pytest.raises(ValueError, match="at most 5 bytes"):
        normalize_sample_logs("abcdef", max_bytes=5)


def test_infer_json_field_paths_flattens_nested_objects_and_arrays():
    lines = [
        json.dumps(
            {
                "time": 1773402155051,
                "eventType": "query-response",
                "sourceAddress": "192.0.2.10",
                "requestData": {
                    "question": [{"domainName": "www.example.com.", "questionType": "A"}]
                },
            }
        )
    ]

    fields = infer_json_field_paths(lines)

    assert ("time", "$.time") in fields
    assert ("eventType", "$.eventType") in fields
    assert ("sourceAddress", "$.sourceAddress") in fields
    assert ("domainName", "$.requestData.question[*].domainName") in fields
    assert ("questionType", "$.requestData.question[*].questionType") in fields


def test_infer_json_field_paths_unions_shapes_across_array_elements_and_lines():
    lines = [
        '{"items":[{"first":"a"},{"second":"b"}],"@timestamp":"2026-04-24T00:00:00Z"}',
        '{"items":[{"third":"c"}]}',
    ]

    fields = infer_json_field_paths(lines)

    assert ("first", "$.items[*].first") in fields
    assert ("second", "$.items[*].second") in fields
    assert ("third", "$.items[*].third") in fields
    assert ("timestamp", "$.@timestamp") in fields


def test_infer_json_field_paths_truncates_at_max_fields():
    line = json.dumps({f"field{i}": i for i in range(41)})

    fields = infer_json_field_paths([line], max_fields=40)

    assert len(fields) == 40
    assert ("field39", "$.field39") in fields
    assert ("field40", "$.field40") not in fields


def test_infer_csv_field_paths_handles_bom_headers_and_quoted_commas():
    fields, row_count, sample_content, header_content, truncated = infer_csv_field_paths(
        '\ufeffSource IP,User-Agent,Bytes In\n192.0.2.10,"Mozilla, Test",42\n'
    )

    assert fields == [("Source_IP", "1"), ("User_Agent", "2"), ("Bytes_In", "3")]
    assert row_count == 1
    assert sample_content == '192.0.2.10,"Mozilla, Test",42\n'
    assert header_content == "Source IP,User-Agent,Bytes In"
    assert truncated is False


def test_infer_csv_field_paths_rejects_unsupported_shapes():
    with pytest.raises(ValueError, match="at least one data row"):
        infer_csv_field_paths("Source IP,User-Agent\n")

    with pytest.raises(ValueError, match="at least two columns"):
        infer_csv_field_paths("Only Column\nvalue\n")

    with pytest.raises(ValueError, match="embedded newlines"):
        infer_csv_field_paths('Source IP,Message\n192.0.2.10,"line one\nline two"\n')


def test_infer_csv_field_paths_truncates_upload_content_at_max_fields():
    header = ",".join(f"field{i}" for i in range(41))
    row = ",".join(str(i) for i in range(41))

    fields, row_count, sample_content, header_content, truncated = infer_csv_field_paths(
        f"{header}\n{row}\n",
        max_fields=40,
    )

    assert len(fields) == 40
    assert ("field39", "40") in fields
    assert ("field40", "41") not in fields
    assert row_count == 1
    assert truncated is True
    assert next(csv.reader(StringIO(header_content))) == [
        f"field{i}" for i in range(40)
    ]
    assert next(csv.reader(StringIO(sample_content))) == [
        str(i) for i in range(40)
    ]


def test_infer_regex_text_field_paths_maps_capture_groups_to_keys():
    fields, row_count, sample_content, pattern, truncated = infer_regex_text_field_paths(
        [
            "INFO user=alice ip=192.0.2.10 action=login",
            "WARN user=bob ip=192.0.2.11 action=logout",
        ],
        regex_pattern=r"^(\w+) user=(\S+) ip=(\S+) action=(\S+)$",
        regex_field_keys=["severity", "user", "sourceip", "action"],
    )

    assert fields == [
        ("severity", "1"),
        ("user", "2"),
        ("sourceip", "3"),
        ("action", "4"),
    ]
    assert row_count == 2
    assert sample_content == (
        "INFO user=alice ip=192.0.2.10 action=login\n"
        "WARN user=bob ip=192.0.2.11 action=logout\n"
    )
    assert pattern == r"^(\w+) user=(\S+) ip=(\S+) action=(\S+)$"
    assert truncated is False


def test_infer_regex_text_field_paths_accepts_oci_java_regex_constructs():
    fields, row_count, sample_content, pattern, truncated = infer_regex_text_field_paths(
        ["2005-Aug-05 12:45:23,123 AM PST Host1 system:Sample log entry"],
        regex_pattern=r"{TIMEDATE}\s+(\p{Upper}\p{Lower}+\d*)\s+(\S+):(.*)",
        regex_field_keys=["host", "service", "message"],
    )

    assert fields == [("host", "1"), ("service", "2"), ("message", "3")]
    assert row_count == 1
    assert sample_content == "2005-Aug-05 12:45:23,123 AM PST Host1 system:Sample log entry\n"
    assert pattern == r"{TIMEDATE}\s+(\p{Upper}\p{Lower}+\d*)\s+(\S+):(.*)"
    assert truncated is False


def test_infer_regex_text_field_paths_ignores_literal_parens_and_char_class_parens():
    with pytest.raises(ValueError, match="at least one capture group"):
        infer_regex_text_field_paths(
            ["literal (paren)"],
            regex_pattern=r"literal \\\(paren\\\)",
            regex_field_keys=["value"],
        )

    with pytest.raises(ValueError, match="at least one capture group"):
        infer_regex_text_field_paths(
            ["a"],
            regex_pattern=r"[abc(def)]",
            regex_field_keys=["value"],
        )


def test_infer_regex_text_field_paths_rejects_invalid_regex_setup():
    with pytest.raises(ValueError, match="regex_pattern is required"):
        infer_regex_text_field_paths(
            ["INFO user=alice"],
            regex_pattern="",
            regex_field_keys=["severity"],
        )

    with pytest.raises(ValueError, match="regex_field_keys"):
        infer_regex_text_field_paths(
            ["INFO user=alice"],
            regex_pattern=r"^(\w+) user=(\S+)$",
            regex_field_keys=[],
        )

    with pytest.raises(ValueError, match="capture group count"):
        infer_regex_text_field_paths(
            ["INFO user=alice"],
            regex_pattern=r"^(\w+) user=(\S+)$",
            regex_field_keys=["severity"],
        )

    with pytest.raises(ValueError, match="unbalanced parentheses"):
        infer_regex_text_field_paths(
            ["INFO user=alice"],
            regex_pattern=r"^(\w+ user=(\S+)$",
            regex_field_keys=["severity", "user"],
        )

    with pytest.raises(ValueError, match="does not match regex_pattern"):
        infer_regex_text_field_paths(
            ["INFO user=alice"],
            regex_pattern=r"^(\w+) action=(\S+)$",
            regex_field_keys=["severity", "action"],
        )


def test_infer_regex_text_field_paths_truncates_at_max_fields():
    pattern = "^" + " ".join(r"(\S+)" for _ in range(41)) + "$"
    sample = " ".join(f"value{i}" for i in range(41))
    keys = [f"field{i}" for i in range(41)]

    fields, row_count, sample_content, regex_pattern, truncated = infer_regex_text_field_paths(
        [sample],
        regex_pattern=pattern,
        regex_field_keys=keys,
        max_fields=40,
    )

    assert len(fields) == 40
    assert ("field39", "40") in fields
    assert ("field40", "41") not in fields
    assert row_count == 1
    assert sample_content == f"{sample}\n"
    assert regex_pattern == pattern
    assert truncated is True


def test_build_field_mappings_prefers_explicit_and_does_not_auto_map_time():
    mappings, skipped = build_field_mappings(
        [("time", "$.time"), ("eventType", "$.eventType"), ("unknown", "$.unknown")],
        [{"name": "time"}, {"name": "event"}, {"name": "udfs1"}, {"name": "udfs2"}],
        explicit_mappings={"unknown": "udfs2"},
    )

    assert mappings["time"] == "udfs1"
    assert mappings["eventType"] == "event"
    assert mappings["unknown"] == "udfs2"
    assert skipped == []


def test_build_field_mappings_normalizes_common_field_lookup():
    mappings, skipped = build_field_mappings(
        [("Source_IP", "1"), ("Bytes_In", "2")],
        [{"name": "clnthostip"}, {"name": "udfs1"}],
    )

    assert mappings == {"Source_IP": "clnthostip", "Bytes_In": "udfs1"}
    assert skipped == []


def test_build_field_mappings_uses_safe_udf_fallback_order():
    mappings, skipped = build_field_mappings(
        [("first", "$.first"), ("second", "$.second"), ("third", "$.third")],
        [{"name": "udfd1"}, {"name": "udff1"}, {"name": "udfs2"}, {"name": "udfs1"}],
    )

    assert mappings == {
        "first": "udfs1",
        "second": "udfs2",
        "third": "udff1",
    }
    assert skipped == []


def test_build_custom_content_zip_contains_parser_and_source_xml():
    payload = build_custom_content_zip(
        source_name="BlueCat Edge DNS Logs",
        parser_name="BlueCat_Edge_DNS_JSON",
        parser_display_name="BlueCat Edge DNS JSON",
        field_paths=[("eventType", "$.eventType")],
        field_mappings={"eventType": "event"},
        entity_type="omc_host_linux",
    )

    with zipfile.ZipFile(BytesIO(payload)) as zf:
        content_xml = zf.read("content.xml").decode("utf-8")

    assert "<Name>BlueCat_Edge_DNS_JSON</Name>" in content_xml
    assert "<DisplayName>BlueCat Edge DNS JSON</DisplayName>" in content_xml
    assert '<Source configWarningSettings="0" name="BlueCat Edge DNS Logs"' in content_xml
    assert "<ParserName>BlueCat_Edge_DNS_JSON</ParserName>" in content_xml
    assert "<FieldName>event</FieldName>" in content_xml
    assert "<StructuredColInfo>$.eventType</StructuredColInfo>" in content_xml
    assert "<IsDefaultParser>0</IsDefaultParser>" in content_xml


@pytest.mark.asyncio
async def test_create_from_sample_upserts_uploads_and_checks_parse_failure():
    oci_client = AsyncMock()
    oci_client.list_parsers.return_value = []
    oci_client.list_log_sources.return_value = []
    oci_client.list_fields.return_value = [
        {"name": "time"},
        {"name": "event"},
        {"name": "clnthostip"},
        {"name": "udfs1"},
    ]
    oci_client.upsert_json_parser.return_value = {
        "data": {
            "name": "BlueCat_Edge_DNS_JSON",
            "example_content": (
                '{"time":1773402155051,"eventType":"query-response","sourceAddress":"192.0.2.10"}\n'
                '{"time":1773402155052,"eventType":"query-response","sourceAddress":"192.0.2.11"}\n'
            ),
        },
    }
    oci_client.upsert_log_source.return_value = {"data": {"name": "BlueCat Edge DNS Logs"}}
    oci_client.upload_log_file.return_value = {"upload_name": "logan-sample"}

    query_engine = AsyncMock()
    query_engine.execute.side_effect = [
        {"data": {"rows": [[2]]}},
        {"data": {"rows": [[0]]}},
        {"data": {"rows": [[2]]}},
        {"data": {"rows": [[2]]}},
        {"data": {"rows": [[2]]}},
    ]

    tool = LogSourceFromSampleTool(oci_client=oci_client, query_engine=query_engine)
    result = await tool.create_from_sample(
        source_name="BlueCat Edge DNS Logs",
        sample_logs=[
            '{"time":1773402155051,"eventType":"query-response","sourceAddress":"192.0.2.10"}',
            '{"time":1773402155052,"eventType":"query-response","sourceAddress":"192.0.2.11"}',
        ],
        log_group_id="ocid1.loganalyticsloggroup.oc1..test",
        parser_name="BlueCat_Edge_DNS_JSON",
        upload_name="logan-sample",
        acknowledge_data_review=True,
        poll_attempts=1,
        poll_interval_seconds=0,
    )

    assert result["status"] == "PASS"
    assert result["created"]["parser_name"] == "BlueCat_Edge_DNS_JSON"
    assert result["verification"]["ingested_count"] == 2
    assert result["verification"]["parse_failed_count"] == 0
    assert result["verification"]["timestamp_configured"] is False
    assert result["verification"]["upload_name"] == "logan-sample"
    assert result["verification"]["upload_filter_field"] == "Upload Name"
    assert result["inference"]["truncated_at_max_fields"] is False
    assert result["oci"]["parser"]["data"]["example_content"] == "<redacted>"
    assert "Only provide logs" in result["data_warning"]
    oci_client.upsert_json_parser.assert_awaited_once()
    parser_kwargs = oci_client.upsert_json_parser.await_args.kwargs
    assert parser_kwargs["parser_name"] == "BlueCat_Edge_DNS_JSON"
    assert parser_kwargs["example_content"].endswith("\n")
    assert len(parser_kwargs["example_content"].splitlines()) == 2
    oci_client.upsert_log_source.assert_awaited_once_with(
        source_name="BlueCat Edge DNS Logs",
        parser_name="BlueCat_Edge_DNS_JSON",
        display_name="BlueCat Edge DNS Logs",
        entity_type="omc_host_linux",
    )
    oci_client.upload_log_file.assert_awaited_once()
    assert oci_client.upload_log_file.await_args.kwargs["upload_name"] == "logan-sample"
    queries = [call.kwargs["query"] for call in query_engine.execute.await_args_list]
    assert (
        "* AND 'Upload Name' = 'logan-sample' | stats count"
        in queries
    )
    assert (
        "* AND 'Upload Name' = 'logan-sample' AND 'Parse Failed' = 1 | stats count"
        in queries
    )
    assert all(call.kwargs["time_range"] == "last_30_days" for call in query_engine.execute.await_args_list)


@pytest.mark.asyncio
async def test_create_from_csv_sample_upserts_uploads_and_checks_parse_failure():
    oci_client = AsyncMock()
    oci_client.list_parsers.return_value = []
    oci_client.list_log_sources.return_value = []
    oci_client.list_fields.return_value = [
        {"name": "clnthostip"},
        {"name": "udfs1"},
    ]
    oci_client.upsert_delimited_parser.return_value = {
        "data": {
            "name": "CSV_App_Parser",
            "example_content": '192.0.2.10,"Mozilla, Test"\n192.0.2.11,Chrome\n',
        },
    }
    oci_client.upsert_log_source.return_value = {"data": {"name": "CSV App Logs"}}
    oci_client.upload_log_file.return_value = {"upload_name": "csv-sample"}

    query_engine = AsyncMock()
    query_engine.execute.side_effect = [
        {"data": {"rows": [[2]]}},
        {"data": {"rows": [[0]]}},
        {"data": {"rows": [[2]]}},
        {"data": {"rows": [[2]]}},
    ]

    tool = LogSourceFromSampleTool(oci_client=oci_client, query_engine=query_engine)
    result = await tool.create_from_sample(
        source_name="CSV App Logs",
        sample_logs='Source IP,User-Agent\n192.0.2.10,"Mozilla, Test"\n192.0.2.11,Chrome\n',
        log_group_id="ocid1.loganalyticsloggroup.oc1..test",
        parser_name="CSV_App_Parser",
        upload_name="csv-sample",
        format="csv",
        acknowledge_data_review=True,
        poll_attempts=1,
        poll_interval_seconds=0,
    )

    assert result["status"] == "PASS"
    assert result["inference"]["format"] == "CSV"
    assert result["inference"]["sample_line_count"] == 2
    assert result["verification"]["uploaded_line_count"] == 2
    assert result["oci"]["parser"]["data"]["example_content"] == "<redacted>"
    assert result["inference"]["mapped_fields"] == [
        {"sample_key": "Source_IP", "csv_column": 1, "logan_field": "clnthostip"},
        {"sample_key": "User_Agent", "csv_column": 2, "logan_field": "udfs1"},
    ]
    oci_client.upsert_json_parser.assert_not_awaited()
    oci_client.upsert_delimited_parser.assert_awaited_once()
    parser_kwargs = oci_client.upsert_delimited_parser.await_args.kwargs
    assert parser_kwargs["parser_name"] == "CSV_App_Parser"
    assert parser_kwargs["header_content"] == "Source IP,User-Agent"
    assert parser_kwargs["example_content"] == '192.0.2.10,"Mozilla, Test"\n192.0.2.11,Chrome\n'
    assert parser_kwargs["field_mappings"] == {
        "Source_IP": "clnthostip",
        "User_Agent": "udfs1",
    }
    oci_client.upload_log_file.assert_awaited_once()
    assert oci_client.upload_log_file.await_args.kwargs["filename"] == "sample.csv"
    assert (
        oci_client.upload_log_file.await_args.kwargs["content"]
        == '192.0.2.10,"Mozilla, Test"\n192.0.2.11,Chrome\n'
    )


@pytest.mark.asyncio
async def test_create_from_regex_text_sample_upserts_uploads_and_checks_fields():
    oci_client = AsyncMock()
    oci_client.list_parsers.return_value = []
    oci_client.list_log_sources.return_value = []
    oci_client.list_fields.return_value = [
        {"name": "clnthostip"},
        {"name": "action"},
        {"name": "udfs1"},
        {"name": "udfs2"},
    ]
    oci_client.upsert_regex_parser.return_value = {
        "data": {
            "name": "Text_App_REGEX",
            "example_content": (
                "INFO user=alice ip=192.0.2.10 action=login\n"
                "WARN user=bob ip=192.0.2.11 action=logout\n"
            ),
        },
    }
    oci_client.upsert_log_source.return_value = {"data": {"name": "Text App Logs"}}
    oci_client.upload_log_file.return_value = {"upload_name": "regex-sample"}

    query_engine = AsyncMock()
    query_engine.execute.side_effect = [
        {"data": {"rows": [[2]]}},
        {"data": {"rows": [[0]]}},
        {"data": {"rows": [[2]]}},
        {"data": {"rows": [[2]]}},
        {"data": {"rows": [[2]]}},
        {"data": {"rows": [[2]]}},
    ]

    tool = LogSourceFromSampleTool(oci_client=oci_client, query_engine=query_engine)
    result = await tool.create_from_sample(
        source_name="Text App Logs",
        sample_logs=[
            "INFO user=alice ip=192.0.2.10 action=login",
            "WARN user=bob ip=192.0.2.11 action=logout",
        ],
        log_group_id="ocid1.loganalyticsloggroup.oc1..test",
        parser_name="Text_App_REGEX",
        upload_name="regex-sample",
        format="regex_text",
        regex_pattern=r"^(\w+) user=(\S+) ip=(\S+) action=(\S+)$",
        regex_field_keys=["severity", "user", "sourceip", "action"],
        acknowledge_data_review=True,
        field_check_limit=4,
        poll_attempts=1,
        poll_interval_seconds=0,
    )

    assert result["status"] == "PASS"
    assert result["inference"]["format"] == "REGEX_TEXT"
    assert result["inference"]["sample_line_count"] == 2
    assert result["inference"]["mapped_fields"] == [
        {"sample_key": "severity", "regex_group": 1, "logan_field": "udfs1"},
        {"sample_key": "user", "regex_group": 2, "logan_field": "udfs2"},
        {"sample_key": "sourceip", "regex_group": 3, "logan_field": "clnthostip"},
        {"sample_key": "action", "regex_group": 4, "logan_field": "action"},
    ]
    assert result["oci"]["parser"]["data"]["example_content"] == "<redacted>"
    oci_client.upsert_json_parser.assert_not_awaited()
    oci_client.upsert_delimited_parser.assert_not_awaited()
    oci_client.upsert_regex_parser.assert_awaited_once()
    parser_kwargs = oci_client.upsert_regex_parser.await_args.kwargs
    assert parser_kwargs["parser_name"] == "Text_App_REGEX"
    assert parser_kwargs["regex_pattern"] == r"^(\w+) user=(\S+) ip=(\S+) action=(\S+)$"
    assert parser_kwargs["example_content"].endswith("\n")
    assert parser_kwargs["field_mappings"] == {
        "severity": "udfs1",
        "user": "udfs2",
        "sourceip": "clnthostip",
        "action": "action",
    }
    oci_client.upload_log_file.assert_awaited_once()
    assert oci_client.upload_log_file.await_args.kwargs["filename"] == "sample.log"
    assert (
        oci_client.upload_log_file.await_args.kwargs["content"]
        == "INFO user=alice ip=192.0.2.10 action=login\n"
        "WARN user=bob ip=192.0.2.11 action=logout\n"
    )


@pytest.mark.asyncio
async def test_create_from_sample_fails_when_parse_failures_are_seen():
    oci_client = AsyncMock()
    oci_client.list_parsers.return_value = []
    oci_client.list_log_sources.return_value = []
    oci_client.list_fields.return_value = [{"name": "event"}, {"name": "udfs1"}]
    oci_client.upsert_json_parser.return_value = {}
    oci_client.upsert_log_source.return_value = {}
    oci_client.upload_log_file.return_value = {}

    query_engine = AsyncMock()
    query_engine.execute.side_effect = [
        {"data": {"rows": [[3]]}},
        {"data": {"rows": [[1]]}},
        {"data": {"rows": [[3]]}},
    ]

    tool = LogSourceFromSampleTool(oci_client=oci_client, query_engine=query_engine)
    result = await tool.create_from_sample(
        source_name="App Logs",
        sample_logs=['{"event":"x"}'],
        log_group_id="ocid1.loganalyticsloggroup.oc1..test",
        acknowledge_data_review=True,
        poll_attempts=1,
        poll_interval_seconds=0,
    )

    assert result["status"] == "FAIL"
    assert result["verification"]["parse_failed_count"] == 1


@pytest.mark.asyncio
async def test_create_from_sample_fails_when_upload_processing_fails():
    oci_client = AsyncMock()
    oci_client.list_parsers.return_value = []
    oci_client.list_log_sources.return_value = []
    oci_client.list_fields.return_value = [{"name": "event"}, {"name": "udfs1"}]
    oci_client.upsert_json_parser.return_value = {}
    oci_client.upsert_log_source.return_value = {}
    oci_client.upload_log_file.return_value = {"data": {"reference": "upload-ref"}}
    oci_client.list_upload_files.return_value = [
        {
            "name": "sample.ndjson",
            "status": "FAILED",
            "failure_details": "Unexpected error encountered",
        }
    ]

    query_engine = AsyncMock()
    query_engine.execute.side_effect = [
        {"data": {"rows": [[0]]}},
        {"data": {"rows": [[0]]}},
    ]

    tool = LogSourceFromSampleTool(oci_client=oci_client, query_engine=query_engine)
    result = await tool.create_from_sample(
        source_name="App Logs",
        sample_logs=['{"event":"x"}'],
        log_group_id="ocid1.loganalyticsloggroup.oc1..test",
        upload_name="sample-upload",
        acknowledge_data_review=True,
        poll_attempts=1,
        poll_interval_seconds=0,
    )

    assert result["status"] == "FAIL"
    assert result["verification"]["upload_reference"] == "upload-ref"
    assert result["verification"]["upload_files"][0]["status"] == "FAILED"


@pytest.mark.asyncio
async def test_create_from_sample_retries_transient_upload_status_errors():
    oci_client = AsyncMock()
    oci_client.list_parsers.return_value = []
    oci_client.list_log_sources.return_value = []
    oci_client.list_fields.return_value = [{"name": "event"}, {"name": "udfs1"}]
    oci_client.upsert_json_parser.return_value = {}
    oci_client.upsert_log_source.return_value = {}
    oci_client.upload_log_file.return_value = {"data": {"reference": "upload-ref"}}
    oci_client.list_upload_files.side_effect = [
        oci.exceptions.ServiceError(
            status=404,
            code="NotAuthorizedOrNotFound",
            headers={"opc-request-id": "req-1"},
            message="upload not visible yet",
        ),
        [{"name": "sample.ndjson", "status": "SUCCESSFUL"}],
    ]

    query_engine = AsyncMock()
    query_engine.execute.side_effect = [
        {"data": {"rows": [[1]]}},
        {"data": {"rows": [[0]]}},
        {"data": {"rows": [[1]]}},
    ]

    tool = LogSourceFromSampleTool(oci_client=oci_client, query_engine=query_engine)
    result = await tool.create_from_sample(
        source_name="App Logs",
        sample_logs=['{"event":"x"}'],
        log_group_id="ocid1.loganalyticsloggroup.oc1..test",
        upload_name="sample-upload",
        acknowledge_data_review=True,
        poll_attempts=2,
        poll_interval_seconds=0,
    )

    assert result["status"] == "PASS"
    assert result["verification"]["upload_status_errors"] == [
        {
            "type": "ServiceError",
            "status": 404,
            "code": "NotAuthorizedOrNotFound",
            "message": "upload not visible yet",
        }
    ]
    assert result["verification"]["upload_files"][0]["status"] == "SUCCESSFUL"


@pytest.mark.asyncio
async def test_create_from_sample_does_not_retry_non_transient_upload_status_errors():
    oci_client = AsyncMock()
    oci_client.list_parsers.return_value = []
    oci_client.list_log_sources.return_value = []
    oci_client.list_fields.return_value = [{"name": "event"}, {"name": "udfs1"}]
    oci_client.upsert_json_parser.return_value = {}
    oci_client.upsert_log_source.return_value = {}
    oci_client.upload_log_file.return_value = {"data": {"reference": "upload-ref"}}
    oci_client.list_upload_files.side_effect = oci.exceptions.ServiceError(
        status=403,
        code="Forbidden",
        headers={"opc-request-id": "req-1"},
        message="not allowed",
    )

    tool = LogSourceFromSampleTool(oci_client=oci_client, query_engine=AsyncMock())

    with pytest.raises(oci.exceptions.ServiceError):
        await tool.create_from_sample(
            source_name="App Logs",
            sample_logs=['{"event":"x"}'],
            log_group_id="ocid1.loganalyticsloggroup.oc1..test",
            upload_name="sample-upload",
            acknowledge_data_review=True,
            poll_attempts=2,
            poll_interval_seconds=0,
        )

    assert oci_client.list_upload_files.await_count == 1


@pytest.mark.asyncio
async def test_create_from_sample_auto_upload_name_uses_nanosecond_suffix(monkeypatch):
    monkeypatch.setattr(log_source_builder.time, "time_ns", lambda: 123456789)

    oci_client = AsyncMock()
    oci_client.list_parsers.return_value = []
    oci_client.list_log_sources.return_value = []
    oci_client.list_fields.return_value = [{"name": "event"}]
    oci_client.upsert_json_parser.return_value = {}
    oci_client.upsert_log_source.return_value = {}
    oci_client.upload_log_file.return_value = {}

    query_engine = AsyncMock()
    query_engine.execute.side_effect = [
        {"data": {"rows": [[1]]}},
        {"data": {"rows": [[0]]}},
        {"data": {"rows": [[1]]}},
    ]

    tool = LogSourceFromSampleTool(oci_client=oci_client, query_engine=query_engine)
    result = await tool.create_from_sample(
        source_name="App Logs",
        sample_logs=['{"event":"x"}'],
        log_group_id="ocid1.loganalyticsloggroup.oc1..test",
        parser_name="App_JSON",
        acknowledge_data_review=True,
        poll_attempts=1,
        poll_interval_seconds=0,
    )

    assert result["verification"]["upload_name"] == "App_JSON_sample_123456789"
    assert oci_client.upload_log_file.await_args.kwargs["upload_name"] == "App_JSON_sample_123456789"


@pytest.mark.asyncio
async def test_create_from_sample_requires_data_review_acknowledgement():
    tool = LogSourceFromSampleTool(oci_client=AsyncMock(), query_engine=AsyncMock())

    with pytest.raises(ValueError, match="acknowledge_data_review"):
        await tool.create_from_sample(
            source_name="App Logs",
            sample_logs=['{"event":"x"}'],
            log_group_id="ocid1.loganalyticsloggroup.oc1..test",
        )


@pytest.mark.asyncio
async def test_create_from_sample_refuses_name_collision_without_overwrite():
    oci_client = AsyncMock()
    oci_client.list_parsers.return_value = [{"name": "App_Logs_JSON"}]
    oci_client.list_log_sources.return_value = [{"name": "App Logs"}]
    tool = LogSourceFromSampleTool(oci_client=oci_client, query_engine=AsyncMock())

    result = await tool.create_from_sample(
        source_name="App Logs",
        sample_logs=['{"event":"x"}'],
        log_group_id="ocid1.loganalyticsloggroup.oc1..test",
        acknowledge_data_review=True,
    )

    assert result["status"] == "CONFLICT"
    assert result["conflicts"] == {
        "parser_exists": True,
        "source_exists": True,
    }
    oci_client.upsert_json_parser.assert_not_awaited()
    oci_client.upsert_log_source.assert_not_awaited()
    oci_client.upload_log_file.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parsers", "sources", "expected"),
    [
        ([{"name": "App_Logs_JSON"}], [], {"parser_exists": True, "source_exists": False}),
        ([], [{"name": "App Logs"}], {"parser_exists": False, "source_exists": True}),
    ],
)
async def test_create_from_sample_reports_single_name_collisions(parsers, sources, expected):
    oci_client = AsyncMock()
    oci_client.list_parsers.return_value = parsers
    oci_client.list_log_sources.return_value = sources
    tool = LogSourceFromSampleTool(oci_client=oci_client, query_engine=AsyncMock())

    result = await tool.create_from_sample(
        source_name="App Logs",
        sample_logs=['{"event":"x"}'],
        log_group_id="ocid1.loganalyticsloggroup.oc1..test",
        acknowledge_data_review=True,
    )

    assert result["status"] == "CONFLICT"
    assert result["conflicts"] == expected
    oci_client.upsert_json_parser.assert_not_awaited()
    oci_client.upsert_log_source.assert_not_awaited()
    oci_client.upload_log_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_from_sample_returns_indeterminate_when_upload_not_queryable_yet():
    oci_client = AsyncMock()
    oci_client.list_parsers.return_value = []
    oci_client.list_log_sources.return_value = []
    oci_client.list_fields.return_value = [{"name": "event"}, {"name": "udfs1"}]
    oci_client.upsert_json_parser.return_value = {}
    oci_client.upsert_log_source.return_value = {}
    oci_client.upload_log_file.return_value = {}

    query_engine = AsyncMock()
    query_engine.execute.side_effect = [
        {"data": {"rows": [[0]]}},
        {"data": {"rows": [[0]]}},
    ]

    tool = LogSourceFromSampleTool(oci_client=oci_client, query_engine=query_engine)
    result = await tool.create_from_sample(
        source_name="App Logs",
        sample_logs=['{"event":"x"}'],
        log_group_id="ocid1.loganalyticsloggroup.oc1..test",
        acknowledge_data_review=True,
        poll_attempts=1,
        poll_interval_seconds=0,
    )

    assert result["status"] == "INDETERMINATE"
    assert result["verification"]["ingested_count"] == 0


@pytest.mark.asyncio
async def test_create_from_sample_warns_when_only_some_uploaded_lines_are_queryable():
    oci_client = AsyncMock()
    oci_client.list_parsers.return_value = []
    oci_client.list_log_sources.return_value = []
    oci_client.list_fields.return_value = [{"name": "event"}]
    oci_client.upsert_json_parser.return_value = {}
    oci_client.upsert_log_source.return_value = {}
    oci_client.upload_log_file.return_value = {}

    query_engine = AsyncMock()
    query_engine.execute.side_effect = [
        {"data": {"rows": [[1]]}},
        {"data": {"rows": [[0]]}},
        {"data": {"rows": [[1]]}},
    ]

    tool = LogSourceFromSampleTool(oci_client=oci_client, query_engine=query_engine)
    result = await tool.create_from_sample(
        source_name="App Logs",
        sample_logs=['{"event":"x"}', '{"event":"y"}'],
        log_group_id="ocid1.loganalyticsloggroup.oc1..test",
        acknowledge_data_review=True,
        poll_attempts=1,
        poll_interval_seconds=0,
    )

    assert result["status"] == "PASS_WITH_WARNINGS"


@pytest.mark.asyncio
async def test_create_from_sample_warns_when_some_fields_are_empty():
    oci_client = AsyncMock()
    oci_client.list_parsers.return_value = []
    oci_client.list_log_sources.return_value = []
    oci_client.list_fields.return_value = [{"name": "event"}, {"name": "udfs1"}]
    oci_client.upsert_json_parser.return_value = {}
    oci_client.upsert_log_source.return_value = {}
    oci_client.upload_log_file.return_value = {}

    query_engine = AsyncMock()
    query_engine.execute.side_effect = [
        {"data": {"rows": [[2]]}},
        {"data": {"rows": [[0]]}},
        {"data": {"rows": [[2]]}},
        {"data": {"rows": [[0]]}},
    ]

    tool = LogSourceFromSampleTool(oci_client=oci_client, query_engine=query_engine)
    result = await tool.create_from_sample(
        source_name="App Logs",
        sample_logs=['{"event":"x","other":"y"}', '{"event":"z","other":"q"}'],
        log_group_id="ocid1.loganalyticsloggroup.oc1..test",
        acknowledge_data_review=True,
        poll_attempts=1,
        poll_interval_seconds=0,
    )

    assert result["status"] == "PASS_WITH_WARNINGS"
    assert len(result["verification"]["field_checks"]) == 2


@pytest.mark.asyncio
async def test_create_from_sample_quotes_mapped_field_names_with_spaces():
    oci_client = AsyncMock()
    oci_client.list_parsers.return_value = []
    oci_client.list_log_sources.return_value = []
    oci_client.list_fields.return_value = [{"name": "Original Log Content"}]
    oci_client.upsert_json_parser.return_value = {}
    oci_client.upsert_log_source.return_value = {}
    oci_client.upload_log_file.return_value = {}

    query_engine = AsyncMock()
    query_engine.execute.side_effect = [
        {"data": {"rows": [[1]]}},
        {"data": {"rows": [[0]]}},
        {"data": {"rows": [[1]]}},
    ]

    tool = LogSourceFromSampleTool(oci_client=oci_client, query_engine=query_engine)
    await tool.create_from_sample(
        source_name="App Logs",
        sample_logs=['{"message":"x"}'],
        log_group_id="ocid1.loganalyticsloggroup.oc1..test",
        upload_name="field-check-upload",
        field_mappings={"message": "Original Log Content"},
        acknowledge_data_review=True,
        poll_attempts=1,
        poll_interval_seconds=0,
    )

    queries = [call.kwargs["query"] for call in query_engine.execute.await_args_list]
    assert (
        "* AND 'Upload Name' = 'field-check-upload' | stats count('Original Log Content')"
        in queries
    )


@pytest.mark.asyncio
async def test_create_from_sample_reports_inference_truncation_in_result():
    oci_client = AsyncMock()
    oci_client.list_parsers.return_value = []
    oci_client.list_log_sources.return_value = []
    oci_client.list_fields.return_value = [{"name": f"udfs{i}"} for i in range(1, 41)]
    oci_client.upsert_json_parser.return_value = {}
    oci_client.upsert_log_source.return_value = {}
    oci_client.upload_log_file.return_value = {}

    query_engine = AsyncMock()
    query_engine.execute.side_effect = [
        {"data": {"rows": [[1]]}},
        {"data": {"rows": [[0]]}},
        *({"data": {"rows": [[1]]}} for _ in range(20)),
    ]

    tool = LogSourceFromSampleTool(oci_client=oci_client, query_engine=query_engine)
    result = await tool.create_from_sample(
        source_name="Wide Logs",
        sample_logs=[json.dumps({f"field{i}": i for i in range(41)})],
        log_group_id="ocid1.loganalyticsloggroup.oc1..test",
        acknowledge_data_review=True,
        poll_attempts=1,
        poll_interval_seconds=0,
    )

    assert result["inference"]["truncated_at_max_fields"] is True
    assert result["inference"]["max_inferred_fields"] == 40
