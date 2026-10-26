"""Reject empty scope before changing the session or saved preference."""

from types import SimpleNamespace

import pytest

from oci_logan_mcp.cache import CacheManager
from oci_logan_mcp.config import Settings
from oci_logan_mcp.handlers import MCPHandlers
from oci_logan_mcp.preferences import PreferenceStore


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,key,valid", [
    ("_set_compartment", "compartment_id", "ocid1.compartment.test"),
    ("_set_namespace", "namespace", "qualification"),
])
@pytest.mark.parametrize("invalid", ["", "   "])
async def test_blank_scope_preserves_previous_context_cache_and_preference(tmp_path, tool, key, valid, invalid):
    handlers = object.__new__(MCPHandlers)
    handlers.settings = Settings()
    handlers.oci_client = SimpleNamespace(compartment_id="original-compartment", namespace="original-namespace")
    handlers.cache = CacheManager()
    handlers.preference_store = PreferenceStore(tmp_path / "users" / "qualification")
    handlers.user_store = SimpleNamespace(user_id="qualification")

    await getattr(handlers, tool)({key: valid})
    assert getattr(handlers.oci_client, key) == valid
    handlers.cache.set("keep-until-valid-scope-change", {"value": 42})
    original_preference = handlers.preference_store.get("default_compartment_id")

    with pytest.raises(ValueError, match="must be non-empty"):
        await getattr(handlers, tool)({key: invalid})

    assert getattr(handlers.oci_client, key) == valid
    assert handlers.cache.get("keep-until-valid-scope-change") == {"value": 42}
    assert handlers.preference_store.get("default_compartment_id") == original_preference
