"""Scope resolution uses authenticated context and never expands a request."""
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import pytest
from oci_logan_mcp.config import Settings
from oci_logan_mcp.metrics.models import MetricsError


def resolve(arguments=None, *, shared=None, configured=True, config=None, signer=None):
    from oci_logan_mcp.metrics.scope import resolve_metric_scope
    settings = Settings()
    if configured:
        settings.log_analytics.default_compartment_id = 'logs'
        settings.monitoring.default_compartment_id = 'monitoring'
        settings.monitoring.region = 'us-phoenix-1'
    return resolve_metric_scope(arguments or {}, settings, shared_compartment_id=shared,
        oci_config=config if config is not None else {'region': 'us-ashburn-1', 'tenancy': 'ocid1.tenancy.root'},
        signer=signer)


@pytest.mark.parametrize('arguments,shared,expected', [
    ({'compartment_id': 'explicit'}, 'remembered', 'explicit'),
    ({}, 'remembered', 'remembered'), ({}, None, 'monitoring'),
])
def test_compartment_precedence(arguments, shared, expected):
    scope = resolve(arguments, shared=shared)
    assert scope.compartment_id == expected
    assert scope.region == 'us-phoenix-1'
    assert scope.tenancy_id == 'ocid1.tenancy.root'
    assert scope.include_subcompartments is False
    with pytest.raises(FrozenInstanceError):
        scope.region = 'changed'


def test_logs_default_is_last_and_not_a_remembered_choice():
    from oci_logan_mcp.metrics.scope import resolve_metric_scope
    settings = Settings()
    settings.log_analytics.default_compartment_id = 'logs'
    scope = resolve_metric_scope({}, settings, shared_compartment_id=None,
        oci_config={'region': 'us-ashburn-1'}, signer=None)
    assert scope.compartment_id == 'logs' and scope.region == 'us-ashburn-1'
    assert scope.tenancy_id is None


@pytest.mark.parametrize('arguments,config,signer,region', [
    ({'region': 'eu-frankfurt-1'}, {}, None, 'eu-frankfurt-1'),
    ({}, {'region': 'us-ashburn-1'}, None, 'us-ashburn-1'),
    ({}, {}, SimpleNamespace(region='ap-mumbai-1', tenancy_id='ocid1.tenancy.signer'), 'ap-mumbai-1'),
])
def test_region_falls_back_only_to_authenticated_sources(arguments, config, signer, region):
    scope = resolve({'compartment_id': 'chosen', **arguments}, configured=False, config=config, signer=signer)
    assert scope.region == region and scope.compartment_id == 'chosen'


@pytest.mark.parametrize('arguments,config', [({}, {}), ({'compartment_id':'chosen'}, {}),
    ({'region':'us-ashburn-1'}, {}), ({'compartment_id':'chosen','region':'bad region'}, {}),
    ({'compartment_id':'chosen','include_subcompartments':'true'}, {'region':'us-ashburn-1'})])
def test_missing_or_malformed_scope_is_actionable(arguments, config):
    with pytest.raises(MetricsError, match='compartment|region|boolean'):
        resolve(arguments, configured=False, config=config)


def test_known_subtree_rejects_compartment_but_keeps_distinct_explicit_root():
    with pytest.raises(MetricsError) as error:
        resolve({'compartment_id':'ocid1.compartment.child','include_subcompartments':True})
    assert error.value.code == 'invalid_subtree_scope'
    for root in ('ocid1.tenancy.root', 'ocid1.tenancy.other'):
        scope = resolve({'compartment_id':root,'include_subcompartments':True})
        assert scope.compartment_id == root and scope.include_subcompartments is True


def test_unknown_tenancy_keeps_original_subtree_request():
    scope = resolve({'compartment_id':'ocid1.compartment.child','include_subcompartments':True}, config={})
    assert scope.tenancy_id is None
    assert scope.compartment_id == 'ocid1.compartment.child' and scope.include_subcompartments is True
