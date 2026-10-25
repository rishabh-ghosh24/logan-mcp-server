"""Discover alert chains (saved search, scheduled task, metric, alarms) through OCI's own ids.

Our freeform tags are only a cache: an alert built in the console has none, but OCI still
stores the links (the task's savedSearchId, the task's metric extraction, the alarm's query).
"""
import re
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Tuple

_METRIC_REFERENCE_RE = re.compile(r"(?<![A-Za-z0-9_.-])([A-Za-z_][A-Za-z0-9_.-]*)\s*\[")
_QUOTED_TEXT_RE = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*' ''', re.VERBOSE)
_COMPARISON_RE = re.compile(r"\)\s*(>=|<=|==|>|<)\s*(-?\d+(?:\.\d+)?)\s*$")
_OPERATOR_NAMES = {">": "gt", ">=": "gte", "==": "eq", "<": "lt", "<=": "lte"}


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
    alert_group: Optional[str] = None
    created_by_user: Optional[str] = None
    created_on: Optional[str] = None


def parse_condition(mql: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (metric token, operator name, threshold text) from an alarm condition.

    The metric token links an alarm to the rule that posts its metric. The operator and threshold
    are read only from a single simple comparison; a compound condition (&&, ||) or an absence
    test has no one threshold, so both come back None and the condition is never rewritten.
    """
    metric = _METRIC_REFERENCE_RE.search(_QUOTED_TEXT_RE.sub(' ', mql or ''))
    if not metric:
        return (None, None, None)
    if "&&" in mql or "||" in mql:
        return (metric.group(1), None, None)
    comparison = _COMPARISON_RE.search(mql)
    if not comparison:
        return (metric.group(1), None, None)
    return (metric.group(1), _OPERATOR_NAMES[comparison.group(1)], comparison.group(2))


CONSOLE_STAMP = {"logan_managed": "true", "logan_kind": "monitoring_alarm", "logan_origin": "console"}


def is_console_origin(alarm: Dict[str, Any]) -> bool:
    return (alarm.get("freeform_tags") or {}).get("logan_origin") == "console"


def creator(alarm: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Who created the alarm and when, from OCI's default tags (Oracle-Tags.CreatedBy / CreatedOn)."""
    for namespace in (alarm.get("defined_tags") or {}).values():
        if isinstance(namespace, dict) and namespace.get("CreatedBy"):
            return namespace.get("CreatedBy"), namespace.get("CreatedOn")
    return None, None


def origin_of(alarm: Dict[str, Any]) -> str:
    """'logan' for an alarm this server created, 'console' for any other."""
    return "console" if is_console_origin(alarm) or not _is_logan_alarm(alarm) else "logan"


def describe_origin(alarm: Dict[str, Any]) -> Optional[str]:
    """One sentence for a confirmation summary when the alert is not ours."""
    if origin_of(alarm) != "console":
        return None
    user, created_on = creator(alarm)
    by = f" by {user}" if user else ""
    on = f" on {created_on[:10]}" if created_on else ""
    return f"Created in the OCI Console{by}{on}, not by this server."


DELETION_SCOPE = ("Deleting removes the alarm and its detection rule (the rule is kept if another alert "
                  "still uses it). The backing saved search stays; use delete_saved_search to remove it.")


def alarms_watching(alarms: List[Dict[str, Any]], task: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Alarms whose condition reads the metric this detection rule posts."""
    return [a for a in alarms if _watches(a, task)]


def metric_references(condition: Optional[str]) -> set[str]:
    """Read every metric operand, excluding names inside quoted dimension values."""
    unquoted = _QUOTED_TEXT_RE.sub(' ', condition or '')
    return {match.group(1) for match in _METRIC_REFERENCE_RE.finditer(unquoted)}


def _is_logan_alarm(alarm: Dict[str, Any]) -> bool:
    tags = alarm.get("freeform_tags") or {}
    return tags.get("logan_managed") == "true" and tags.get("logan_kind") == "monitoring_alarm"


def _from_tagged(alarm: Dict[str, Any]) -> LinkedAlert:
    tags = alarm.get("freeform_tags") or {}
    metric, operator, threshold = parse_condition(alarm.get("query"))
    return LinkedAlert(
        alarm_id=alarm["id"], display_name=alarm.get("display_name", ""),
        severity=alarm.get("severity", ""), lifecycle_state=alarm.get("lifecycle_state", ""),
        created_by=tags.get("logan_origin", "logan"), condition=alarm.get("query", "") or "",
        threshold_operator=operator, threshold_value=threshold,
        backing_saved_search_id=tags.get("logan_backing_saved_search_id"),
        backing_metric_task_id=tags.get("logan_backing_metric_task_id"),
        metric_namespace=alarm.get("namespace"), metric_name=metric,
        query=tags.get("logan_query", ""), schedule=tags.get("logan_schedule", ""),
        schedule_format="logan", alert_group=tags.get("logan_group_id"),
        created_by_user=creator(alarm)[0], created_on=creator(alarm)[1],
    )


def _watches(alarm: Dict[str, Any], task: Dict[str, Any]) -> bool:
    if (task.get("metric_name") not in metric_references(alarm.get("query"))
            or alarm.get("namespace") != task.get("metric_namespace")):
        return False
    alarm_compartment = alarm.get("metric_compartment_id")
    task_compartment = task.get("metric_compartment_id")
    if alarm_compartment and task_compartment and alarm_compartment != task_compartment:
        return False
    alarm_group, task_group = alarm.get("resource_group"), task.get("metric_resource_group")
    return not (alarm_group and task_group and alarm_group != task_group)


def _task_schedule(task: Dict[str, Any]) -> Tuple[str, str]:
    """A detection rule runs on an OCI cron expression or on a fixed interval (ISO duration)."""
    if task.get("cron"):
        return task["cron"], "oci"
    if task.get("interval"):
        return task["interval"], "interval"
    return "", "unknown"


async def _console_matches(client, alarms: List[Dict[str, Any]], task: Dict[str, Any]) -> List[LinkedAlert]:
    if not task.get("metric_name") or not task.get("metric_namespace"):
        return []
    watching = [a for a in alarms if _watches(a, task)]
    if not watching:
        return []
    query = ""
    if task.get("saved_search_id"):
        query = (await client.get_saved_search(task["saved_search_id"])).get("query", "") or ""
    schedule, schedule_format = _task_schedule(task)
    results = []
    for alarm in watching:
        _, operator, threshold = parse_condition(alarm.get("query"))
        results.append(LinkedAlert(
            alarm_id=alarm["id"], display_name=alarm.get("display_name", ""),
            severity=alarm.get("severity", ""), lifecycle_state=alarm.get("lifecycle_state", ""),
            created_by=origin_of(alarm),
            condition=alarm.get("query", "") or "",
            threshold_operator=operator, threshold_value=threshold,
            backing_saved_search_id=task.get("saved_search_id"), backing_metric_task_id=task["id"],
            metric_namespace=task.get("metric_namespace"), metric_name=task.get("metric_name"),
            query=query, schedule=schedule, schedule_format=schedule_format,
            created_by_user=creator(alarm)[0], created_on=creator(alarm)[1],
        ))
    return results


async def find_alerts_for_saved_search(client, saved_search_id: str,
                                       compartment_id: Optional[str] = None) -> List[LinkedAlert]:
    """Merge tagged alarms with every alarm found through OCI's current rule links."""
    alarms = await client.list_alarms(compartment_id)
    tasks = await client.list_scheduled_tasks_for_saved_search(saved_search_id)
    details = [await client.get_scheduled_task_details(task["id"]) for task in tasks]
    eligible = []
    for alarm in alarms:
        metrics = metric_references(alarm.get("query"))
        producers = [task for task in details if task.get("metric_name") in metrics
                     and task.get("metric_namespace") == alarm.get("namespace")]
        cid = alarm.get("metric_compartment_id")
        if cid and producers and all(task.get("metric_compartment_id")
                and task["metric_compartment_id"] != cid for task in producers):
            continue
        eligible.append(alarm)
    tagged = [_from_tagged(a) for a in eligible
              if (a.get("freeform_tags") or {}).get("logan_backing_saved_search_id") == saved_search_id]
    results = {alert.alarm_id: alert for alert in tagged}
    for task in details:
        for alert in await _console_matches(client, eligible, task):
            previous = results.get(alert.alarm_id)
            if previous and previous.created_by == "logan":
                # Keep the public five-field schedule and grouping on our alarms.
                alert = replace(alert, schedule=previous.schedule,
                                schedule_format=previous.schedule_format,
                                alert_group=previous.alert_group)
            results[alert.alarm_id] = alert
    return list(results.values())


async def find_log_alerts(client, compartment_id: Optional[str] = None) -> List[LinkedAlert]:
    """Every alarm backed by a Log Analytics scheduled search, ours or console-made.

    Nothing here is cached: alarms and detection rules are read as they are now, so a change
    made in the console or through this server shows on the next call.
    """
    alarms = await client.list_alarms(compartment_id)
    found: Dict[str, LinkedAlert] = {a.alarm_id: a for a in (_from_tagged(x) for x in alarms if _is_logan_alarm(x))}
    for task in await client.list_saved_searches():
        details = await client.get_scheduled_task_details(task["id"])
        for alert in await _console_matches(client, alarms, details):
            found.setdefault(alert.alarm_id, alert)
    return list(found.values())


_SYMBOLS = {"gt": ">", "gte": ">=", "eq": "=", "lt": "<", "lte": "<="}


def linked_alert_from_alarm(alarm: Dict[str, Any]) -> LinkedAlert:
    """A LinkedAlert built from one alarm record and its tags (no OCI lookups)."""
    return _from_tagged(alarm)


def _fires_when(alert: LinkedAlert) -> str:
    if alert.threshold_operator and alert.threshold_value is not None:
        return f"fires when value {_SYMBOLS[alert.threshold_operator]} {alert.threshold_value}"
    return "fires on its current condition"


def describe_linked_alerts(alerts: List[LinkedAlert], *, via: str) -> Optional[str]:
    """The sentence shown in a confirmation summary before a query change is confirmed."""
    if not alerts:
        return None
    if via == "alert":
        alert = alerts[0]
        return (f"Alert '{alert.display_name}' {_fires_when(alert)}. It will use the new query from "
                "its next run. Its threshold stays unless you also pass threshold_value.")
    named = ", ".join(f"'{a.display_name}' ({a.created_by}-made; {_fires_when(a)})" for a in alerts)
    if len(alerts) == 1:
        return (f"This search feeds alert {named}. The alert will use the new query from its next run. "
                "Its threshold stays unless you change it with update_alert.")
    return (f"This search feeds alerts {named}. These alerts will use the new query from their next "
            "runs. Their thresholds stay unless you change them with update_alert.")
