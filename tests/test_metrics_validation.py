"""Bounded MQL preserves literals and rejects unsafe requests before execution."""
from dataclasses import replace, FrozenInstanceError
from datetime import datetime, timedelta, timezone
import pytest
from oci_logan_mcp.config import Settings
from oci_logan_mcp.metrics.models import MetricsError

NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)


def raw(**overrides):
    from oci_logan_mcp.metrics.validation import build_raw_plan
    return build_raw_plan({'namespace':'oci_computeagent', 'query':'CpuUtilization[5m].mean()',
        'time_start':'2026-10-24T11:00:00Z', 'time_end':'2026-10-24T12:00:00Z', **overrides})


def check(plan, *, points=4000, now=NOW):
    from oci_logan_mcp.metrics.models import ResponseLimits
    from oci_logan_mcp.metrics.validation import validate_plan
    return validate_plan(plan, ResponseLimits(max_datapoints=points), now=now, max_window_hours=24)


@pytest.mark.parametrize('query,expected', [
    ('Cpu[5m].mean()', '5m'), ('Cpu[10m].mean()', '5m'),
    ('(Cpu[1m].mean() + Mem[5m].max()).abs()', '1m'), ('Cpu[1h].mean()', '1h'),
    ('Cpu[1d].mean()', '1d'),
])
def test_raw_resolution_is_derived_without_changing_mql(query, expected):
    plan = raw(query=query)
    assert plan.resolution == expected and plan.resolution_source == 'query_interval'
    assert plan.queries == (query,) and plan.namespace == 'oci_computeagent'
    assert check(plan) is None
    with pytest.raises(FrozenInstanceError):
        plan.resolution = 'changed'


def test_explicit_resolution_and_absolute_window_take_precedence():
    plan = raw(resolution='1m', time_start='2026-10-24T16:00:00+05:30', time_range='bad-preset')
    assert plan.start == datetime(2026,10,24,10,30,tzinfo=timezone.utc)
    assert plan.end == NOW and plan.start.utcoffset() == timedelta(0)
    assert plan.resolution == '1m' and plan.resolution_source == 'explicit'
    assert check(plan) is None


@pytest.mark.parametrize('missing', ['time_start','time_end'])
def test_incomplete_absolute_range_is_not_filled_in(missing):
    with pytest.raises(MetricsError) as error:
        raw(**{missing:None})
    assert error.value.code == 'invalid_time_range'


def test_relative_window_is_valid_against_a_later_now():
    from oci_logan_mcp.metrics.validation import build_raw_plan, validate_plan
    from oci_logan_mcp.metrics.models import ResponseLimits
    plan = build_raw_plan({'namespace':'custom','query':'Metric[1m].mean()','time_range':'last_1_hour'})
    assert 3599 < (plan.end-plan.start).total_seconds() < 3601
    assert validate_plan(plan, ResponseLimits(), now=datetime.now(timezone.utc), max_window_hours=24) is None


@pytest.mark.parametrize('query,seconds', [
    ('Cpu[5m]{name="fake [1d]  two spaces"}.mean() | rate()', [300]),
    (r'Cpu[5m]{name="a\" [1m]"}.mean() + Mem[1h].max()', [300,3600]),
    ("Cpu[5m]{name='it''s [1m]'}.mean()", [300]),
    ('Cpu[5m]{name=~"foo|bar"}.mean() | absent()', [300]),
    ('(Cpu[10m].mean() + (Mem[1m].max())).abs()', [600,60]),
])
def test_interval_extraction_ignores_quoted_text_and_keeps_every_operand(query, seconds):
    from oci_logan_mcp.metrics.validation import parse_mql_intervals
    assert [x.total_seconds() for x in parse_mql_intervals(query)] == seconds
    assert raw(query=query).queries == (query,)


@pytest.mark.parametrize('query', ['', 'Metric.mean()', 'Metric[0m].mean()', 'Metric[61m].mean()',
    'Metric[25h].mean()', 'Metric[2d].mean()', 'Metric[1s].mean()', 'Metric[x].mean()',
    'Metric[1m]{name="unterminated}.mean()', 'Metric[1m].mean()\x00', 'x'*4097,
    'Metric[5m].mean() + Other.mean()'])
def test_unsupported_or_undetermined_operands_fail_safely(query):
    with pytest.raises(MetricsError) as error:
        raw(query=query)
    assert error.value.code in ('invalid_mql','invalid_interval')
    assert query not in str(error.value) if query else True


@pytest.mark.parametrize('resolution,days', [('1m',7),('5m',30),('1h',90),('1d',90)])
def test_retention_is_from_now_at_each_boundary(resolution, days):
    plan = raw(query='Cpu[1d].mean()', resolution=resolution)
    start = NOW - timedelta(days=days)
    assert check(replace(plan,start=start,end=start+timedelta(hours=1))) is None
    with pytest.raises(MetricsError) as error:
        check(replace(plan,start=start-timedelta(microseconds=1),end=start+timedelta(hours=1)))
    assert error.value.code == 'retention_exceeded'


@pytest.mark.parametrize('changes,code', [
    ({'start':NOW-timedelta(hours=25)},'window_exceeded'),
    ({'end':NOW+timedelta(microseconds=1)},'invalid_time_range'),
    ({'start':NOW},'invalid_time_range'),
    ({'resolution':'10m'},'invalid_resolution'),
    ({'resolution':'1h'},'resolution_exceeds_interval'),
    ({'queries':('Cpu[1m].mean()','Mem[5m].max()'),'resolution':'5m'},'resolution_exceeds_interval'),
])
def test_window_and_resolution_rules_cover_all_operands(changes, code):
    with pytest.raises(MetricsError) as error:
        check(replace(raw(), **changes))
    assert error.value.code == code


def test_dense_day_rejects_before_dispatch():
    plan = raw(query='Cpu[1m].mean()',time_start='2026-10-23T12:00:00Z')
    assert check(plan) is None
    with pytest.raises(MetricsError) as error:
        check(plan,points=1000)
    assert error.value.code == 'point_limit_exceeded'
    assert error.value.details['point_bound'] == 1441
    assert error.value.details['suggested_resolution'] == '5m'
    assert error.value.details['required_min_interval'] == '5m'
    assert check(plan,points=1441) is None
    with pytest.raises(MetricsError):
        check(plan,points=1440)
    assert check(replace(plan,queries=('Cpu[5m].mean()',),resolution='5m'),points=289) is None
    with pytest.raises(MetricsError) as five:
        check(replace(plan,queries=('Cpu[5m].mean()',),resolution='5m'),points=288)
    assert five.value.details['point_bound'] == 289


def test_point_guidance_considers_retention_as_well_as_density():
    plan = replace(raw(query='Cpu[1d].mean()',resolution='5m'),start=NOW-timedelta(days=29),end=NOW-timedelta(days=28))
    with pytest.raises(MetricsError) as error:
        check(plan,points=10)
    assert error.value.details['suggested_resolution'] == '1d'


@pytest.mark.parametrize('arguments,field,expected', [
    ({},'max_datapoints',4000), ({'max_datapoints':10000},'max_datapoints',10000),
    ({'max_series':100},'max_series',100), ({},'max_response_bytes',262144),
])
def test_limits_use_config_and_allow_explicit_counts_up_to_hard_max(arguments, field, expected):
    from oci_logan_mcp.metrics.validation import limits_from_args
    limits = limits_from_args(arguments, Settings().monitoring)
    assert getattr(limits,field) == expected


@pytest.mark.parametrize('arguments', [{'max_series':True},{'max_series':0},{'max_series':101},
    {'max_datapoints':10001},{'max_datapoints':1.5},{'max_response_bytes':1000000}])
def test_invalid_count_or_caller_controlled_bytes_fails(arguments):
    from oci_logan_mcp.metrics.validation import limits_from_args
    with pytest.raises(MetricsError) as error:
        limits_from_args(arguments,Settings().monitoring)
    assert error.value.code == 'invalid_limits'
