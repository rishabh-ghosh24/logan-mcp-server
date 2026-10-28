"""Bound SDK admission and keep dispatched requests charged after caller timeout."""
import asyncio
import re
import threading
import time
from typing import Any, Callable
import oci
from oci._vendor.requests.exceptions import Timeout as SdkTimeout
from ..budget_tracker import BudgetExceededError, BudgetTracker
from ..rate_limiter import RateLimiter
from .models import MetricsError


def _is_timeout(error: Exception) -> bool:
    return (isinstance(error,(TimeoutError,SdkTimeout))
            or getattr(error,'status',None) in (408,504)
            or any(isinstance(arg,SdkTimeout) for arg in error.args))


def _safe_error(error: Exception) -> MetricsError:
    status, code, request_id = None, 'monitoring_unavailable', None
    if isinstance(error,oci.exceptions.ServiceError):
        code = error.code if isinstance(error.code,str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',error.code) else 'monitoring_service_error'
        request_id = error.request_id
        if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_./:-]{1,256}',request_id):
            request_id = None
        status = error.status if type(error.status) is int else None
    if _is_timeout(error):
        return MetricsError('monitoring_timeout','Monitoring exceeded its deadline. Narrow the request before trying again.',
                            status=status, request_id=request_id,
                            details={'service_code':code} if isinstance(error,oci.exceptions.ServiceError) else None)
    if isinstance(error,oci.exceptions.ServiceError):
        return MetricsError(code,'OCI Monitoring rejected the request. Check scope, IAM permissions and query syntax.',
                            status=status, request_id=request_id)
    return MetricsError('monitoring_unavailable','Monitoring is unavailable. Check OCI setup and use test_connection.')


class MonitoringRuntime:
    """One process-wide owner of the two worker slots and five-per-second gate."""
    def __init__(self):
        self._slots = asyncio.Semaphore(2)
        self._rate_limiter = RateLimiter(requests_per_second=5)
        self._workers: set[asyncio.Task] = set()

    async def run(self, call: Callable[[], Any], *, deadline: float, budget: BudgetTracker) -> Any:
        acquired = False
        worker = None
        state_lock = threading.Lock()
        state = {'abandoned':False, 'entered':False, 'refunded':False}

        def refund():
            if not state['refunded']:
                budget.release(bytes=0,cost_usd=0)
                state['refunded'] = True

        def invoke():
            with state_lock:
                if state['abandoned'] or time.monotonic() >= deadline:
                    refund()
                    raise MetricsError('monitoring_timeout','The deadline expired before Monitoring dispatch.')
                state['entered'] = True
            try:
                return call()
            except Exception as error:
                with state_lock:
                    if not state['abandoned'] and time.monotonic() < deadline and not _is_timeout(error):
                        refund()
                raise _safe_error(error) from None

        def finished(task):
            self._workers.discard(task)
            self._slots.release()
            # Observe late failures without logging provider text or changing a past answer.
            if not task.cancelled():
                task.exception()

        try:
            async with asyncio.timeout_at(deadline):
                await self._slots.acquire()
                acquired = True
                await self._rate_limiter.acquire()
            if time.monotonic() >= deadline:
                raise TimeoutError()
            try:
                budget.reserve(estimated_bytes=0,estimated_cost_usd=0,override=False)
            except BudgetExceededError:
                raise MetricsError('budget_exceeded','The session query budget is exhausted. Start a new session.') from None
            worker = asyncio.create_task(asyncio.to_thread(invoke))
            self._workers.add(worker)
            worker.add_done_callback(finished)
            return await asyncio.wait_for(asyncio.shield(worker),timeout=max(0,deadline-time.monotonic()))
        except TimeoutError:
            with state_lock:
                state['abandoned'] = True
            raise MetricsError('monitoring_timeout','Monitoring exceeded its deadline. Narrow the request before trying again.') from None
        except asyncio.CancelledError:
            with state_lock:
                state['abandoned'] = True
            raise
        finally:
            if acquired and worker is None:
                self._slots.release()
