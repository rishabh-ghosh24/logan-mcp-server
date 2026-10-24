"""One-page Monitoring requests with pinned scope, deadlines and no SDK retries."""
from collections.abc import Mapping
import oci
from ..budget_tracker import BudgetTracker
from ..client import OCILogAnalyticsClient
from .models import MetricScope, QueryPlan, MetricsError
from .runtime import MonitoringRuntime


class MonitoringGateway:
    def __init__(self, client: OCILogAnalyticsClient, runtime: MonitoringRuntime, budget: BudgetTracker):
        self.client, self.runtime, self.budget = client, runtime, budget
        self._retry = oci.retry.NoneRetryStrategy()

    async def discover(self, scope: MetricScope, *, namespace=None, metric_name=None,
                       resource_group=None, dimension_filters=None, limit=50,
                       page_token=None, group_namespaces=False, deadline: float):
        if (type(limit) is not int or not 1 <= limit <= 100 or type(group_namespaces) is not bool
                or (group_namespaces and any((namespace,metric_name,resource_group,dimension_filters)))
                or (dimension_filters is not None and (not isinstance(dimension_filters,Mapping)
                    or any(not isinstance(k,str) or not isinstance(v,str) for k,v in dimension_filters.items())))):
            raise MetricsError('invalid_discovery','Use a limit from 1 to 100 and choose namespace grouping or metric filters.')
        details = oci.monitoring.models.ListMetricsDetails(group_by=['namespace']) if group_namespaces else (
            oci.monitoring.models.ListMetricsDetails(namespace=namespace,name=metric_name,
                resource_group=resource_group,dimension_filters=dict(dimension_filters) if dimension_filters is not None else None))
        timeout = self.client.settings.monitoring.timeout_seconds
        def request():
            sdk = self.client.monitoring_client_for_region(scope.region,timeout_seconds=timeout)
            return sdk.list_metrics(compartment_id=scope.compartment_id,list_metrics_details=details,
                compartment_id_in_subtree=scope.include_subcompartments,limit=limit,page=page_token,retry_strategy=self._retry)
        return await self.runtime.run(request,deadline=deadline,budget=self.budget)

    async def query(self, scope: MetricScope, plan: QueryPlan, query: str, *, deadline: float):
        if query not in plan.queries:
            raise MetricsError('invalid_mql','Execute only an expression from the validated query plan.')
        details = oci.monitoring.models.SummarizeMetricsDataDetails(namespace=plan.namespace,
            resource_group=plan.resource_group,query=query,start_time=plan.start,end_time=plan.end,resolution=plan.resolution)
        timeout = self.client.settings.monitoring.timeout_seconds
        def request():
            sdk = self.client.monitoring_client_for_region(scope.region,timeout_seconds=timeout)
            return sdk.summarize_metrics_data(compartment_id=scope.compartment_id,summarize_metrics_data_details=details,
                compartment_id_in_subtree=scope.include_subcompartments,retry_strategy=self._retry)
        return await self.runtime.run(request,deadline=deadline,budget=self.budget)
