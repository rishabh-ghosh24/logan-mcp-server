"""The live validator must make real stdio and independent SDK calls to pass."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from tests.test_protocol_smoke import _params,GOOD_CONFIG,FAKE_OCI

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/validate_metrics_live.py'


def run_validator(tmp_path,*,mode='success',bad_starter=False):
    params=_params(tmp_path,GOOD_CONFIG,True)
    metrics=tmp_path/'metrics-requests';metrics.mkdir()
    env=params.env
    env['LOGAN_FAKE_METRICS_DIR']=str(metrics)
    env['LOGAN_FAKE_METRICS_MODE']=mode
    config=tmp_path/'input.yaml'
    config.write_text(GOOD_CONFIG+'monitoring:\n  default_compartment_id: chosen\n  region: us-phoenix-1\n')
    args=['--config',str(config),'--python',sys.executable,'--compartment-id','chosen',
        '--region','us-phoenix-1','--output-dir',str(tmp_path/'evidence')]
    command=[sys.executable,str(SCRIPT),*args]
    if bad_starter:
        injection=tmp_path/'injection';injection.mkdir()
        (injection/'sitecustomize.py').write_text('import runpy\nrunpy.run_path('+repr(str(FAKE_OCI/'sitecustomize.py'))+')\n'
            'import oci\noriginal=oci.monitoring.MonitoringClient.summarize_metrics_data\n'
            'def reject(self,*args,**kwargs):\n'
            ' response=original(self,*args,**kwargs)\n'
            ' if "BADSTARTER" in kwargs["summarize_metrics_data_details"].query:\n'
            '  raise oci.exceptions.ServiceError(400,"InvalidParameter",{},"PRIVATE-INVALID-STARTER")\n'
            ' return response\noci.monitoring.MonitoringClient.summarize_metrics_data=reject\n')
        env['PYTHONPATH']=str(injection)+':'+env['PYTHONPATH']
        code=('import importlib.util;spec=importlib.util.spec_from_file_location("validator",'+repr(str(SCRIPT))+');'
            'module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);'
            'module.packaged_templates=lambda:[{"name":"bad-starter","query_language":"mql",'
            '"namespace":"oci_computeagent","tool":"query_metrics","arguments":{"namespace":"oci_computeagent",'
            '"query":"BADSTARTER[5m].mean()"},"required_substitutions":[],"source_urls":["https://docs.oracle.com/example"]}];'
            'raise SystemExit(module.main('+repr(args)+'))')
        command=[sys.executable,'-c',code]
    completed=subprocess.run(command,cwd=tmp_path,env=env,text=True,capture_output=True,timeout=90)
    report=tmp_path/'evidence'/'review-summary.json'
    assert report.is_file(),completed.stdout+completed.stderr
    return completed,json.loads(report.read_text()),metrics,config


def test_validator_proves_positive_data_fixed_windows_and_independent_comparison(tmp_path):
    completed,report,metrics,config=run_validator(tmp_path)
    assert completed.returncode==0,completed.stdout+completed.stderr
    assert report['status']=='passed' and report['positive_query_count']>=1
    assert report['direct_comparison']=='passed' and report['starter_results']
    private=json.loads((tmp_path/'evidence'/'private-results.json').read_text())
    assert private['tool_sequence'][:4]==['list_metric_namespaces','list_metrics','search_metric_catalog','query_metrics']
    assert private['sdk_comparison']['point_count']==2 and private['sdk_comparison']['equal'] is True
    calls=[json.loads(line) for line in (metrics/'requests.jsonl').read_text().splitlines()]
    assert sum(row['operation']=='query' for row in calls)>=4
    assert config.read_text().startswith('oci:') and 'chosen' not in json.dumps(report)
    assert (tmp_path/'evidence'/'private-results.json').stat().st_mode & 0o777==0o600


def test_validator_reports_no_data_as_pending_not_live_pass(tmp_path):
    completed,report,metrics,config=run_validator(tmp_path,mode='no_data')
    assert completed.returncode==2 and report['status']=='pending'
    assert report['positive_query_count']==0 and report['direct_comparison']!='passed'
    assert (metrics/'requests.jsonl').is_file()


def test_validator_bad_starter_is_a_failure_with_evidence(tmp_path):
    completed,report,metrics,config=run_validator(tmp_path,bad_starter=True)
    assert completed.returncode==1 and report['status']=='failed'
    assert any(row['name']=='bad-starter' and row['status']=='failed' for row in report['starter_results'])
    assert 'PRIVATE-INVALID-STARTER' not in json.dumps(report)+completed.stdout+completed.stderr
    assert 'BADSTARTER' in (metrics/'requests.jsonl').read_text()
