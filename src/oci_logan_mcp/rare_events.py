"""A3 — find low-frequency field values using Logan's native `rare` command."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


_HISTORY_TIME_RANGES = {
    1: "last_24_hours",
    2: "last_2_days",
    7: "last_7_days",
    14: "last_14_days",
    30: "last_30_days",
}


class RareEventsTool:
    """Wrap Logan's native `rare` command with agent-friendly defaults."""

    def __init__(self, query_engine):
        self._engine = query_engine

    async def run(
        self,
        source: str,
        field: str,
        time_range: Dict[str, str],
        rarity_threshold_percentile: float = 5.0,
        history_days: int = 30,
    ) -> Dict[str, Any]:
        current = await self._engine.execute(
            query=(
                f"'Log Source' = '{source}' "
                f"| rare limit = -1 showcount = true showpercent = true {self._format_field(field)}"
            ),
            **time_range,
        )
        history = await self._engine.execute(
            query=(
                f"'Log Source' = '{source}' "
                f"| stats count as count_in_history, earliest(Time) as first_seen, latest(Time) as last_seen "
                f"by {self._format_field(field)}"
            ),
            **self._history_window(history_days),
        )

        current_rows = self._rows_as_dicts(current)
        history_rows = self._rows_as_dicts(history)
        history_by_value = {
            row.get(field): row
            for row in history_rows
            if row.get(field) is not None
        }

        rare_values = []
        count_name = self._metric_column(current, "Rare Count(")
        percent_name = self._metric_column(current, "Rare Percent(")

        for row in current_rows:
            value = row.get(field)
            if value is None:
                continue
            percent = row.get(percent_name)
            if percent is None or percent > rarity_threshold_percentile:
                continue
            history_row = history_by_value.get(value, {})
            rare_values.append(
                {
                    "value": value,
                    "count_in_range": row.get(count_name),
                    "percent_in_range": percent,
                    "count_in_history": history_row.get("count_in_history"),
                    "first_seen": history_row.get("first_seen"),
                    "last_seen": history_row.get("last_seen"),
                }
            )

        rare_values.sort(
            key=lambda entry: (
                entry.get("percent_in_range") is None,
                entry.get("percent_in_range") or 0,
                entry.get("count_in_range") or 0,
                str(entry.get("value") or ""),
            )
        )

        return {
            "source": source,
            "field": field,
            "time_range": time_range,
            "history_days": history_days,
            "rarity_threshold_percentile": rarity_threshold_percentile,
            "rare_values": rare_values,
        }

    @staticmethod
    def _format_field(field: str) -> str:
        if field.replace("_", "").isalnum():
            return field
        return f"'{field}'"

    @staticmethod
    def _rows_as_dicts(response: Dict[str, Any]) -> List[Dict[str, Any]]:
        data = response.get("data", {}) or {}
        columns = [column.get("name") for column in data.get("columns", [])]
        rows = []
        for row in data.get("rows", []) or []:
            rows.append(dict(zip(columns, row)))
        return rows

    @staticmethod
    def _metric_column(response: Dict[str, Any], prefix: str) -> Optional[str]:
        data = response.get("data", {}) or {}
        for column in data.get("columns", []) or []:
            name = column.get("name")
            if isinstance(name, str) and name.startswith(prefix):
                return name
        return None

    @staticmethod
    def _history_window(history_days: int) -> Dict[str, str]:
        time_range = _HISTORY_TIME_RANGES.get(history_days)
        if time_range is not None:
            return {"time_range": time_range}

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=history_days)
        return {
            "time_start": start.isoformat(),
            "time_end": end.isoformat(),
        }
