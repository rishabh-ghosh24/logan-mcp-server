"""OCI-native autonomous alert orchestration."""
import re
import logging
from typing import Any, Dict, List, Optional
from uuid import uuid4

import oci

from .client import OCILogAnalyticsClient
from .cache import CacheManager

# OCI Management Dashboard API requires crossService in featuresConfig
FEATURES_CONFIG = {"crossService": {"shared": True}, "dependencies": []}

logger = logging.getLogger(__name__)

_CRON_FIELD = r"(\*|(\*\/\d+)|\d+(-\d+)?(\/\d+)?(,(\*|(\*\/\d+)|\d+(-\d+)?(\/\d+)?))*)"
_CRON_RE = re.compile(
    r"^" + r"\s+".join([_CRON_FIELD] * 5) + r"$"
)
_AGGREGATION_RE = re.compile(r"\|\s*stats\b", re.IGNORECASE)


class AlarmService:
    def __init__(self, oci_client: OCILogAnalyticsClient, cache: CacheManager):
        self.oci_client = oci_client
        self.cache = cache

    def _validate_cron(self, expr: str) -> None:
        if not _CRON_RE.match(expr.strip()):
            raise ValueError(
                f"Invalid cron expression '{expr}'. "
                "Expected 5-field format: min hour day month weekday (e.g. '0 */15 * * *')"
            )

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
        schedule: str = "0 */15 * * *",
        threshold_value: int = 0,
        threshold_operator: str = "gt",
        severity: str = "CRITICAL",
        compartment_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        cid = compartment_id or self.oci_client.compartment_id

        self._validate_cron(schedule)
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
                provider_name="Logging Analytics",
                provider_version="3.0.0",
                metadata_version="2.0",
                nls={},
                data_config=[{"query": query}],
                screen_image="to-do",
                widget_template="visualizations/chartWidgetTemplate.html",
                widget_vm="visualizations/chartWidget",
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
                    expression=schedule,
                    time_zone="UTC",
                )],
                action=oci.log_analytics.models.StreamAction(
                    saved_search_id=saved_search_id,
                    saved_search_duration="PT1H",
                    metric_extraction=oci.log_analytics.models.MetricExtraction(
                        compartment_id=cid,
                        namespace="logan_custom_metrics",
                        metric_name=metric_name,
                        resource_group="logan_alerts",
                    ),
                ),
                freeform_tags={**base_tags, "logan_kind": "alert_metric_task"},
            )
            task = await self.oci_client.create_scheduled_task(task_details)
            task_id = task["id"]

            op_map = {"gt": ">", "gte": ">=", "eq": "==", "lt": "<", "lte": "<="}
            op_sym = op_map.get(threshold_operator, ">")
            mql = f"{metric_name}[1m].count() {op_sym} {threshold_value}"
            alarm_details = oci.monitoring.models.CreateAlarmDetails(
                display_name=display_name,
                compartment_id=cid,
                metric_compartment_id=cid,
                namespace="logan_custom_metrics",
                query=mql,
                severity=severity,
                destinations=[destination_topic_id],
                is_enabled=True,
                pending_duration="PT5M",
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
            "threshold": f"count {threshold_operator} {threshold_value}",
            "severity": severity,
            "metric_name": metric_name,
            "status": "ACTIVE",
        }

    async def list_alerts(self, compartment_id: Optional[str] = None) -> List[Dict[str, Any]]:
        alarms = await self.oci_client.list_alarms(compartment_id)
        results = []
        for a in alarms:
            tags = a.get("freeform_tags", {})
            if tags.get("logan_managed") == "true" and tags.get("logan_kind") == "monitoring_alarm":
                results.append({
                    "alarm_id": a["id"],
                    "display_name": a.get("display_name", ""),
                    "severity": a.get("severity", ""),
                    "lifecycle_state": a.get("lifecycle_state", ""),
                    "query": tags.get("logan_query", ""),
                    "schedule": tags.get("logan_schedule", ""),
                    "backing_saved_search_id": tags.get("logan_backing_saved_search_id"),
                    "backing_metric_task_id": tags.get("logan_backing_metric_task_id"),
                    "alert_group": tags.get("logan_group_id"),
                })
        return results

    async def delete_alert(self, alert_id: str) -> Dict[str, Any]:
        alarm = await self.oci_client.get_alarm(alert_id)
        tags = alarm.get("freeform_tags", {})
        task_id = tags.get("logan_backing_metric_task_id")
        saved_search_id = tags.get("logan_backing_saved_search_id")

        deleted = []
        remaining = []

        for resource_id, delete_fn, label in [
            (task_id, self.oci_client.delete_scheduled_task, "metric_task"),
            (saved_search_id, self.oci_client.delete_management_saved_search, "backing_saved_search"),
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

        if remaining:
            return {
                "partial_failure": True,
                "deleted": deleted,
                "remaining": [r["id"] for r in remaining],
                "details": remaining,
            }
        return {"deleted": deleted, "partial_failure": False}

    async def update_alert(self, alert_id: str, **kwargs) -> Dict[str, Any]:
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

        if query and saved_search_id:
            self._validate_metric_query(query)
            mss_update = oci.management_dashboard.models.UpdateManagementSavedSearchDetails(
                data_config=[{"query": query}],
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
            self._validate_cron(schedule)
            task_update = oci.log_analytics.models.UpdateStandardTaskDetails(
                kind="STANDARD",
                schedules=[oci.log_analytics.models.CronSchedule(
                    type="CRON", expression=schedule, time_zone="UTC"
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
        current_mql = alarm.get("query", "")
        if threshold_value is not None or threshold_operator is not None:
            metric_name = current_mql.split("[")[0].strip()
            if not metric_name:
                raise ValueError(
                    "Cannot update threshold: alarm query is missing or not in Logan MQL format. "
                    "Retrieve the alarm with get_alarm to inspect its current query."
                )
            op_map = {"gt": ">", "gte": ">=", "eq": "==", "lt": "<", "lte": "<="}
            op_sym = op_map.get(threshold_operator or "gt", ">")
            tv = threshold_value if threshold_value is not None else 0
            alarm_update_fields["query"] = f"{metric_name}[1m].count() {op_sym} {tv}"
        if severity:
            alarm_update_fields["severity"] = severity
        if destination_topic_id:
            alarm_update_fields["destinations"] = [destination_topic_id]
        if display_name:
            alarm_update_fields["display_name"] = display_name

        if schedule:
            new_tags = dict(tags)
            new_tags["logan_schedule"] = schedule
            alarm_update_fields["freeform_tags"] = new_tags

        if alarm_update_fields:
            alarm_details = oci.monitoring.models.UpdateAlarmDetails(**alarm_update_fields)
            await self.oci_client.update_alarm(alert_id, alarm_details)

        return {"alarm_id": alert_id, "updated": list(kwargs.keys())}
