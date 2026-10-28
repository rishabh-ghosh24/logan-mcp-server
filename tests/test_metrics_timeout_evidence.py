"""OCI timeout diagnostics retain safe provider evidence and consume budget."""
import asyncio
import traceback
import oci
import pytest
from oci_logan_mcp.budget_tracker import BudgetTracker, BudgetLimits
from oci_logan_mcp.metrics.models import MetricsError
from oci_logan_mcp.metrics.runtime import MonitoringRuntime


@pytest.mark.asyncio
@pytest.mark.parametrize('status,service_code',[(408,'RequestTimeout'),(504,'GatewayTimeout')])
async def test_oci_timeout_preserves_safe_evidence_without_refunding(status,service_code):
    budget=BudgetTracker('metrics',BudgetLimits())
    calls=[]
    def sdk():
        calls.append(1)
        raise oci.exceptions.ServiceError(status,service_code,{'opc-request-id':'request-73'},'private-marker')
    with pytest.raises(MetricsError) as caught:
        await MonitoringRuntime().run(sdk,deadline=asyncio.get_running_loop().time()+3,budget=budget)
    assert caught.value.code=='monitoring_timeout'
    assert caught.value.status==status and caught.value.request_id=='request-73'
    assert caught.value.details['service_code']==service_code
    assert 'private-marker' not in ''.join(traceback.format_exception(caught.value))
    assert calls==[1] and budget.snapshot().queries==1
