from unittest.mock import AsyncMock

import pytest

from oci_logan_mcp.rare_events import RareEventsTool


@pytest.fixture
def engine():
    class Engine:
        execute = AsyncMock()

    return Engine()


@pytest.fixture
def tool(engine):
    return RareEventsTool(engine)


def _response(columns, rows):
    return {
        "data": {
            "columns": [{"name": name} for name in columns],
            "rows": rows,
        }
    }


@pytest.mark.asyncio
async def test_merges_native_rare_with_history_annotation(tool, engine):
    engine.execute.side_effect = [
        _response(
            ["Severity", "Rare Count(Severity)", "Rare Percent(Severity)"],
            [
                ["ERROR", 3, 0.5],
                ["INFO", 200, 10.0],
                [None, None, None],
            ],
        ),
        _response(
            ["Severity", "count_in_history", "first_seen", "last_seen"],
            [
                ["ERROR", 9, "2026-03-25T10:00:00+00:00", "2026-04-23T00:15:00+00:00"],
                ["INFO", 1200, "2026-03-01T00:00:00+00:00", "2026-04-23T00:10:00+00:00"],
            ],
        ),
    ]

    result = await tool.run(
        source="Linux Syslog Logs",
        field="Severity",
        time_range={"time_range": "last_24_hours"},
    )

    assert result["source"] == "Linux Syslog Logs"
    assert result["field"] == "Severity"
    assert result["history_days"] == 30
    assert result["rarity_threshold_percentile"] == 5.0
    assert result["rare_values"] == [
        {
            "value": "ERROR",
            "count_in_range": 3,
            "percent_in_range": 0.5,
            "count_in_history": 9,
            "first_seen": "2026-03-25T10:00:00+00:00",
            "last_seen": "2026-04-23T00:15:00+00:00",
        }
    ]

    current_call = engine.execute.await_args_list[0]
    assert current_call.kwargs == {
        "query": "'Log Source' = 'Linux Syslog Logs' | rare limit = -1 showcount = true showpercent = true Severity",
        "time_range": "last_24_hours",
    }

    history_call = engine.execute.await_args_list[1]
    assert history_call.kwargs == {
        "query": (
            "'Log Source' = 'Linux Syslog Logs' "
            "| stats count as count_in_history, earliest(Time) as first_seen, latest(Time) as last_seen by Severity"
        ),
        "time_range": "last_30_days",
    }


@pytest.mark.asyncio
async def test_quotes_multiword_fields_and_returns_empty_when_no_usable_rare_rows(tool, engine):
    engine.execute.side_effect = [
        _response(
            ["Log Source", "Rare Count(Log Source)", "Rare Percent(Log Source)"],
            [[None, None, None]],
        ),
        _response(
            ["Log Source", "count_in_history", "first_seen", "last_seen"],
            [],
        ),
    ]

    result = await tool.run(
        source="Linux Syslog Logs",
        field="Log Source",
        time_range={"time_range": "last_24_hours"},
        rarity_threshold_percentile=5.0,
        history_days=7,
    )

    assert result["rare_values"] == []

    current_call = engine.execute.await_args_list[0]
    assert current_call.kwargs["query"] == (
        "'Log Source' = 'Linux Syslog Logs' "
        "| rare limit = -1 showcount = true showpercent = true 'Log Source'"
    )

    history_call = engine.execute.await_args_list[1]
    assert history_call.kwargs["query"] == (
        "'Log Source' = 'Linux Syslog Logs' "
        "| stats count as count_in_history, earliest(Time) as first_seen, latest(Time) as last_seen by 'Log Source'"
    )
    assert history_call.kwargs["time_range"] == "last_7_days"


@pytest.mark.asyncio
async def test_falls_back_to_absolute_history_window_for_unsupported_history_days(tool, engine):
    engine.execute.side_effect = [
        _response(
            ["Severity", "Rare Count(Severity)", "Rare Percent(Severity)"],
            [["WARN", 1, 1.0]],
        ),
        _response(
            ["Severity", "count_in_history", "first_seen", "last_seen"],
            [["WARN", 4, "2026-04-01T00:00:00+00:00", "2026-04-23T00:15:00+00:00"]],
        ),
    ]

    result = await tool.run(
        source="Linux Syslog Logs",
        field="Severity",
        time_range={"time_range": "last_24_hours"},
        history_days=5,
    )

    assert result["rare_values"][0]["value"] == "WARN"

    history_call = engine.execute.await_args_list[1]
    assert "time_range" not in history_call.kwargs
    assert history_call.kwargs["time_start"]
    assert history_call.kwargs["time_end"]
