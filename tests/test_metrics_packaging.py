"""Inspect real wheels and exercise their static help outside the checkout."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest
import yaml

ROOT=Path(__file__).resolve().parents[1]


def load_validator():
    spec=importlib.util.spec_from_file_location('metrics_validator',ROOT/'scripts/validate_metrics_live.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def metrics_wheel(tmp_path_factory):
    supplied=os.environ.get('LOGAN_METRICS_TEST_WHEEL')
    if supplied:
        path=Path(supplied)
        assert path.is_file()
        return path
    directory=tmp_path_factory.mktemp('metrics-wheel')
    built=subprocess.run([sys.executable,'-m','pip','wheel','--no-deps','--no-build-isolation',
        '--disable-pip-version-check','--wheel-dir',str(directory),str(ROOT)],capture_output=True,text=True,timeout=90)
    assert built.returncode==0,built.stdout+built.stderr
    [wheel]=directory.glob('oci_logan_mcp-*.whl')
    return wheel


def test_wheel_contains_verified_monitoring_data_without_duplicate_entries(metrics_wheel):
    with zipfile.ZipFile(metrics_wheel) as archive:
        names=archive.namelist()
        assert len(names)==len(set(names))
        for path in ('metric_catalog.yaml','metric_catalog_manifest.json','monitoring_templates.yaml','monitoring_syntax.md'):
            assert archive.read('oci_logan_mcp/data/'+path).strip()
        assert archive.read('oci_logan_mcp/__main__.py').strip()
        catalog=yaml.safe_load(archive.read('oci_logan_mcp/data/metric_catalog.yaml'))
        manifest=json.loads(archive.read('oci_logan_mcp/data/metric_catalog_manifest.json'))
        reviewed={row['metric_key']:row for row in manifest['candidates'] if row['status']!='withheld'}
        assert len(reviewed)==23 and len(manifest['candidates'])==81
        templates=yaml.safe_load(archive.read('oci_logan_mcp/data/monitoring_templates.yaml'))
        assert templates['query_language']=='mql' and templates['templates']
        for entry in catalog['definitions']:
            assert entry['metric_key'] in reviewed
            for metric in reviewed[entry['metric_key']]['verification']['metrics'].values():
                assert metric['source_urls'] and all(url.startswith('https://docs.oracle.com/') for url in metric['source_urls'])
        for template in templates['templates']:
            assert template['source_urls'] and template['query_language']=='mql'
            if template['tool']=='query_catalog_metric':
                assert template['catalog_key'] in reviewed and template['catalog_version']==catalog['version']


def test_installed_wheel_loads_monitoring_help_outside_checkout(metrics_wheel,tmp_path):
    site=tmp_path/'installed'
    installed=subprocess.run([sys.executable,'-m','pip','install','--no-deps','--no-index','--target',str(site),str(metrics_wheel)],
        capture_output=True,text=True,timeout=60)
    assert installed.returncode==0,installed.stdout+installed.stderr
    code=('import sys;sys.path.insert(0,'+repr(str(site))+');'
        'import oci_logan_mcp;assert oci_logan_mcp.__file__.startswith('+repr(str(site))+');'
        'from oci_logan_mcp.metrics.resources import read_monitoring_resource;'
        'from oci_logan_mcp.metrics.catalog import MetricCatalog;'
        'assert "MQL" in read_monitoring_resource("monitoring://syntax-guide");'
        'assert read_monitoring_resource("monitoring://query-templates")["query_language"]=="mql";'
        'assert MetricCatalog.from_package().require("cpu").unit=="percent";print("wheel-help-loaded")')
    result=subprocess.run([sys.executable,'-c',code],cwd=tmp_path,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    assert result.stdout.strip()=='wheel-help-loaded'


@pytest.mark.parametrize('damage',['missing_source','withheld_key','wrong_language'])
def test_validator_rejects_unverified_packaged_starters(monkeypatch,damage):
    from oci_logan_mcp.metrics.resources import read_monitoring_resource
    module=load_validator()
    data=deepcopy(read_monitoring_resource('monitoring://query-templates'))
    if damage=='missing_source':
        data['templates'][0]['source_urls']=[]
    elif damage=='withheld_key':
        data['templates'][0]['arguments']['metric_key']='unreviewed_key'
        data['templates'][0]['catalog_key']='unreviewed_key'
    else:
        data['templates'][0]['query_language']='logan'
    monkeypatch.setattr(module,'read_monitoring_resource',lambda uri:data)
    with pytest.raises(ValueError):
        module.packaged_templates()
