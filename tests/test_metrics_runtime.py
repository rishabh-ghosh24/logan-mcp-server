"""Thread barriers prove deadlines, admission and query-budget settlement."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import traceback
import oci
import pytest
from oci_logan_mcp.budget_tracker import BudgetTracker, BudgetLimits
from oci_logan_mcp.metrics.models import MetricsError


class CountingBudget(BudgetTracker):
    def __init__(self, maximum=100):
        super().__init__('metrics',BudgetLimits(max_queries_per_session=maximum))
        self.releases=0
    def release(self, **kwargs):
        self.releases+=1
        return super().release(**kwargs)


def runtime():
    from oci_logan_mcp.metrics.runtime import MonitoringRuntime
    return MonitoringRuntime()


def deadline(seconds=3):
    return asyncio.get_running_loop().time()+seconds


async def entered(event):
    async with asyncio.timeout(3):
        while not event.is_set():
            await asyncio.sleep(.001)


async def drained(engine):
    await asyncio.gather(*engine._workers,return_exceptions=True)


@pytest.mark.asyncio
async def test_success_preserves_result_and_consumes_exactly_one_query():
    engine,budget=runtime(),CountingBudget()
    main=threading.get_ident()
    result=await engine.run(lambda:(threading.get_ident(),73),deadline=deadline(),budget=budget)
    assert result[1]==73 and result[0]!=main
    assert budget.snapshot().queries==1 and budget.snapshot().bytes==0 and budget.snapshot().cost_usd==0
    assert budget.releases==0


@pytest.mark.asyncio
@pytest.mark.parametrize('failure,code,status', [
    (oci.exceptions.ServiceError(400,'InvalidParameter',{'opc-request-id':'request-73'},'private-marker'),'InvalidParameter',400),
    (oci.exceptions.ServiceError(429,'TooManyRequests',{'opc-request-id':'request-73'},'private-marker'),'TooManyRequests',429),
    (RuntimeError('private-marker'),'monitoring_unavailable',None),
])
async def test_definite_failure_is_safe_released_once_and_never_retried(failure,code,status):
    engine,budget=runtime(),CountingBudget()
    calls=[]
    def invoke():
        calls.append(1)
        raise failure
    with pytest.raises(MetricsError) as caught:
        await engine.run(invoke,deadline=deadline(),budget=budget)
    assert caught.value.code==code and caught.value.status==status
    if status:
        assert caught.value.request_id=='request-73'
    assert 'private-marker' not in ''.join(traceback.format_exception(caught.value))
    assert len(calls)==1 and budget.snapshot().queries==0 and budget.releases==1


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [TimeoutError('private-marker'), oci.exceptions.ConnectTimeout('private-marker'),
    oci.exceptions.ServiceError(504,'GatewayTimeout',{},'private-marker')])
async def test_sdk_timeout_keeps_the_reservation(failure):
    engine,budget=runtime(),CountingBudget()
    calls=[]
    def invoke():
        calls.append(1)
        raise failure
    with pytest.raises(MetricsError) as caught:
        await engine.run(invoke,deadline=deadline(),budget=budget)
    assert caught.value.code=='monitoring_timeout' and 'private-marker' not in str(caught.value)
    assert calls==[1] and budget.snapshot().queries==1 and budget.releases==0


@pytest.mark.asyncio
async def test_timed_out_worker_keeps_slot_until_actual_completion():
    engine,budget=runtime(),CountingBudget()
    first_in,second_in=threading.Event(),threading.Event()
    first_out,second_out=threading.Event(),threading.Event()
    third_in=threading.Event()
    def held(start,release,value):
        start.set()
        assert release.wait(4)
        return value
    first=asyncio.create_task(engine.run(lambda:held(first_in,first_out,11),deadline=deadline(.65),budget=budget))
    second=asyncio.create_task(engine.run(lambda:held(second_in,second_out,22),deadline=deadline(),budget=budget))
    try:
        await entered(first_in)
        await entered(second_in)
        with pytest.raises(MetricsError) as caught:
            await first
        assert caught.value.code=='monitoring_timeout'
        third=asyncio.create_task(engine.run(lambda:(third_in.set(),33)[1],deadline=deadline(),budget=budget))
        await asyncio.sleep(.03)
        assert not third.done() and not third_in.is_set() and budget.snapshot().queries==2
        first_out.set()
        assert await third==33 and third_in.is_set()
        second_out.set()
        assert await second==22
        await drained(engine)
        assert budget.snapshot().queries==3 and budget.releases==0
    finally:
        first_out.set(); second_out.set()
        await asyncio.gather(first,second,return_exceptions=True)
        await drained(engine)


@pytest.mark.asyncio
async def test_queue_deadline_and_queued_cancellation_consume_no_query():
    engine,budget=runtime(),CountingBudget()
    events=[threading.Event(),threading.Event()]
    release=threading.Event()
    def held(index):
        events[index].set()
        assert release.wait(4)
        return index+1
    workers=[asyncio.create_task(engine.run(lambda i=i:held(i),deadline=deadline(),budget=budget)) for i in range(2)]
    forbidden=[]
    try:
        await entered(events[0]); await entered(events[1])
        with pytest.raises(MetricsError) as caught:
            await engine.run(lambda:forbidden.append(1),deadline=deadline(.03),budget=budget)
        assert caught.value.code=='monitoring_timeout'
        queued=asyncio.create_task(engine.run(lambda:forbidden.append(1),deadline=deadline(),budget=budget))
        await asyncio.sleep(.01)
        assert not queued.done()
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert budget.snapshot().queries==2 and not forbidden
    finally:
        release.set()
        assert await asyncio.gather(*workers)==[1,2]
        await drained(engine)


@pytest.mark.asyncio
async def test_rate_wait_obeys_same_deadline_without_reserving():
    engine,budget=runtime(),CountingBudget()
    assert await engine.run(lambda:7,deadline=deadline(),budget=budget)==7
    calls=[]
    with pytest.raises(MetricsError) as caught:
        await engine.run(lambda:calls.append(1),deadline=deadline(.02),budget=budget)
    assert caught.value.code=='monitoring_timeout' and calls==[]
    assert budget.snapshot().queries==1 and budget.releases==0
    assert await engine.run(lambda:9,deadline=deadline(),budget=budget)==9


@pytest.mark.asyncio
async def test_cancellation_after_sdk_entry_stays_consumed_after_late_failure():
    engine,budget=runtime(),CountingBudget()
    start,release=threading.Event(),threading.Event()
    def call():
        start.set()
        assert release.wait(3)
        raise oci.exceptions.ServiceError(400,'InvalidParameter',{},'private-marker')
    request=asyncio.create_task(engine.run(call,deadline=deadline(),budget=budget))
    try:
        await entered(start)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert budget.snapshot().queries==1
    finally:
        release.set()
        await drained(engine)
    assert budget.snapshot().queries==1 and budget.releases==0


@pytest.mark.asyncio
async def test_cancelled_reserved_worker_refunds_only_after_confirmed_non_dispatch():
    engine,budget=runtime(),CountingBudget()
    loop=asyncio.get_running_loop()
    pool=ThreadPoolExecutor(max_workers=1)
    old=loop._default_executor
    loop.set_default_executor(pool)
    start,release=threading.Event(),threading.Event()
    blocker=loop.run_in_executor(None,lambda:(start.set(),release.wait(3)))
    calls=[]
    try:
        await entered(start)
        request=asyncio.create_task(engine.run(lambda:calls.append(1),deadline=deadline(),budget=budget))
        async with asyncio.timeout(2):
            while budget.snapshot().queries!=1:
                await asyncio.sleep(.001)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert budget.snapshot().queries==1 and calls==[]
        release.set()
        await blocker
        await drained(engine)
        assert calls==[] and budget.snapshot().queries==0 and budget.releases==1
    finally:
        release.set()
        await blocker
        await drained(engine)
        loop._default_executor=old
        pool.shutdown(wait=True)


@pytest.mark.asyncio
async def test_repeated_dispatched_timeouts_exhaust_budget_without_late_refunds():
    engine,budget=runtime(),CountingBudget(maximum=2)
    for _ in range(2):
        start,release=threading.Event(),threading.Event()
        def call():
            start.set()
            assert release.wait(3)
            return 73
        request=asyncio.create_task(engine.run(call,deadline=deadline(.3),budget=budget))
        try:
            await entered(start)
            with pytest.raises(MetricsError):
                await request
        finally:
            release.set()
            await drained(engine)
    calls=[]
    with pytest.raises(MetricsError) as caught:
        await engine.run(lambda:calls.append(1),deadline=deadline(),budget=budget)
    assert caught.value.code=='budget_exceeded' and 'override' not in str(caught.value)
    assert calls==[] and budget.snapshot().queries==2 and budget.releases==0


@pytest.mark.asyncio
async def test_composite_uses_one_deadline_including_rate_wait():
    engine,budget=runtime(),CountingBudget()
    whole_deadline=deadline(.1)
    assert await engine.run(lambda:31,deadline=whole_deadline,budget=budget)==31
    calls=[]
    with pytest.raises(MetricsError) as caught:
        await engine.run(lambda:calls.append(1),deadline=whole_deadline,budget=budget)
    assert caught.value.code=='monitoring_timeout' and calls==[] and budget.snapshot().queries==1
