"""Stateful OCI boundaries for the final blocking-finding regressions."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import oci

from oci_logan_mcp.audit import AuditLogger
from oci_logan_mcp.cache import CacheManager
from oci_logan_mcp.config import Settings
from oci_logan_mcp.handlers import MCPHandlers
from oci_logan_mcp.query_logger import QueryLogger
from oci_logan_mcp.secret_store import SecretStore
from oci_logan_mcp.user_store import UserStore

CID = "ocid1.compartment.example"
SEARCH = "ocid1.managementsavedsearch.oc1..current"
RULE = "ocid1.loganalyticsscheduledtask.oc1..current"
ALARM = "ocid1.alarm.oc1..current"
OLD_QUERY = "* | stats count as old"
NEW_QUERY = "* | stats count as revised"


class MemoryOCI:
    """Persist fake cloud writes so a no-op service cannot satisfy a test."""

    def __init__(self):
        self.namespace, self.compartment_id = "example", CID
        self.tenancy_id = "ocid1.tenancy.example"
        self._config = {"region": "us-ashburn-1", "tenancy": self.tenancy_id}
        self._signer = SimpleNamespace()
        self.searches = {SEARCH: {"id": SEARCH, "compartment_id": CID, "display_name": "Example search",
                         "query": OLD_QUERY, "ui_config": {"queryString": OLD_QUERY, "visualizationType": "bar"}}}
        self.rules = {RULE: {"id": RULE, "compartment_id": CID, "saved_search_id": SEARCH,
                     "metric_name": "metric_current", "metric_namespace": "example_metrics",
                     "metric_compartment_id": CID, "metric_resource_group": "example_group",
                     "cron": "0 */5 * ? * *", "lifecycle_state": "ACTIVE"}}
        self.alarms = {ALARM: {"id": ALARM, "display_name": "Example alert", "compartment_id": CID,
                      "namespace": "example_metrics", "metric_compartment_id": CID,
                      "resource_group": "example_group", "query": "metric_current[5m].last() > 4",
                      "lifecycle_state": "ACTIVE", "severity": "INFO", "body": "Example notification",
                      "freeform_tags": {"logan_managed": "true", "logan_kind": "monitoring_alarm",
                          "logan_backing_saved_search_id": SEARCH, "logan_backing_metric_task_id": RULE,
                          "logan_schedule": "*/5 * * * *", "logan_query": OLD_QUERY}}}
        self.dashboards = {"example-dashboard": {"id": "example-dashboard", "tiles": []}}
        self.fail_operations = set()
        self.writes = []
        self.query_calls = []

    def _write(self, kind, identifier):
        if (kind, identifier) in self.fail_operations:
            raise oci.exceptions.ServiceError(503, "Unavailable", {}, "Synthetic provider failure")
        self.writes.append((kind, identifier))

    async def get_alarm(self, identifier):
        return deepcopy(self.alarms[identifier])

    async def list_alarms(self, compartment_id=None):
        return [deepcopy(row) for row in self.alarms.values() if row["compartment_id"] == (compartment_id or CID)]

    async def list_saved_searches(self):
        return [{"id": key} for key in self.rules]

    async def list_scheduled_tasks_for_saved_search(self, saved_search_id=None, compartment_id=None):
        return [{"id": key, "lifecycle_state": row["lifecycle_state"]} for key, row in self.rules.items()
                if (saved_search_id is None or row["saved_search_id"] == saved_search_id)
                and row["compartment_id"] == (compartment_id or CID)]

    async def get_scheduled_task_details(self, identifier):
        if identifier not in self.rules:
            raise oci.exceptions.ServiceError(404, "NotFound", {}, "Synthetic missing rule")
        return deepcopy(self.rules[identifier])

    async def get_saved_search(self, identifier):
        if identifier in self.rules:
            row = self.rules[identifier]
            return {"id": identifier, "_action": SimpleNamespace(saved_search_id=row["saved_search_id"])}
        return deepcopy(self.searches[identifier])

    async def update_management_saved_search(self, identifier, details):
        self._write("search", identifier)
        row = self.searches[identifier]
        if details.data_config is not None:
            row["data_config"] = deepcopy(details.data_config)
        if details.ui_config is not None:
            row["ui_config"] = deepcopy(details.ui_config)
            row["query"] = details.ui_config["queryString"]
        if details.display_name is not None:
            row["display_name"] = details.display_name
        return {"id": identifier}

    async def update_scheduled_task(self, identifier, details):
        self._write("rule", identifier)
        row = self.rules[identifier]
        if details.schedules is not None:
            row["cron"] = details.schedules[0].expression
        if details.display_name is not None:
            row["display_name"] = details.display_name
        return {"id": identifier}

    async def update_alarm(self, identifier, details):
        self._write("alarm", identifier)
        for name in ("query", "freeform_tags", "body", "resource_group", "pending_duration",
                     "display_name", "severity", "destinations"):
            value = getattr(details, name, None)
            if value is not None:
                self.alarms[identifier][name] = deepcopy(value)
        return {"id": identifier}

    async def delete_scheduled_task(self, identifier):
        self._write("delete_rule", identifier)
        del self.rules[identifier]

    async def delete_alarm(self, identifier):
        self._write("delete_alarm", identifier)
        del self.alarms[identifier]

    async def get_management_dashboard(self, identifier):
        return deepcopy(self.dashboards[identifier])

    async def delete_management_dashboard(self, identifier):
        self._write("delete_dashboard", identifier)
        del self.dashboards[identifier]

    async def query(self, **arguments):
        self.query_calls.append(arguments)
        return {"columns": [{"name": "count"}], "rows": [[7]], "total_count": 1}


def make_handlers(tmp_path, client=None):
    settings = Settings()
    settings.log_analytics.namespace, settings.log_analytics.default_compartment_id = "example", CID
    settings.logging.query_logging = False
    settings.report_delivery.artifact_dir = tmp_path / "reports"
    return MCPHandlers(settings, client or MemoryOCI(), CacheManager(), QueryLogger(settings.logging),
                       MagicMock(), UserStore(tmp_path / "users-state", user_id="review"),
                       secret_store=SecretStore(tmp_path / "secret.yaml"),
                       audit_logger=AuditLogger(tmp_path / "audit"))


def payload(result):
    assert isinstance(result, list) and result, "The tool must return its operation evidence"
    return json.loads(result[0]["text"])


async def confirmed_call(handlers, name, arguments):
    secret = "synthetic-confirmation-for-this-test"
    handlers.secret_store.set_secret(secret)
    preview = payload(await handlers.handle_tool_call(name, arguments))
    assert preview["status"] == "confirmation_required"
    return await handlers.handle_tool_call(name, {**arguments, "confirmation_token": preview["confirmation_token"],
                                                  "confirmation_secret": secret})
