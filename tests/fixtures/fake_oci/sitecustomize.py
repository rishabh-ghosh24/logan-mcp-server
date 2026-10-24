"""Install a fake OCI backend only in explicitly configured smoke subprocesses."""

import os
from pathlib import Path
import time
from types import SimpleNamespace

import oci
from oci.response import Response


def _config_from_file(*args, **kwargs):
    return {"tenancy": "ocid1.tenancy.oc1..fake", "region": "us-phoenix-1",
            "user": "u", "fingerprint": "f", "key_file": "/dev/null"}


class _Signer:
    def __init__(self, **kwargs):
        pass


class _LogAnalyticsClient:
    def __init__(self, config, signer, **kwargs):
        pass

    def query(self, namespace_name, query_details, limit, page=None):
        if "slow" in query_details.query_string:
            barrier = Path(os.environ["LOGAN_FAKE_OCI_BARRIER_DIR"])
            marker = barrier / "starting"
            marker.write_text(namespace_name)
            os.replace(marker, barrier / "started")
            deadline = time.monotonic() + 10
            while not (barrier / "release").exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("Fake OCI barrier was not released")
                time.sleep(0.02)
        columns = [
            SimpleNamespace(display_name="count", internal_name="func1_count", value_type="LONG"),
            SimpleNamespace(display_name="namespace", internal_name="ns", value_type="STRING"),
        ]
        return Response(200, {}, SimpleNamespace(
            columns=columns, items=[{"count": 42, "namespace": namespace_name}],
            total_count=1, is_partial_result=False,
        ), None)

    def _empty_collection(self, *args, **kwargs):
        return Response(200, {}, SimpleNamespace(items=[]), None)

    list_sources = _empty_collection
    list_fields = _empty_collection
    list_log_analytics_entities = _empty_collection
    list_parsers = _empty_collection
    list_labels = _empty_collection


class _IdentityClient:
    def __init__(self, config, signer, **kwargs):
        pass

    def list_compartments(self, *args, **kwargs):
        return Response(200, {}, [], None)


def _unexpected_real_call(*args, **kwargs):
    raise AssertionError("The fake OCI subprocess must not issue real OCI requests")


oci.config.from_file = _config_from_file
oci.signer.Signer = _Signer
oci.log_analytics.LogAnalyticsClient = _LogAnalyticsClient
oci.identity.IdentityClient = _IdentityClient
oci.base_client.BaseClient.call_api = _unexpected_real_call

from metrics_backend import install as _install_metrics_backend
_install_metrics_backend()
