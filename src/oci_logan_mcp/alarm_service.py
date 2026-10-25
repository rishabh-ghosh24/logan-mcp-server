"""OCI-native autonomous alert orchestration."""
import re
import logging
from typing import Any, Dict, List, Optional
from uuid import uuid4

import oci

from .client import OCILogAnalyticsClient
from .cache import CacheManager
from .saved_search_config import build_scope_filters, build_ui_config
from .alert_links import CONSOLE_STAMP, alarms_watching, find_log_alerts, origin_of

# OCI Management Dashboard API requires crossService in featuresConfig
FEATURES_CONFIG = {"crossService": {"shared": True}}

logger = logging.getLogger(__name__)

_CRON_FIELD = r"(\*|(\*\/\d+)|\d+(-\d+)?(\/\d+)?(,(\*|(\*\/\d+)|\d+(-\d+)?(\/\d+)?))*)"
_CRON_RE = re.compile(
    r"^" + r"\s+".join([_CRON_FIELD] * 5) + r"$"
)
_AGGREGATION_RE = re.compile(r"\|\s*stats\b", re.IGNORECASE)

# Every 15 minutes; the task, handler and tool description share this value.
DEFAULT_ALERT_SCHEDULE = "*/15 * * * *"
# The task posts under this resource group; the alarm must name the same one, because an
# alarm without a resource group only sees metrics that have none.
METRIC_RESOURCE_GROUP = "logan_alerts"
# One datapoint per run: the first breaching evaluation must fire (OCI minimum is PT1M).
ALERT_PENDING_DURATION = "PT1M"


class AlarmService:
    def __init__(self, oci_client: OCILogAnalyticsClient, cache: CacheManager):
        self.oci_client = oci_client
        self.cache = cache

    _OPERATORS = {"gt": ">", "gte": ">=", "eq": "==", "lt": "<", "lte": "<="}

    def _validate_cron(self, expr: str) -> str:
        return self._parse_cron(expr)[0]

    @classmethod
    def alarm_interval(cls, schedule: str) -> str:
        """MQL interval long enough that the latest run's datapoint is always in the window."""
        _, fields, values = cls._parse_cron(schedule)
        minutes, hours = sorted(values[0]), sorted(values[1])
        if fields[1] == "*":
            runs, period = minutes, 60
        else:
            runs, period = sorted(h * 60 + m for h in hours for m in minutes), 1440
        gap = max([b - a for a, b in zip(runs, runs[1:])] + [period - runs[-1] + runs[0]])
        if any(field != "*" for field in fields[2:]) or gap >= 1440:
            return "1d"
        if gap < 60:
            return f"{gap}m"
        if gap % 60 == 0:
            return f"{gap // 60}h"
        return "1d"

    @classmethod
    def _condition(cls, metric_name: str, interval: str, operator: str, threshold) -> str:
        """Compare the latest extracted value; count() would only count datapoints, and max()
        would keep an old breach alive after a newer clean run on a sparse schedule."""
        return f"{metric_name}[{interval}].last() {cls._OPERATORS.get(operator, '>')} {threshold}"

    def _repair_fields(self, alarm: Dict[str, Any], *, schedule: Optional[str] = None,
                       threshold_value=None, threshold_operator: Optional[str] = None) -> Dict[str, Any]:
        """Rebuild the condition from the current operator and threshold, and set the
        resource group and pending duration that alarms from earlier releases lack."""
        tags = alarm.get("freeform_tags", {}) or {}
        current_mql = alarm.get("query", "") or ""
        metric_name = current_mql.split("[")[0].strip()
        if not metric_name:
            raise ValueError(
                "Cannot update threshold: alarm query is missing or not in Logan MQL format. "
                "Retrieve the alarm with get_alarm to inspect its current query."
            )
        current = re.search(r"\)\s*(>=|<=|==|>|<)\s*(-?\d+(?:\.\d+)?)\s*$", current_mql)
        symbols = {symbol: name for name, symbol in self._OPERATORS.items()}
        operator = threshold_operator or (symbols[current.group(1)] if current else "gt")
        tv = threshold_value if threshold_value is not None else (current.group(2) if current else 0)
        if origin_of(alarm) == "console":
            # Not ours: keep the author's interval and statistic, change only the comparison.
            if current is None or "&&" in current_mql or "||" in current_mql:
                raise ValueError(
                    f"Cannot change the threshold of alert '{alarm.get('display_name', '')}': its "
                    f"condition '{current_mql}' combines several comparisons or has no simple "
                    "threshold. Change it in the OCI Console."
                )
            head = current_mql[:current.start() + 1]
            return {"query": f"{head.rstrip()} {self._OPERATORS.get(operator, '>')} {tv}"}
        effective_schedule = schedule or tags.get("logan_schedule") or DEFAULT_ALERT_SCHEDULE
        return {
            "query": self._condition(metric_name, self.alarm_interval(effective_schedule), operator, tv),
            "resource_group": METRIC_RESOURCE_GROUP,
            "pending_duration": ALERT_PENDING_DURATION,
        }

    def refresh_alarm_for_query(self, alarm: Dict[str, Any], query: str, *,
                                stamp: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Alarm fields to write when the query behind an alert changes: the stored query
        text (tag and notification body), optional ownership tags, and the repair."""
        tags = dict(alarm.get("freeform_tags", {}) or {})
        tags.update(stamp or {})
        tags["logan_query"] = query
        if origin_of(alarm) == "console" or tags.get("logan_origin") == "console":
            # Not ours: record the query and ownership, leave the author's condition and text.
            return {"freeform_tags": tags}
        return {
            "freeform_tags": tags,
            "body": f"Logan alert: {alarm.get('display_name', '')}\nQuery: {query}",
            **self._repair_fields(alarm),
        }

    @classmethod
    def _parse_cron(cls, expr: str):
        if not _CRON_RE.match(expr.strip()):
            raise ValueError(
                f"Invalid cron expression '{expr}'. "
                "Expected 5-field format: min hour day month weekday (e.g. '*/15 * * * *')"
            )
        fields = expr.split()
        values = []
        for field, (low, high) in zip(fields, ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))):
            expanded = set()
            for part in field.split(","):
                base, _, step = part.partition("/")
                stride = int(step) if step else 1
                if base == "*":
                    start, end = low, high
                elif "-" in base:
                    start, end = map(int, base.split("-"))
                else:
                    start = int(base)
                    end = high if step else start
                if stride < 1 or not low <= start <= end <= high:
                    raise ValueError("Invalid cron field range or step.")
                expanded.update(range(start, end + 1, stride))
            values.append(expanded)
        minute, hour, day, month, weekday = fields
        if day != "*" and weekday != "*":
            raise ValueError("Invalid cron schedule: choose day of month or weekday, not both.")
        if weekday == "*" and day != "*":
            weekday = "?"
        else:
            day = "?"
            if weekday != "*":
                weekday = ",".join(str(value) for value in sorted({value % 7 + 1 for value in values[4]}))
        return f"0 {minute} {hour} {day} {month} {weekday}", fields, values

    def _validate_metric_query(self, query: str) -> None:
        if not _AGGREGATION_RE.search(query):
            raise ValueError(
                "Alert query must produce a numeric aggregation result. "
                "Use '| stats count' or '| stats avg(field)'. "
                "Raw log queries cannot be used as alert sources."
            )

    async def create_alert(
        self,
        display_name: str,
        query: str,
        destination_topic_id: str,
        schedule: str = DEFAULT_ALERT_SCHEDULE,
        threshold_value: int = 0,
        threshold_operator: str = "gt",
        severity: str = "CRITICAL",
        compartment_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        cid = compartment_id or self.oci_client.compartment_id
        self.cache.delete('saved_searches')

        native_schedule = self._validate_cron(schedule)
        interval = self.alarm_interval(schedule)
        self._validate_metric_query(query)
        await self.oci_client.get_topic(destination_topic_id)

        group_id = uuid4()
        metric_name = f"logan_alert_{group_id.hex[:12]}"
        group_id_str = str(group_id)
        base_tags = {"logan_managed": "true", "logan_group_id": group_id_str}

        saved_search_id = None
        task_id = None

        try:
            mss_details = oci.management_dashboard.models.CreateManagementSavedSearchDetails(
                display_name=f"logan-alert-{display_name}",
                description=f"Backing saved search for alert: {display_name}",
                compartment_id=cid,
                is_oob_saved_search=False,
                type="SEARCH_DONT_SHOW_IN_DASHBOARD",
                provider_id="log-analytics",
                provider_name="Log Analytics",
                provider_version="3.0.0",
                metadata_version="2.0",
                nls={},
                data_config=[{"query": query}],
                ui_config=build_ui_config(query, "summary_table", build_scope_filters(
                    cid, self.oci_client.tenancy_id, self.oci_client._config["region"],
                )),
                screen_image=" ",
                widget_template="visualizations/chartWidgetTemplate.html",
                widget_vm="jet-modules/dashboards/widgets/lxSavedSearchWidget",
                parameters_config=[],
                drilldown_config=[],
                features_config=FEATURES_CONFIG,
                freeform_tags={**base_tags, "logan_kind": "alert_saved_search"},
            )
            mss = await self.oci_client.create_management_saved_search(mss_details)
            saved_search_id = mss["id"]

            task_details = oci.log_analytics.models.CreateStandardTaskDetails(
                kind="STANDARD",
                task_type="SAVED_SEARCH",
                display_name=f"logan-alert-task-{display_name}",
                compartment_id=cid,
                schedules=[oci.log_analytics.models.CronSchedule(
                    type="CRON",
                    expression=native_schedule,
                    time_zone="UTC",
                )],
                action=oci.log_analytics.models.StreamAction(
                    saved_search_id=saved_search_id,
                    saved_search_duration="PT1H",
                    metric_extraction=oci.log_analytics.models.MetricExtraction(
                        compartment_id=cid,
                        namespace="logan_custom_metrics",
                        metric_name=metric_name,
                        resource_group=METRIC_RESOURCE_GROUP,
                    ),
                ),
                freeform_tags={**base_tags, "logan_kind": "alert_metric_task"},
            )
            task = await self.oci_client.create_scheduled_task(task_details)
            task_id = task["id"]

            mql = self._condition(metric_name, interval, threshold_operator, threshold_value)
            alarm_details = oci.monitoring.models.CreateAlarmDetails(
                display_name=display_name,
                compartment_id=cid,
                metric_compartment_id=cid,
                namespace="logan_custom_metrics",
                resource_group=METRIC_RESOURCE_GROUP,
                query=mql,
                severity=severity,
                destinations=[destination_topic_id],
                is_enabled=True,
                pending_duration=ALERT_PENDING_DURATION,
                body=f"Logan alert: {display_name}\nQuery: {query}",
                message_format="ONS_OPTIMIZED",
                freeform_tags={
                    **base_tags,
                    "logan_kind": "monitoring_alarm",
                    "logan_backing_saved_search_id": saved_search_id,
                    "logan_backing_metric_task_id": task_id,
                    "logan_query": query,
                    "logan_schedule": schedule,
                },
            )
            alarm = await self.oci_client.create_alarm(alarm_details)

        except Exception:
            if task_id:
                try:
                    await self.oci_client.delete_scheduled_task(task_id)
                except Exception:
                    pass
            if saved_search_id:
                try:
                    await self.oci_client.delete_management_saved_search(saved_search_id)
                except Exception:
                    pass
            raise

        return {
            "alarm_id": alarm["id"],
            "backing_saved_search_id": saved_search_id,
            "backing_metric_task_id": task_id,
            "destination_topic_id": destination_topic_id,
            "alert_group": group_id_str,
            "display_name": display_name,
            "query": query,
            "schedule": schedule,
            "threshold": f"value {threshold_operator} {threshold_value}",
            "severity": severity,
            "metric_name": metric_name,
            "status": "ACTIVE",
        }

    async def list_alerts(self, compartment_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Every alarm backed by a Log Analytics scheduled search, ours or console-made."""
        alerts = await find_log_alerts(self.oci_client, compartment_id)
        return [{
            "alarm_id": a.alarm_id,
            "display_name": a.display_name,
            "severity": a.severity,
            "lifecycle_state": a.lifecycle_state,
            "created_by": a.created_by,
            "query": a.query,
            "condition": a.condition,
            "threshold_operator": a.threshold_operator,
            "threshold_value": a.threshold_value,
            "schedule": a.schedule,
            "schedule_format": a.schedule_format,
            "backing_saved_search_id": a.backing_saved_search_id,
            "backing_metric_task_id": a.backing_metric_task_id,
            "alert_group": a.alert_group,
            "created_by_user": a.created_by_user,
            "created_on": a.created_on,
        } for a in alerts]

    async def delete_alert(self, alert_id: str) -> Dict[str, Any]:
        """Remove the alarm and its detection rule. The saved search is never deleted here, and
        the rule is kept while another alarm still reads the metric it posts."""
        self.cache.delete('saved_searches')
        alarm = await self.oci_client.get_alarm(alert_id)
        tags = alarm.get("freeform_tags", {}) or {}
        task_id = tags.get("logan_backing_metric_task_id")
        saved_search_id = tags.get("logan_backing_saved_search_id")
        if not task_id:
            # A console-made alert carries none of our tags: follow OCI's own links instead.
            for linked in await find_log_alerts(self.oci_client, alarm.get("compartment_id")):
                if linked.alarm_id == alert_id:
                    task_id, saved_search_id = linked.backing_metric_task_id, linked.backing_saved_search_id
                    break

        kept = []
        if saved_search_id:
            kept.append({"resource": "saved_search", "id": saved_search_id,
                         "reason": "delete_alert never deletes saved searches; use delete_saved_search "
                                   "if it is no longer wanted"})
        if task_id:
            others = await self._other_alerts_on_rule(task_id, alert_id, alarm.get("compartment_id"))
            if others is None:
                task_id = None
            elif others:
                kept.append({"resource": "detection_rule", "id": task_id,
                             "reason": "still used by alert(s) " + ", ".join(f"'{name}'" for name in others)})
                task_id = None

        deleted = []
        remaining = []
        for resource_id, delete_fn, label in [
            (task_id, self.oci_client.delete_scheduled_task, "metric_task"),
            (alert_id, self.oci_client.delete_alarm, "alarm"),
        ]:
            if not resource_id:
                continue
            try:
                await delete_fn(resource_id)
                deleted.append(label)
            except oci.exceptions.ServiceError as e:
                if e.status == 404:
                    deleted.append(label)
                else:
                    remaining.append({"label": label, "id": resource_id, "error": str(e)})

        result: Dict[str, Any] = {"deleted": deleted, "kept": kept, "partial_failure": False}
        if remaining:
            result.update(partial_failure=True, remaining=[r["id"] for r in remaining], details=remaining)
        return result

    async def _other_alerts_on_rule(self, task_id: str, alert_id: str,
                                    compartment_id: Optional[str]) -> Optional[List[str]]:
        """Return other consumers, or None only when inventory confirms the rule is gone."""
        try:
            rule = await self.oci_client.get_scheduled_task_details(task_id)
        except oci.exceptions.ServiceError as e:
            if e.status in (400, 404):
                tasks = await self.oci_client.list_scheduled_tasks_for_saved_search(
                    compartment_id=compartment_id or self.oci_client.compartment_id,
                )
                if not isinstance(tasks, list) or any(not row.get('id') for row in tasks):
                    raise RuntimeError('Could not verify detection-rule absence.') from None
                if not any(row['id'] == task_id and row.get('lifecycle_state') != 'DELETED' for row in tasks):
                    return None
            raise
        alarms = await self.oci_client.list_alarms(compartment_id)
        return [a.get("display_name") or a["id"] for a in alarms_watching(alarms, rule) if a["id"] != alert_id]

    async def update_alert(self, alert_id: str, **kwargs) -> Dict[str, Any]:
        self.cache.delete('saved_searches')
        alarm = await self.oci_client.get_alarm(alert_id)
        tags = alarm.get("freeform_tags", {})
        task_id = tags.get("logan_backing_metric_task_id")
        saved_search_id = tags.get("logan_backing_saved_search_id")

        display_name = kwargs.get("display_name")
        query = kwargs.get("query")
        schedule = kwargs.get("schedule")
        threshold_value = kwargs.get("threshold_value")
        threshold_operator = kwargs.get("threshold_operator")
        severity = kwargs.get("severity")
        destination_topic_id = kwargs.get("destination_topic_id")

        console_origin = origin_of(alarm) == "console"
        if console_origin and (query or schedule or display_name) and not (saved_search_id and task_id):
            links = await find_log_alerts(self.oci_client, alarm.get("compartment_id"))
            linked = next((row for row in links if row.alarm_id == alert_id), None)
            if linked:
                saved_search_id, task_id = linked.backing_saved_search_id, linked.backing_metric_task_id
            if not (saved_search_id and task_id):
                raise ValueError("Cannot resolve this console alert's backing resources. "
                                 "Inspect its rule in OCI before retrying; no changes were made.")

        if query and saved_search_id:
            self._validate_metric_query(query)
            current = await self.oci_client.get_saved_search(saved_search_id)
            mss_update = oci.management_dashboard.models.UpdateManagementSavedSearchDetails(
                data_config=[{"query": query}],
                ui_config={**(current["ui_config"] if "ui_config" in current else {}),
                           "queryString": query},
            )
            if display_name:
                mss_update.display_name = f"logan-alert-{display_name}"
            await self.oci_client.update_management_saved_search(saved_search_id, mss_update)
        elif query:
            self._validate_metric_query(query)  # validate even if we can't update

        if display_name and not query and saved_search_id:
            mss_update = oci.management_dashboard.models.UpdateManagementSavedSearchDetails(
                display_name=f"logan-alert-{display_name}",
            )
            await self.oci_client.update_management_saved_search(saved_search_id, mss_update)

        if schedule:
            native_schedule = self._validate_cron(schedule)
            task_update = oci.log_analytics.models.UpdateStandardTaskDetails(
                kind="STANDARD",
                schedules=[oci.log_analytics.models.CronSchedule(
                    type="CRON", expression=native_schedule, time_zone="UTC"
                )],
            )
            if display_name:
                task_update.display_name = f"logan-alert-task-{display_name}"
            await self.oci_client.update_scheduled_task(task_id, task_update)
        elif display_name and not query:
            task_update = oci.log_analytics.models.UpdateStandardTaskDetails(
                kind="STANDARD",
                display_name=f"logan-alert-task-{display_name}",
            )
            await self.oci_client.update_scheduled_task(task_id, task_update)

        alarm_update_fields = {}
        if query:
            # The alarm keeps a copy of the query for list_alerts and the notification body;
            # refresh both and repair the alarm in the same update.
            stamp = {**CONSOLE_STAMP, "logan_backing_saved_search_id": saved_search_id,
                     "logan_backing_metric_task_id": task_id} if console_origin else None
            alarm_update_fields.update(self.refresh_alarm_for_query(alarm, query, stamp=stamp))
        if threshold_value is not None or threshold_operator is not None or schedule:
            alarm_update_fields.update(self._repair_fields(
                alarm, schedule=schedule, threshold_value=threshold_value,
                threshold_operator=threshold_operator,
            ))
        if severity:
            alarm_update_fields["severity"] = severity
        if destination_topic_id:
            alarm_update_fields["destinations"] = [destination_topic_id]
        if display_name:
            alarm_update_fields["display_name"] = display_name

        if schedule:
            new_tags = dict(alarm_update_fields.get("freeform_tags") or tags)
            new_tags["logan_schedule"] = schedule
            alarm_update_fields["freeform_tags"] = new_tags

        if alarm_update_fields:
            alarm_details = oci.monitoring.models.UpdateAlarmDetails(**alarm_update_fields)
            await self.oci_client.update_alarm(alert_id, alarm_details)

        changed = [field for field in ("display_name", "query", "schedule", "threshold_value",
                                     "threshold_operator", "severity", "destination_topic_id")
                   if kwargs.get(field) is not None]
        return {"alarm_id": alert_id, "updated": changed}
