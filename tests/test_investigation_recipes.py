from oci_logan_mcp.investigation_recipes import select_recipe


def test_exact_recipe_match_wins_over_family_fallback():
    recipe = select_recipe("OCI VCN Flow Unified Schema Logs")

    assert recipe is not None
    assert recipe.recipe_id == "oci_vcn_flow_unified"


def test_family_recipe_selected_for_kubernetes_source():
    recipe = select_recipe("Kubernetes Container Generic Logs")

    assert recipe is not None
    assert recipe.recipe_id == "kubernetes_workload"


def test_unknown_source_has_no_recipe():
    assert select_recipe("Custom App Logs") is None
