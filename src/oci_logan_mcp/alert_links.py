"""Discover alert chains (saved search, scheduled task, metric, alarms) through OCI's own ids.

Our freeform tags are only a cache: an alert built in the console has none, but OCI still
stores the links (the task's savedSearchId, the task's metric extraction, the alarm's query).
"""
import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

_CONDITION_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\[[^\]]*\].*\)\s*(>=|<=|==|>|<)\s*(-?\d+(?:\.\d+)?)\s*$"
)
_OPERATOR_NAMES = {">": "gt", ">=": "gte", "==": "eq", "<": "lt", "<=": "lte"}
CACHE_CATEGORY = "schema"


@dataclass(frozen=True)
class LinkedAlert:
    alarm_id: str
    display_name: str
    severity: str
    lifecycle_state: str
    created_by: str
    condition: str
    threshold_operator: Optional[str]
    threshold_value: Optional[str]
    backing_saved_search_id: Optional[str]
    backing_metric_task_id: Optional[str]
    metric_namespace: Optional[str]
    metric_name: Optional[str]
    query: str
    schedule: str
    schedule_format: str


def parse_condition(mql: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (metric token, operator name, threshold text) from an alarm condition."""
    match = _CONDITION_RE.match(mql or "")
    if not match:
        return (None, None, None)
    return (match.group(1), _OPERATOR_NAMES[match.group(2)], match.group(3))


def _cache_key(client, compartment_id: Optional[str]) -> str:
    return f"alert_links:{compartment_id or client.compartment_id}"


def invalidate(cache, compartment_id: Optional[str], client=None) -> None:
    key = f"alert_links:{compartment_id}" if compartment_id else _cache_key(client, None)
    cache.delete(key, category=CACHE_CATEGORY)


def _is_logan_alarm(alarm: Dict[str, Any]) -> bool:
    tags = alarm.get("freeform_tags") or {}
    return tags.get("logan_managed") == "true" and tags.get("logan_kind") == "monitoring_alarm"


def _from_tagged(alarm: Dict[str, Any]) -> LinkedAlert:
    tags = alarm.get("freeform_tags") or {}
    metric, operator, threshold = parse_condition(alarm.get("query"))
    return LinkedAlert(
        alarm_id=alarm["id"], display_name=alarm.get("display_name", ""),
        severity=alarm.get("severity", ""), lifecycle_state=alarm.get("lifecycle_state", ""),
        created_by="logan", condition=alarm.get("query", "") or "",
        threshold_operator=operator, threshold_value=threshold,
        backing_saved_search_id=tags.get("logan_backing_saved_search_id"),
        backing_metric_task_id=tags.get("logan_backing_metric_task_id"),
        metric_namespace=alarm.get("namespace"), metric_name=metric,
        query=tags.get("logan_query", ""), schedule=tags.get("logan_schedule", ""),
        schedule_format="logan",
    )


def _watches(alarm: Dict[str, Any], task: Dict[str, Any]) -> bool:
    metric, _, _ = parse_condition(alarm.get("query"))
    if metric != task.get("metric_name") or alarm.get("namespace") != task.get("metric_namespace"):
        return False
    alarm_group, task_group = alarm.get("resource_group"), task.get("metric_resource_group")
    return not (alarm_group and task_group and alarm_group != task_group)


async def _console_matches(client, alarms: List[Dict[str, Any]], task: Dict[str, Any]) -> List[LinkedAlert]:
    if not task.get("metric_name") or not task.get("metric_namespace"):
        return []
    watching = [a for a in alarms if _watches(a, task)]
    if not watching:
        return []
    query = ""
    if task.get("saved_search_id"):
        query = (await client.get_saved_search(task["saved_search_id"])).get("query", "") or ""
    results = []
    for alarm in watching:
        _, operator, threshold = parse_condition(alarm.get("query"))
        results.append(LinkedAlert(
            alarm_id=alarm["id"], display_name=alarm.get("display_name", ""),
            severity=alarm.get("severity", ""), lifecycle_state=alarm.get("lifecycle_state", ""),
            created_by="logan" if _is_logan_alarm(alarm) else "console",
            condition=alarm.get("query", "") or "",
            threshold_operator=operator, threshold_value=threshold,
            backing_saved_search_id=task.get("saved_search_id"), backing_metric_task_id=task["id"],
            metric_namespace=task.get("metric_namespace"), metric_name=task.get("metric_name"),
            query=query, schedule=task.get("cron") or "", schedule_format="oci",
        ))
    return results


async def find_alerts_for_saved_search(client, cache, saved_search_id: str,
                                       compartment_id: Optional[str] = None) -> List[LinkedAlert]:
    """Alarms that depend on one saved search: our tags first, then OCI's own links."""
    alarms = await client.list_alarms(compartment_id)
    tagged = [_from_tagged(a) for a in alarms
              if (a.get("freeform_tags") or {}).get("logan_backing_saved_search_id") == saved_search_id]
    if tagged:
        return tagged
    results: List[LinkedAlert] = []
    for task in await client.list_scheduled_tasks_for_saved_search(saved_search_id):
        details = await client.get_scheduled_task_details(task["id"])
        results.extend(await _console_matches(client, alarms, details))
    return results


async def find_log_alerts(client, cache, compartment_id: Optional[str] = None) -> List[LinkedAlert]:
    """Every alarm backed by a Log Analytics scheduled search, ours or console-made (cached)."""
    key = _cache_key(client, compartment_id)
    cached = cache.get(key, category=CACHE_CATEGORY)
    if cached is not None:
        return [LinkedAlert(**row) for row in cached]
    alarms = await client.list_alarms(compartment_id)
    found: Dict[str, LinkedAlert] = {a.alarm_id: a for a in (_from_tagged(x) for x in alarms if _is_logan_alarm(x))}
    for task in await client.list_saved_searches():
        details = await client.get_scheduled_task_details(task["id"])
        for alert in await _console_matches(client, alarms, details):
            found.setdefault(alert.alarm_id, alert)
    results = list(found.values())
    cache.set(key, [asdict(a) for a in results], category=CACHE_CATEGORY)
    return results
