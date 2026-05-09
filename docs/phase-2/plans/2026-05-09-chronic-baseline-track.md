# `chronic_baseline` Track Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a peer ranking track to `investigate_incident` that scores log sources by absolute error-like volume in the current window, surfacing high-volume steady-state failures (e.g. OKE kube-apiserver gRPC dial errors) that the existing `pct_change` anomaly track is structurally blind to.

**Architecture:** New `_run_chronic_baseline_track` joins `_run_core_tracks` as a peer of `diff` (concurrent, gated by mode). After both tracks complete, `_merge_chronic_with_anomalous` produces a unified candidate list; `_drill_down_one_source` iterates it unchanged. Outputs flow into `chronic_baseline_sources` (raw) and `anomalous_sources` (merged, with `reasons` tags). Configurable via `settings.chronic_baseline` only — no public `run()` parameter.

**Tech Stack:** Python 3.10+, `asyncio`, `pytest` + `pytest-asyncio`, OCI Logging Analytics query syntax. Existing dependencies only.

**Spec:** `docs/phase-2/specs/2026-05-09-chronic-baseline-track-design.md`.

**Branch:** Implementation lands on `goofy-mestorf-606e96` (worktree branch cut from `feat/combined-investigation-report-persistence`). Do NOT branch off `main`.

---

## File structure

| File | Role | Change shape |
|---|---|---|
| `src/oci_logan_mcp/config.py` | `ChronicBaselineConfig` dataclass; term validator; YAML/dict wiring | Add ~50 lines |
| `src/oci_logan_mcp/investigate.py` | New query composer, parser, merge fn, peer track method, mode flag, `run()` integration, `_finalize` field, summary line | Add ~150 lines, modify `_run_core_tracks`, `run`, `_finalize`, `_templated_summary`, `_InvestigationModeConfig`, `_MODE_CONFIGS` |
| `src/oci_logan_mcp/report_generator.py` | Audit for None-tolerance on `pct_change` / `current_count` / `comparison_count` | Add safety branches only if any reader is non-tolerant |
| `tests/test_config.py` | `ChronicBaselineConfig` defaults, validator, YAML round-trip | Add ~80 lines |
| `tests/test_investigate.py` | Unit tests for new helpers; integration tests for orchestrator behaviour | Add ~400 lines |
| `tests/test_report_generator.py` | None-tolerance assertions for chronic-only entries (only if audit reveals gaps) | Add tests if audit demands |

---

## Task 1: `ChronicBaselineConfig` dataclass with term validation

**Files:**
- Modify: `src/oci_logan_mcp/config.py:128-138` (insert new dataclass after `IngestionHealthConfig`)
- Test: `tests/test_config.py`

The validator must reject anything except lowercase ASCII alpha. Reasoning is in spec §3.2: terms render literally into `like '%<term>%'` with no escaping, so any non-alpha character is a hard error.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
class TestChronicBaselineConfig:
    def test_defaults(self):
        from oci_logan_mcp.config import ChronicBaselineConfig
        c = ChronicBaselineConfig()
        assert c.enabled is True
        assert c.count_threshold == 1000
        assert c.error_like_terms == (
            "error", "fail", "fatal", "critical", "exception", "timeout",
            "reject", "deny", "drop", "nxdomain", "servfail", "refused",
        )

    def test_default_terms_pass_validation(self):
        from oci_logan_mcp.config import ChronicBaselineConfig, _validate_chronic_baseline_terms
        c = ChronicBaselineConfig()
        # Must not raise.
        _validate_chronic_baseline_terms(c.error_like_terms)

    def test_uppercase_term_rejected(self):
        import pytest
        from oci_logan_mcp.config import _validate_chronic_baseline_terms
        with pytest.raises(ValueError, match="lowercase ASCII alpha"):
            _validate_chronic_baseline_terms(("Error",))

    def test_term_with_quote_rejected(self):
        import pytest
        from oci_logan_mcp.config import _validate_chronic_baseline_terms
        with pytest.raises(ValueError, match="lowercase ASCII alpha"):
            _validate_chronic_baseline_terms(("err'or",))

    def test_term_with_double_quote_rejected(self):
        import pytest
        from oci_logan_mcp.config import _validate_chronic_baseline_terms
        with pytest.raises(ValueError, match="lowercase ASCII alpha"):
            _validate_chronic_baseline_terms(('err"or',))

    def test_term_with_percent_wildcard_rejected(self):
        import pytest
        from oci_logan_mcp.config import _validate_chronic_baseline_terms
        with pytest.raises(ValueError, match="lowercase ASCII alpha"):
            _validate_chronic_baseline_terms(("er%or",))

    def test_term_with_underscore_wildcard_rejected(self):
        import pytest
        from oci_logan_mcp.config import _validate_chronic_baseline_terms
        with pytest.raises(ValueError, match="lowercase ASCII alpha"):
            _validate_chronic_baseline_terms(("er_or",))

    def test_term_with_digit_rejected(self):
        import pytest
        from oci_logan_mcp.config import _validate_chronic_baseline_terms
        with pytest.raises(ValueError, match="lowercase ASCII alpha"):
            _validate_chronic_baseline_terms(("err0r",))

    def test_term_with_whitespace_rejected(self):
        import pytest
        from oci_logan_mcp.config import _validate_chronic_baseline_terms
        with pytest.raises(ValueError, match="lowercase ASCII alpha"):
            _validate_chronic_baseline_terms(("er ror",))

    def test_empty_term_rejected(self):
        import pytest
        from oci_logan_mcp.config import _validate_chronic_baseline_terms
        with pytest.raises(ValueError, match="lowercase ASCII alpha"):
            _validate_chronic_baseline_terms(("",))

    def test_no_terms_rejected(self):
        import pytest
        from oci_logan_mcp.config import _validate_chronic_baseline_terms
        with pytest.raises(ValueError, match="at least one"):
            _validate_chronic_baseline_terms(())

    def test_negative_threshold_rejected(self):
        import pytest
        from oci_logan_mcp.config import ChronicBaselineConfig, _validate_chronic_baseline_threshold
        with pytest.raises(ValueError, match="non-negative"):
            _validate_chronic_baseline_threshold(-1)
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_config.py::TestChronicBaselineConfig -v
```

Expected: ImportError (`cannot import name 'ChronicBaselineConfig'`) — all tests in the class error out at collection.

- [ ] **Step 3: Add the dataclass and validators in `src/oci_logan_mcp/config.py`**

Insert after the `IngestionHealthConfig` dataclass (currently ends at line 138):

```python
def _validate_chronic_baseline_terms(terms: Tuple[str, ...]) -> None:
    """Validate chronic-baseline error-like terms.

    Terms render literally into Logan `like '%<term>%'` clauses with no
    escaping, so they must be safe-by-construction: lowercase ASCII alpha
    only, no quotes, no SQL wildcards (`%`, `_`), no whitespace, no digits.
    Defaults are 12 known-safe substrings; this validator is belt-and-braces
    against future tunability.

    Source-name escaping for `focus_sources` is a separate path and uses
    single-quote-doubling — that path does NOT call this validator.
    """
    if not terms:
        raise ValueError(
            "chronic_baseline.error_like_terms must contain at least one term"
        )
    for t in terms:
        if not t or not t.isascii() or not t.islower() or not t.isalpha():
            raise ValueError(
                f"chronic_baseline.error_like_terms entry {t!r} must be "
                f"lowercase ASCII alpha only (no quotes, wildcards, digits, "
                f"or whitespace). See "
                f"docs/phase-2/specs/2026-05-09-chronic-baseline-track-design.md §3.2."
            )


def _validate_chronic_baseline_threshold(threshold: int) -> None:
    if threshold < 0:
        raise ValueError(
            f"chronic_baseline.count_threshold must be non-negative; got {threshold}"
        )


@dataclass
class ChronicBaselineConfig:
    """Chronic-baseline track for investigate_incident — see
    docs/phase-2/specs/2026-05-09-chronic-baseline-track-design.md."""

    enabled: bool = True
    error_like_terms: Tuple[str, ...] = (
        "error", "fail", "fatal", "critical", "exception", "timeout",
        "reject", "deny", "drop", "nxdomain", "servfail", "refused",
    )
    count_threshold: int = 1000   # absolute event count over the investigation window

    def __post_init__(self):
        _validate_chronic_baseline_terms(self.error_like_terms)
        _validate_chronic_baseline_threshold(self.count_threshold)
```

If `Tuple` is not already imported at the top of `config.py`, add it to the existing `from typing import ...` line.

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_config.py::TestChronicBaselineConfig -v
```

Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/config.py tests/test_config.py
git commit -m "Add ChronicBaselineConfig dataclass with term validator"
```

---

## Task 2: Wire `ChronicBaselineConfig` into `Settings`, `to_dict`, and YAML loader

**Files:**
- Modify: `src/oci_logan_mcp/config.py:140-156` (Settings class), `src/oci_logan_mcp/config.py:222-225` (to_dict), `src/oci_logan_mcp/config.py:357-367` (_parse_config)
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py` inside `TestChronicBaselineConfig`:

```python
    def test_settings_includes_chronic_baseline_default(self):
        from oci_logan_mcp.config import Settings, ChronicBaselineConfig
        s = Settings()
        assert isinstance(s.chronic_baseline, ChronicBaselineConfig)
        assert s.chronic_baseline.enabled is True

    def test_to_dict_includes_chronic_baseline(self):
        from oci_logan_mcp.config import Settings
        s = Settings()
        d = s.to_dict()
        assert "chronic_baseline" in d
        assert d["chronic_baseline"]["enabled"] is True
        assert d["chronic_baseline"]["count_threshold"] == 1000
        assert "error" in d["chronic_baseline"]["error_like_terms"]

    def test_load_config_parses_chronic_baseline(self, tmp_path):
        import yaml
        from oci_logan_mcp.config import load_config
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump({
            "chronic_baseline": {
                "enabled": False,
                "error_like_terms": ["error", "fail"],
                "count_threshold": 500,
            }
        }))
        s = load_config(cfg_path)
        assert s.chronic_baseline.enabled is False
        assert s.chronic_baseline.error_like_terms == ("error", "fail")
        assert s.chronic_baseline.count_threshold == 500

    def test_load_config_rejects_invalid_term(self, tmp_path):
        import pytest
        import yaml
        from oci_logan_mcp.config import load_config
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump({
            "chronic_baseline": {"error_like_terms": ["Error"]}
        }))
        with pytest.raises(ValueError, match="lowercase ASCII alpha"):
            load_config(cfg_path)
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_config.py::TestChronicBaselineConfig::test_settings_includes_chronic_baseline_default tests/test_config.py::TestChronicBaselineConfig::test_to_dict_includes_chronic_baseline tests/test_config.py::TestChronicBaselineConfig::test_load_config_parses_chronic_baseline tests/test_config.py::TestChronicBaselineConfig::test_load_config_rejects_invalid_term -v
```

Expected: 4 failed (AttributeError on `s.chronic_baseline`, missing dict key).

- [ ] **Step 3: Wire `chronic_baseline` into `Settings`**

In `src/oci_logan_mcp/config.py`, at the `Settings` dataclass (around line 140), add a field after the `ingestion_health` line:

```python
    chronic_baseline: ChronicBaselineConfig = field(default_factory=ChronicBaselineConfig)
```

In `to_dict` (around line 222, after the `ingestion_health` block), add:

```python
            "chronic_baseline": {
                "enabled": self.chronic_baseline.enabled,
                "error_like_terms": list(self.chronic_baseline.error_like_terms),
                "count_threshold": self.chronic_baseline.count_threshold,
            },
```

In `_parse_config` (around line 357, after the `if ih_data := ...` block), add:

```python
    if cb_data := data.get("chronic_baseline"):
        terms = cb_data.get("error_like_terms")
        settings.chronic_baseline = ChronicBaselineConfig(
            enabled=cb_data.get("enabled", settings.chronic_baseline.enabled),
            error_like_terms=(
                tuple(terms) if terms is not None else settings.chronic_baseline.error_like_terms
            ),
            count_threshold=cb_data.get(
                "count_threshold", settings.chronic_baseline.count_threshold
            ),
        )
```

The `__post_init__` validator on `ChronicBaselineConfig` will raise `ValueError` if YAML supplies invalid terms; `_parse_config` does not need to re-validate.

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_config.py::TestChronicBaselineConfig -v
```

Expected: 15 passed (11 from Task 1 + 4 from Task 2).

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/config.py tests/test_config.py
git commit -m "Wire ChronicBaselineConfig into Settings, to_dict, YAML loader"
```

---

## Task 3: Add `run_chronic_baseline` flag to `_InvestigationModeConfig` and `_MODE_CONFIGS`

**Files:**
- Modify: `src/oci_logan_mcp/investigate.py:264-315` (`_InvestigationModeConfig` dataclass and `_MODE_CONFIGS` dict)
- Test: `tests/test_investigate.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_investigate.py` (anywhere among the unit-style classes, e.g. after `TestComputeWindows`):

```python
class TestChronicBaselineModeFlag:
    def test_quick_mode_does_not_run_chronic_baseline(self):
        from oci_logan_mcp.investigate import _mode_config
        assert _mode_config("quick").run_chronic_baseline is False

    def test_standard_mode_runs_chronic_baseline(self):
        from oci_logan_mcp.investigate import _mode_config
        assert _mode_config("standard").run_chronic_baseline is True

    def test_deep_mode_runs_chronic_baseline(self):
        from oci_logan_mcp.investigate import _mode_config
        assert _mode_config("deep").run_chronic_baseline is True
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_investigate.py::TestChronicBaselineModeFlag -v
```

Expected: 3 failed (AttributeError on `run_chronic_baseline`).

- [ ] **Step 3: Add the flag**

In `src/oci_logan_mcp/investigate.py`, modify `_InvestigationModeConfig` (around line 264) to add the new field at the end of the dataclass:

```python
@dataclass(frozen=True)
class _InvestigationModeConfig:
    name: str
    run_ingestion_health: bool
    run_parser_failures: bool
    run_entities: bool
    run_timeline: bool
    cluster_head: int
    entity_head: int
    timeline_head: int
    per_source_concurrency: int
    timeout_seconds: float
    run_chronic_baseline: bool
```

Update each entry in `_MODE_CONFIGS` to set the flag:

- `"quick"` → `run_chronic_baseline=False`
- `"standard"` → `run_chronic_baseline=True`
- `"deep"` → `run_chronic_baseline=True`

Apply consistently — three additions inside `_MODE_CONFIGS`.

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_investigate.py::TestChronicBaselineModeFlag -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/investigate.py tests/test_investigate.py
git commit -m "Add run_chronic_baseline flag to investigation mode configs"
```

---

## Task 4: `_compose_chronic_baseline_query` — pure query builder

**Files:**
- Modify: `src/oci_logan_mcp/investigate.py` (add new module-level function near the other composers, e.g. after `_compose_source_scoped_query` around line 84)
- Test: `tests/test_investigate.py`

The composer takes `seed_filter: str`, `terms: Tuple[str, ...]`, `top_k: int`, `focus_sources: Optional[List[str]]`. It produces the ranking query string per spec §3.2. Source names in `focus_sources` are quote-escaped; terms are NOT escaped (they are validated upstream).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investigate.py`:

```python
class TestComposeChronicBaselineQuery:
    DEFAULT_TERMS = ("error", "fail")  # short list for readable assertions

    def test_wildcard_seed_omits_seed_clause(self):
        from oci_logan_mcp.investigate import _compose_chronic_baseline_query
        q = _compose_chronic_baseline_query(
            seed_filter="*",
            terms=self.DEFAULT_TERMS,
            top_k=3,
            focus_sources=None,
        )
        assert q == (
            "('Original Log Content' like '%error%' or 'Original Log Content' like '%fail%')"
            " | stats count as n by 'Log Source' | sort -n | head 3"
        )

    def test_simple_seed_wraps_in_parens(self):
        from oci_logan_mcp.investigate import _compose_chronic_baseline_query
        q = _compose_chronic_baseline_query(
            seed_filter="'Event' = 'error'",
            terms=self.DEFAULT_TERMS,
            top_k=5,
            focus_sources=None,
        )
        assert q.startswith("('Event' = 'error') and (")
        assert q.endswith("| stats count as n by 'Log Source' | sort -n | head 5")
        assert "'Original Log Content' like '%error%'" in q
        assert "'Original Log Content' like '%fail%'" in q

    def test_focus_sources_appends_in_clause(self):
        from oci_logan_mcp.investigate import _compose_chronic_baseline_query
        q = _compose_chronic_baseline_query(
            seed_filter="*",
            terms=self.DEFAULT_TERMS,
            top_k=3,
            focus_sources=["Apache Access", "OKE Control Plane Logs"],
        )
        assert "and 'Log Source' in ('Apache Access', 'OKE Control Plane Logs')" in q

    def test_focus_source_with_embedded_quote_escaped(self):
        from oci_logan_mcp.investigate import _compose_chronic_baseline_query
        q = _compose_chronic_baseline_query(
            seed_filter="*",
            terms=self.DEFAULT_TERMS,
            top_k=3,
            focus_sources=["Bob's Logs"],
        )
        # Source names get single-quote doubled (matches _compose_source_scoped_query).
        assert "'Bob''s Logs'" in q

    def test_custom_terms_render_literally(self):
        from oci_logan_mcp.investigate import _compose_chronic_baseline_query
        q = _compose_chronic_baseline_query(
            seed_filter="*",
            terms=("xyz", "abc"),
            top_k=3,
            focus_sources=None,
        )
        assert "'Original Log Content' like '%xyz%'" in q
        assert "'Original Log Content' like '%abc%'" in q
        # No literal escaping of term content — terms are validated upstream.
        assert "%xyz%" in q

    def test_seed_with_focus_and_custom_top_k(self):
        from oci_logan_mcp.investigate import _compose_chronic_baseline_query
        q = _compose_chronic_baseline_query(
            seed_filter="'Severity' = 'ERROR'",
            terms=("error",),
            top_k=10,
            focus_sources=["X"],
        )
        assert q == (
            "('Severity' = 'ERROR') and ('Original Log Content' like '%error%')"
            " and 'Log Source' in ('X')"
            " | stats count as n by 'Log Source' | sort -n | head 10"
        )
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_investigate.py::TestComposeChronicBaselineQuery -v
```

Expected: 6 failed (`ImportError: cannot import name '_compose_chronic_baseline_query'`).

- [ ] **Step 3: Add the composer to `src/oci_logan_mcp/investigate.py`**

Insert after `_compose_source_scoped_query` (around line 84):

```python
def _compose_chronic_baseline_query(
    seed_filter: str,
    terms: Tuple[str, ...],
    top_k: int,
    focus_sources: Optional[List[str]],
) -> str:
    """Compose the chronic-baseline ranking query.

    Counts events per source matching any error-like substring over the
    current investigation window. Per spec §3.2:
      - terms are validated upstream (config load) and rendered literally
      - source names are escaped via single-quote-doubling
      - wildcard seed omits the seed clause entirely
    """
    error_clause = "(" + " or ".join(
        f"'Original Log Content' like '%{t}%'" for t in terms
    ) + ")"

    if seed_filter == "*":
        head = error_clause
    else:
        head = f"({seed_filter}) and {error_clause}"

    if focus_sources:
        escaped = ", ".join(
            f"'{name.replace(chr(39), chr(39) * 2)}'" for name in focus_sources
        )
        head = f"{head} and 'Log Source' in ({escaped})"

    return f"{head} | stats count as n by 'Log Source' | sort -n | head {top_k}"
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_investigate.py::TestComposeChronicBaselineQuery -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/investigate.py tests/test_investigate.py
git commit -m "Add _compose_chronic_baseline_query"
```

---

## Task 5: `_parse_chronic_response` — defensive parser

**Files:**
- Modify: `src/oci_logan_mcp/investigate.py` (add after `_parse_cluster_response`, around line 382)
- Test: `tests/test_investigate.py`

Per spec §3.3: reads `Log Source` and `n` columns; applies threshold and `focus_sources` filters in Python; emits `{source, error_like_count, error_like_share_of_seed}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investigate.py`:

```python
class TestParseChronicResponse:
    def _resp(self, rows):
        return {
            "data": {
                "columns": [{"name": "Log Source"}, {"name": "n"}],
                "rows": rows,
            }
        }

    def test_happy_path(self):
        from oci_logan_mcp.investigate import _parse_chronic_response
        out = _parse_chronic_response(
            self._resp([["Apache", 5000], ["Nginx", 2000]]),
            threshold=1000,
        )
        assert out == [
            {"source": "Apache", "error_like_count": 5000, "error_like_share_of_seed": None},
            {"source": "Nginx", "error_like_count": 2000, "error_like_share_of_seed": None},
        ]

    def test_threshold_filter_drops_below(self):
        from oci_logan_mcp.investigate import _parse_chronic_response
        out = _parse_chronic_response(
            self._resp([["Apache", 5000], ["Quiet", 50]]),
            threshold=1000,
        )
        assert [e["source"] for e in out] == ["Apache"]

    def test_threshold_zero_keeps_all(self):
        from oci_logan_mcp.investigate import _parse_chronic_response
        out = _parse_chronic_response(
            self._resp([["A", 1], ["B", 2]]),
            threshold=0,
        )
        assert len(out) == 2

    def test_malformed_columns_returns_empty(self):
        from oci_logan_mcp.investigate import _parse_chronic_response
        bad = {"data": {"columns": [{"name": "Wrong"}], "rows": [["x"]]}}
        assert _parse_chronic_response(bad, threshold=0) == []

    def test_empty_response(self):
        from oci_logan_mcp.investigate import _parse_chronic_response
        assert _parse_chronic_response({}, threshold=0) == []
        assert _parse_chronic_response({"data": {}}, threshold=0) == []

    def test_null_count_skipped(self):
        from oci_logan_mcp.investigate import _parse_chronic_response
        out = _parse_chronic_response(
            self._resp([["Apache", 5000], ["NullCount", None]]),
            threshold=0,
        )
        assert [e["source"] for e in out] == ["Apache"]

    def test_null_source_skipped(self):
        from oci_logan_mcp.investigate import _parse_chronic_response
        out = _parse_chronic_response(
            self._resp([[None, 9999], ["Apache", 5000]]),
            threshold=0,
        )
        assert [e["source"] for e in out] == ["Apache"]

    def test_focus_sources_filter_applied_defensively(self):
        # Even if engine returns sources outside focus list, parser drops them.
        from oci_logan_mcp.investigate import _parse_chronic_response
        out = _parse_chronic_response(
            self._resp([["Apache", 9000], ["Nginx", 8000], ["Other", 7000]]),
            threshold=0,
            focus_sources=["Apache", "Nginx"],
        )
        assert [e["source"] for e in out] == ["Apache", "Nginx"]

    def test_focus_sources_none_means_no_filter(self):
        from oci_logan_mcp.investigate import _parse_chronic_response
        out = _parse_chronic_response(
            self._resp([["A", 1000], ["B", 1000]]),
            threshold=0,
            focus_sources=None,
        )
        assert len(out) == 2

    def test_share_of_seed_when_seed_total_provided(self):
        from oci_logan_mcp.investigate import _parse_chronic_response
        out = _parse_chronic_response(
            self._resp([["Apache", 250]]),
            threshold=0,
            seed_total_events=1000,
        )
        assert out[0]["error_like_share_of_seed"] == 0.25

    def test_share_of_seed_zero_total_treated_as_none(self):
        # Avoid divide-by-zero; emit None.
        from oci_logan_mcp.investigate import _parse_chronic_response
        out = _parse_chronic_response(
            self._resp([["Apache", 250]]),
            threshold=0,
            seed_total_events=0,
        )
        assert out[0]["error_like_share_of_seed"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_investigate.py::TestParseChronicResponse -v
```

Expected: 11 failed (`ImportError`).

- [ ] **Step 3: Add the parser to `src/oci_logan_mcp/investigate.py`**

Insert after `_parse_cluster_response` (around line 382):

```python
def _parse_chronic_response(
    response: Dict[str, Any],
    threshold: int,
    focus_sources: Optional[List[str]] = None,
    seed_total_events: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Parse a `| stats count as n by 'Log Source'` response for chronic baseline.

    Defensive in two ways (spec §3.3):
      1. Drops rows where `n < threshold` even though the query may already cap.
      2. Drops sources not in `focus_sources` if provided, even if the query
         already filtered.

    `seed_total_events`, if provided and > 0, populates `error_like_share_of_seed`
    on each entry as `n / seed_total_events`. Otherwise the field is None.
    """
    data = response.get("data", {}) or {}
    columns = [c.get("name") for c in data.get("columns", []) or []]
    rows = data.get("rows", []) or []
    if "Log Source" not in columns or "n" not in columns:
        return []
    src_idx = columns.index("Log Source")
    cnt_idx = columns.index("n")
    max_idx = max(src_idx, cnt_idx)
    focus_set = set(focus_sources) if focus_sources else None
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not row or len(row) <= max_idx:
            continue
        src = row[src_idx]
        cnt = row[cnt_idx]
        if src is None or cnt is None:
            continue
        n = int(cnt)
        if n < threshold:
            continue
        src_str = str(src)
        if focus_set is not None and src_str not in focus_set:
            continue
        if seed_total_events and seed_total_events > 0:
            share: Optional[float] = n / seed_total_events
        else:
            share = None
        out.append({
            "source": src_str,
            "error_like_count": n,
            "error_like_share_of_seed": share,
        })
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_investigate.py::TestParseChronicResponse -v
```

Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/investigate.py tests/test_investigate.py
git commit -m "Add _parse_chronic_response with defensive threshold + focus filters"
```

---

## Task 6: `_merge_chronic_with_anomalous` — unified candidate list

**Files:**
- Modify: `src/oci_logan_mcp/investigate.py` (add after `_rank_anomalous_sources`, around line 147)
- Test: `tests/test_investigate.py`

Per spec §3.4: anomaly entries first (preserving their order), chronic-only appended (sorted by `error_like_count` desc), overlap merges with `reasons=["anomaly", "chronic_baseline"]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investigate.py`:

```python
class TestMergeChronicWithAnomalous:
    def _anomaly(self, source, pct=10.0, current=100, comparison=50):
        return {
            "source": source,
            "current_count": current,
            "comparison_count": comparison,
            "pct_change": pct,
        }

    def _chronic(self, source, count=5000, share=None):
        return {
            "source": source,
            "error_like_count": count,
            "error_like_share_of_seed": share,
        }

    def test_anomaly_only(self):
        from oci_logan_mcp.investigate import _merge_chronic_with_anomalous
        merged = _merge_chronic_with_anomalous(
            anomalous_sources=[self._anomaly("Apache")],
            chronic_sources=[],
        )
        assert len(merged) == 1
        assert merged[0]["source"] == "Apache"
        assert merged[0]["reasons"] == ["anomaly"]
        assert merged[0]["error_like_count"] is None
        assert merged[0]["error_like_share_of_seed"] is None

    def test_chronic_only(self):
        from oci_logan_mcp.investigate import _merge_chronic_with_anomalous
        merged = _merge_chronic_with_anomalous(
            anomalous_sources=[],
            chronic_sources=[self._chronic("OKE", 34000)],
        )
        assert len(merged) == 1
        assert merged[0]["source"] == "OKE"
        assert merged[0]["reasons"] == ["chronic_baseline"]
        assert merged[0]["error_like_count"] == 34000
        # Chronic-only entries must carry None for the anomaly numeric fields.
        assert merged[0]["current_count"] is None
        assert merged[0]["comparison_count"] is None
        assert merged[0]["pct_change"] is None

    def test_overlap_single_entry_combined_reasons(self):
        from oci_logan_mcp.investigate import _merge_chronic_with_anomalous
        merged = _merge_chronic_with_anomalous(
            anomalous_sources=[self._anomaly("X", pct=200)],
            chronic_sources=[self._chronic("X", count=8000)],
        )
        assert len(merged) == 1
        assert merged[0]["source"] == "X"
        assert merged[0]["reasons"] == ["anomaly", "chronic_baseline"]
        assert merged[0]["pct_change"] == 200
        assert merged[0]["error_like_count"] == 8000

    def test_anomaly_first_then_chronic_only(self):
        from oci_logan_mcp.investigate import _merge_chronic_with_anomalous
        merged = _merge_chronic_with_anomalous(
            anomalous_sources=[self._anomaly("A"), self._anomaly("B")],
            chronic_sources=[self._chronic("C", 9000), self._chronic("D", 3000)],
        )
        sources = [e["source"] for e in merged]
        # Anomaly order preserved (A, B). Chronic-only sorted by count desc (C, D).
        assert sources == ["A", "B", "C", "D"]

    def test_chronic_only_sorted_by_count_desc(self):
        from oci_logan_mcp.investigate import _merge_chronic_with_anomalous
        merged = _merge_chronic_with_anomalous(
            anomalous_sources=[],
            chronic_sources=[
                self._chronic("Low", 1500),
                self._chronic("High", 9000),
                self._chronic("Mid", 3000),
            ],
        )
        assert [e["source"] for e in merged] == ["High", "Mid", "Low"]

    def test_empty_inputs(self):
        from oci_logan_mcp.investigate import _merge_chronic_with_anomalous
        assert _merge_chronic_with_anomalous([], []) == []

    def test_overlap_preserves_anomaly_position(self):
        # Anomaly order: A, B, C. B is also chronic. D is chronic-only.
        # Result order: A, B (overlap), C, D.
        from oci_logan_mcp.investigate import _merge_chronic_with_anomalous
        merged = _merge_chronic_with_anomalous(
            anomalous_sources=[
                self._anomaly("A"), self._anomaly("B"), self._anomaly("C"),
            ],
            chronic_sources=[self._chronic("B", 7000), self._chronic("D", 5000)],
        )
        assert [e["source"] for e in merged] == ["A", "B", "C", "D"]
        b = next(e for e in merged if e["source"] == "B")
        assert b["reasons"] == ["anomaly", "chronic_baseline"]
        assert b["error_like_count"] == 7000
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_investigate.py::TestMergeChronicWithAnomalous -v
```

Expected: 7 failed (`ImportError`).

- [ ] **Step 3: Add the merger to `src/oci_logan_mcp/investigate.py`**

Insert after `_rank_anomalous_sources` (around line 147):

```python
def _merge_chronic_with_anomalous(
    anomalous_sources: List[Dict[str, Any]],
    chronic_sources: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge anomaly and chronic-baseline candidate lists per spec §3.4.

    Order: anomaly entries first (preserving incoming order), then chronic-only
    entries sorted by `error_like_count` desc. Sources appearing in both lists
    yield a single entry with `reasons=["anomaly", "chronic_baseline"]` placed
    at the anomaly source's position.
    """
    chronic_by_source = {c["source"]: c for c in chronic_sources}
    seen_anomaly_sources = set()
    merged: List[Dict[str, Any]] = []

    for a in anomalous_sources:
        src = a["source"]
        seen_anomaly_sources.add(src)
        chronic_match = chronic_by_source.get(src)
        entry = dict(a)
        if chronic_match is not None:
            entry["reasons"] = ["anomaly", "chronic_baseline"]
            entry["error_like_count"] = chronic_match["error_like_count"]
            entry["error_like_share_of_seed"] = chronic_match["error_like_share_of_seed"]
        else:
            entry["reasons"] = ["anomaly"]
            entry["error_like_count"] = None
            entry["error_like_share_of_seed"] = None
        merged.append(entry)

    chronic_only = [
        c for c in chronic_sources if c["source"] not in seen_anomaly_sources
    ]
    chronic_only.sort(key=lambda c: c["error_like_count"], reverse=True)
    for c in chronic_only:
        merged.append({
            "source": c["source"],
            "current_count": None,
            "comparison_count": None,
            "pct_change": None,
            "reasons": ["chronic_baseline"],
            "error_like_count": c["error_like_count"],
            "error_like_share_of_seed": c["error_like_share_of_seed"],
        })

    return merged
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_investigate.py::TestMergeChronicWithAnomalous -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/investigate.py tests/test_investigate.py
git commit -m "Add _merge_chronic_with_anomalous with anomaly-first ordering"
```

---

## Task 7: `_run_chronic_baseline_track` method on `InvestigateIncidentTool`

**Files:**
- Modify: `src/oci_logan_mcp/investigate.py` — add method to `InvestigateIncidentTool` class (around line 902, near `_run_diff_track`)
- Test: deferred to Task 8 (covered by orchestrator integration tests)

This task adds the method only. Wiring into `_run_core_tracks` happens in Task 8 to keep diffs reviewable.

- [ ] **Step 1: Add the method to `InvestigateIncidentTool`**

In `src/oci_logan_mcp/investigate.py`, in the `InvestigateIncidentTool` class (after `_run_diff_track`, around line 928), add:

```python
    async def _run_chronic_baseline_track(
        self,
        seed_filter: str,
        time_range: str,
        compartment_id: Optional[str],
        focus_sources: Optional[List[str]],
        top_k: int,
        timeout_seconds: Optional[float],
    ) -> List[Dict[str, Any]]:
        """Chronic-baseline ranking track — peer of diff per spec §3.1.

        Returns the parsed chronic_baseline_sources list (raw, pre-merge).
        Re-raises BudgetExceededError; other exceptions propagate to
        _run_core_tracks where they get mapped to partial_reasons.
        """
        cfg = self._settings.chronic_baseline
        if not cfg.enabled:
            return []
        query = _compose_chronic_baseline_query(
            seed_filter=seed_filter,
            terms=cfg.error_like_terms,
            top_k=top_k,
            focus_sources=focus_sources,
        )
        response = await _await_with_timeout(
            self._engine.execute(
                query=query,
                time_range=time_range,
                compartment_id=compartment_id,
            ),
            timeout_seconds,
        )
        return _parse_chronic_response(
            response,
            threshold=cfg.count_threshold,
            focus_sources=focus_sources,
            seed_total_events=None,  # share-of-seed wiring deferred; see spec §3.3
        )
```

The `seed_total_events=None` is intentional — spec §3.3 says best-effort, and we don't have a cheap seed-total at this layer yet. Future work can populate it without changing this method's signature for callers.

- [ ] **Step 2: Quick smoke check that the method imports cleanly**

```
python -c "from oci_logan_mcp.investigate import InvestigateIncidentTool; print(hasattr(InvestigateIncidentTool, '_run_chronic_baseline_track'))"
```

Expected output: `True`.

- [ ] **Step 3: Commit**

```bash
git add src/oci_logan_mcp/investigate.py
git commit -m "Add _run_chronic_baseline_track method (not yet wired into orchestrator)"
```

---

## Task 8: Wire chronic track into `_run_core_tracks` and `run()`

**Files:**
- Modify: `src/oci_logan_mcp/investigate.py:619-778` (`run`), `src/oci_logan_mcp/investigate.py:779-845` (`_run_core_tracks`)
- Test: integration tests added in Tasks 11-16; smoke test now

This task is the largest single-file change. Break it into reviewable sub-steps.

- [ ] **Step 1: Write a smoke test that fails until wiring is complete**

Append to `tests/test_investigate.py`:

```python
class TestChronicTrackWiring:
    @pytest.mark.asyncio
    async def test_report_includes_chronic_baseline_sources_key(self):
        """The merged report exposes chronic_baseline_sources at top level."""
        engine = _make_engine()
        schema, ih, j2, diff = _make_deps()
        tool = InvestigateIncidentTool(
            query_engine=engine, schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="*", time_range="last_1_hour", top_k=3)
        assert "chronic_baseline_sources" in report
        assert isinstance(report["chronic_baseline_sources"], list)

    @pytest.mark.asyncio
    async def test_anomalous_sources_entries_have_reasons_field(self):
        """Existing anomaly entries gain a `reasons` field with ["anomaly"]."""
        diff_result = {
            "current": {}, "comparison": {}, "summary": "",
            "delta": [{"dimension": "Apache", "current": 100, "comparison": 50, "pct_change": 100.0}],
        }
        schema, ih, j2, diff = _make_deps()
        diff.run = AsyncMock(return_value=diff_result)
        tool = InvestigateIncidentTool(
            query_engine=_make_engine(), schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="*", time_range="last_1_hour", top_k=3)
        assert report["anomalous_sources"]
        assert report["anomalous_sources"][0]["reasons"] == ["anomaly"]
```

Run: `pytest tests/test_investigate.py::TestChronicTrackWiring -v` — expected: 2 failed (KeyError on `chronic_baseline_sources`, KeyError on `reasons`).

- [ ] **Step 2: Add the chronic track to `_run_core_tracks`**

In `src/oci_logan_mcp/investigate.py`, modify `_run_core_tracks` (around line 779). Update the signature to accept `top_k` and `focus_sources`:

```python
    async def _run_core_tracks(
        self,
        *,
        acc: Dict[str, Any],
        query: str,
        seed_filter: str,
        time_range: str,
        compartment_id: Optional[str],
        config: _InvestigationModeConfig,
        timeout_seconds: Optional[float],
        top_k: int,
        focus_sources: Optional[List[str]],
    ) -> None:
```

After the existing `parser_failures` track-spec block (around line 819, after the `if config.run_parser_failures` / `else` block), add:

```python
        if config.run_chronic_baseline:
            track_specs.append((
                "chronic_baseline",
                "chronic_baseline_sources",
                self._run_chronic_baseline_track(
                    seed_filter=seed_filter,
                    time_range=time_range,
                    compartment_id=compartment_id,
                    focus_sources=focus_sources,
                    top_k=top_k,
                    timeout_seconds=timeout_seconds,
                ),
            ))
        else:
            acc["chronic_baseline_sources"] = []
```

In the existing result-handling loop (around line 831), the failure mapping needs custom behaviour for the chronic track because its `acc_key` value is a list, not a dict-with-status. Modify the loop:

```python
        results = await asyncio.gather(
            *(spec[2] for spec in track_specs),
            return_exceptions=True,
        )
        for (track_name, acc_key, _), result in zip(track_specs, results):
            if isinstance(result, BudgetExceededError):
                acc["partial_reasons"].add("budget_exceeded")
                if track_name == "chronic_baseline":
                    acc[acc_key] = []
                else:
                    acc[acc_key] = _track_error_payload(track_name, result)
                continue
            if isinstance(result, asyncio.TimeoutError):
                acc["partial_reasons"].add(f"{track_name}_timeout")
                if track_name == "chronic_baseline":
                    acc[acc_key] = []
                else:
                    acc[acc_key] = _track_error_payload(track_name, result)
                continue
            if isinstance(result, Exception):
                acc["partial_reasons"].add(f"{track_name}_errors")
                if track_name == "chronic_baseline":
                    acc[acc_key] = []
                else:
                    acc[acc_key] = _track_error_payload(track_name, result)
                continue
            acc[acc_key] = result
```

The chronic-track failure path produces `chronic_baseline_sources = []` and a `partial_reasons` entry — never a `_track_error_payload` dict — per spec §4.3.

- [ ] **Step 3: Update `run()` to pass `top_k` and `focus_sources` to `_run_core_tracks`, then merge after core tracks**

In `src/oci_logan_mcp/investigate.py`, in `run()` (around line 619), at the call site of `_run_core_tracks` (around line 663), update the call:

```python
            await self._run_core_tracks(
                acc=acc,
                query=query,
                seed_filter=seed_filter,
                time_range=time_range,
                compartment_id=compartment_id,
                config=config,
                timeout_seconds=timeout_seconds,
                top_k=top_k,
                focus_sources=normalized_focus_sources,
            )
```

After the existing block that builds `acc["anomalous_sources"]` from `_rank_anomalous_sources` (around line 691-703), and BEFORE the `for s in acc["anomalous_sources"]` per_source seeding loop (around line 705), insert:

```python
            # Merge chronic baseline candidates with the anomaly-track entries.
            # Per spec §3.4, anomaly entries lead and chronic-only entries are
            # appended in error_like_count desc order. Drill-down (below)
            # iterates the merged list — chronic-only sources flow through the
            # same cluster + entity + timeline pipeline as anomaly entries.
            chronic_sources = acc.get("chronic_baseline_sources") or []
            acc["anomalous_sources"] = _merge_chronic_with_anomalous(
                anomalous_sources=acc["anomalous_sources"],
                chronic_sources=chronic_sources,
            )
```

The existing per_source seeding loop and `bounded()` drill-down already use `acc["anomalous_sources"]`, so no change is needed there — the loop just iterates more entries (anomaly + chronic-only).

- [ ] **Step 4: Run smoke tests**

```
pytest tests/test_investigate.py::TestChronicTrackWiring -v
```

Expected: 2 passed.

- [ ] **Step 5: Run the full investigate test module to catch regressions**

```
pytest tests/test_investigate.py -v
```

Expected: all pre-existing tests still pass; the new `TestChronicTrackWiring` tests pass; no other failures.

- [ ] **Step 6: Commit**

```bash
git add src/oci_logan_mcp/investigate.py tests/test_investigate.py
git commit -m "Wire chronic_baseline track into _run_core_tracks and run()"
```

---

## Task 9: `_finalize` exposes `chronic_baseline_sources` at top level

**Files:**
- Modify: `src/oci_logan_mcp/investigate.py:1048-1087` (`_finalize`)
- Test: `tests/test_investigate.py`

The `acc` accumulator already carries `chronic_baseline_sources` after Task 8. `_finalize` needs to copy it into the returned report dict.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_investigate.py`:

```python
class TestChronicBaselineSourcesInFinalReport:
    @pytest.mark.asyncio
    async def test_chronic_baseline_sources_propagates_from_track(self):
        """When the chronic track returns sources, they appear in the final report."""
        engine = _make_engine()
        # Diff returns nothing (anomaly track empty).
        # Chronic-baseline ranking query is the second engine.execute call after
        # the seed track. Use a side-effect to vary responses.
        chronic_resp = {
            "data": {
                "columns": [{"name": "Log Source"}, {"name": "n"}],
                "rows": [["OKE Control Plane Logs", 34000]],
            }
        }
        empty_resp = {"data": {"columns": [], "rows": []}}

        # Side effect: any ranking-style query (contains 'stats count as n by')
        # returns chronic_resp; everything else returns empty.
        async def fake_execute(*, query, **kwargs):
            if "'Original Log Content' like" in query and "by 'Log Source'" in query:
                return chronic_resp
            return empty_resp
        engine.execute = AsyncMock(side_effect=fake_execute)

        schema, ih, j2, diff = _make_deps()
        tool = InvestigateIncidentTool(
            query_engine=engine, schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="*", time_range="last_1_hour", top_k=3)

        assert report["chronic_baseline_sources"] == [
            {
                "source": "OKE Control Plane Logs",
                "error_like_count": 34000,
                "error_like_share_of_seed": None,
            }
        ]
```

Run: `pytest tests/test_investigate.py::TestChronicBaselineSourcesInFinalReport -v` — expected: KeyError on `chronic_baseline_sources` in the returned report.

- [ ] **Step 2: Add the field to `_finalize` output**

In `src/oci_logan_mcp/investigate.py`, modify the return dict in `_finalize` (around line 1073) to include the new key. Insert after `"anomalous_sources": anomalous_list,`:

```python
        "chronic_baseline_sources": acc.get("chronic_baseline_sources") or [],
```

- [ ] **Step 3: Run the test**

```
pytest tests/test_investigate.py::TestChronicBaselineSourcesInFinalReport -v
```

Expected: 1 passed.

- [ ] **Step 4: Commit**

```bash
git add src/oci_logan_mcp/investigate.py tests/test_investigate.py
git commit -m "Expose chronic_baseline_sources at top level in _finalize"
```

---

## Task 10: `_templated_summary` adds chronic-baseline sentence

**Files:**
- Modify: `src/oci_logan_mcp/investigate.py:559-589` (`_templated_summary`)
- Test: `tests/test_investigate.py`

Per spec §4.4: count merged entries with `"chronic_baseline"` in `reasons`. Pick the highest-volume chronic source by `error_like_count` (NOT the first merged entry — that may be an overlap, not the loudest chronic).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investigate.py`:

```python
class TestTemplatedSummaryChronicSentence:
    def _acc(self, anomalous_sources):
        return {
            "seed": {"seed_filter": "*", "seed_filter_degraded": True, "time_range": "last_1_hour"},
            "ingestion_health": None,
            "parser_failures": None,
            "anomalous_sources": anomalous_sources,
            "partial_reasons": set(),
        }

    def test_no_chronic_no_sentence(self):
        from oci_logan_mcp.investigate import _templated_summary
        acc = self._acc([
            {"source": "Apache", "pct_change": 100.0, "reasons": ["anomaly"]},
        ])
        s = _templated_summary(acc)
        assert "Chronic baseline" not in s

    def test_chronic_only_appends_sentence(self):
        from oci_logan_mcp.investigate import _templated_summary
        acc = self._acc([
            {
                "source": "OKE Control Plane Logs",
                "pct_change": None,
                "reasons": ["chronic_baseline"],
                "error_like_count": 34000,
            },
        ])
        s = _templated_summary(acc)
        assert "Chronic baseline: 1 source(s) with high error-like volume" in s
        assert "OKE Control Plane Logs 34000 events" in s

    def test_overlap_counted_in_sentence(self):
        # Source that's both anomaly and chronic still contributes to chronic count.
        from oci_logan_mcp.investigate import _templated_summary
        acc = self._acc([
            {
                "source": "Apache",
                "pct_change": 200.0,
                "reasons": ["anomaly", "chronic_baseline"],
                "error_like_count": 8000,
            },
        ])
        s = _templated_summary(acc)
        assert "Chronic baseline: 1 source(s)" in s
        assert "Apache 8000 events" in s

    def test_top_chronic_picked_by_count_not_position(self):
        # First chronic-tagged entry in merged list is an overlap with low count.
        # The 'top' selector must pick the highest error_like_count instead.
        from oci_logan_mcp.investigate import _templated_summary
        acc = self._acc([
            {
                "source": "Apache",
                "pct_change": 100.0,
                "reasons": ["anomaly", "chronic_baseline"],
                "error_like_count": 1500,
            },
            {
                "source": "OKE",
                "pct_change": None,
                "reasons": ["chronic_baseline"],
                "error_like_count": 34000,
            },
        ])
        s = _templated_summary(acc)
        assert "OKE 34000 events" in s
        assert "Apache 1500" not in s

    def test_handles_none_error_like_count_defensively(self):
        # If somehow a chronic-tagged entry has no count, don't crash.
        from oci_logan_mcp.investigate import _templated_summary
        acc = self._acc([
            {
                "source": "X",
                "pct_change": None,
                "reasons": ["chronic_baseline"],
                "error_like_count": None,
            },
        ])
        # Expect it not to raise; we don't pin exact text since the count is None.
        _templated_summary(acc)
```

Run: `pytest tests/test_investigate.py::TestTemplatedSummaryChronicSentence -v` — expected: 4 failed (sentence not produced), 1 passed (`test_no_chronic_no_sentence`).

- [ ] **Step 2: Add the chronic sentence to `_templated_summary`**

In `src/oci_logan_mcp/investigate.py`, modify `_templated_summary` (line 559). After the `if parse_count:` block (around line 583) and before the `reasons = acc.get("partial_reasons")` block, insert:

```python
    chronic_entries = [
        s for s in (anomalous or [])
        if "chronic_baseline" in (s.get("reasons") or [])
    ]
    if chronic_entries:
        # Pick the highest-volume chronic source — NOT the first merged entry.
        # Merged ordering is anomaly-first, so the first chronic-tagged entry is
        # often an anomaly+chronic overlap, not the loudest chronic finding.
        # Treat None defensively as 0 for the max() key.
        top_chronic = max(
            chronic_entries,
            key=lambda s: s.get("error_like_count") or 0,
        )
        parts.append(
            f"Chronic baseline: {len(chronic_entries)} source(s) with high "
            f"error-like volume (top: {top_chronic['source']} "
            f"{top_chronic.get('error_like_count')} events)."
        )
```

(`anomalous` is already defined at line 568 as `acc.get("anomalous_sources") or []`.)

- [ ] **Step 3: Run the tests**

```
pytest tests/test_investigate.py::TestTemplatedSummaryChronicSentence -v
```

Expected: 5 passed.

- [ ] **Step 4: Run the full test module**

```
pytest tests/test_investigate.py -v
```

Expected: all green. The `test_summary_mentions_anomalous_sources` existing test still passes (the chronic sentence appends; it doesn't replace the anomalous-source sentence).

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/investigate.py tests/test_investigate.py
git commit -m "Add chronic-baseline sentence to investigate summary"
```

---

## Task 11: Integration test — OKE-style scenario (the primary bug fix)

**Files:**
- Test: `tests/test_investigate.py`

This is the regression-prevention test for the original failure case from the spec §1. Anomaly delta empty + chronic returns one source over threshold → that source must (a) appear in merged `anomalous_sources` with `reasons=["chronic_baseline"]`, (b) have drill-down populated (clusters / entities / timeline), (c) appear in top-level `chronic_baseline_sources`, (d) be mentioned in the summary.

- [ ] **Step 1: Write the integration test**

Append to `tests/test_investigate.py`:

```python
class TestChronicBaselineOKEScenario:
    @pytest.mark.asyncio
    async def test_oke_style_chronic_only_drill_down_runs(self):
        """The primary bug fix: chronic-only sources go through drill-down.

        Anomaly track returns no entries (steady-state, near-zero delta).
        Chronic ranking returns OKE Control Plane Logs over threshold.
        Drill-down (cluster / entity / timeline) MUST run for it.
        """
        # Mock engine response routing by query content.
        cluster_resp = {
            "data": {
                "columns": [{"name": "Cluster Sample"}, {"name": "Count"}],
                "rows": [
                    ["kube-apiserver dial tcp 168.254.5.3:2379: connection refused", 1440],
                ],
            }
        }
        entity_resp = {
            "data": {
                "columns": [{"name": "Host Name (Server)"}, {"name": "n"}],
                "rows": [["kube-apiserver-0", 800]],
            }
        }
        timeline_resp = {
            "data": {
                "columns": [{"name": "Time"}, {"name": "Severity"}, {"name": "Original Log Content"}],
                "rows": [["2026-05-09T10:00:00+00:00", "ERROR", "dial tcp ..."]],
            }
        }
        chronic_resp = {
            "data": {
                "columns": [{"name": "Log Source"}, {"name": "n"}],
                "rows": [["OKE Control Plane Logs", 34475]],
            }
        }

        async def fake_execute(*, query, **kwargs):
            if "'Original Log Content' like" in query and "by 'Log Source'" in query:
                return chronic_resp
            if "| cluster" in query:
                return cluster_resp
            if "stats count as n by 'Host Name" in query:
                return entity_resp
            if "fields Time, Severity, 'Original Log Content'" in query:
                return timeline_resp
            return {"data": {"columns": [], "rows": []}}

        engine = _make_engine()
        engine.execute = AsyncMock(side_effect=fake_execute)
        schema, ih, j2, diff = _make_deps()
        # Diff returns no anomalies (the OKE failure mode).
        diff.run = AsyncMock(return_value={
            "current": {}, "comparison": {}, "delta": [], "summary": "no change",
        })
        tool = InvestigateIncidentTool(
            query_engine=engine, schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(
            query="*", time_range="last_24_hours", top_k=3, mode="standard",
        )

        # (a) chronic-only source appears in merged anomalous_sources
        sources = [s["source"] for s in report["anomalous_sources"]]
        assert "OKE Control Plane Logs" in sources
        oke = next(s for s in report["anomalous_sources"] if s["source"] == "OKE Control Plane Logs")
        assert oke["reasons"] == ["chronic_baseline"]
        assert oke["error_like_count"] == 34475
        # Anomaly numerics must be None for chronic-only entries.
        assert oke["pct_change"] is None
        assert oke["current_count"] is None
        assert oke["comparison_count"] is None

        # (b) drill-down ran — clusters / entities / timeline populated. THIS IS
        # the regression-prevention assertion. Without merge before drill-down,
        # this list would be empty.
        assert oke["top_error_clusters"], "drill-down clusters missing — chronic source bypassed _drill_down_one_source"
        assert oke["top_entities"], "drill-down entities missing"
        assert oke["timeline"], "drill-down timeline missing"

        # (c) raw chronic_baseline_sources at top level
        assert report["chronic_baseline_sources"] == [
            {
                "source": "OKE Control Plane Logs",
                "error_like_count": 34475,
                "error_like_share_of_seed": None,
            }
        ]

        # (d) summary mentions chronic baseline
        assert "Chronic baseline" in report["summary"]
        assert "OKE Control Plane Logs" in report["summary"]
```

- [ ] **Step 2: Run it**

```
pytest tests/test_investigate.py::TestChronicBaselineOKEScenario -v
```

Expected: 1 passed. If drill-down assertions fail, debug Task 8 — the merge step must run before the per_source seeding loop (the loop iterates `acc["anomalous_sources"]` to populate the drill-down dict).

- [ ] **Step 3: Commit**

```bash
git add tests/test_investigate.py
git commit -m "Add OKE-style chronic-only drill-down regression test"
```

---

## Task 12: Integration test — both tracks fire on same source (overlap)

**Files:**
- Test: `tests/test_investigate.py`

- [ ] **Step 1: Write the test**

Append to `tests/test_investigate.py`:

```python
class TestChronicBaselineOverlap:
    @pytest.mark.asyncio
    async def test_overlap_single_drilldown_combined_reasons(self):
        cluster_resp = {
            "data": {
                "columns": [{"name": "Cluster Sample"}, {"name": "Count"}],
                "rows": [["sample log", 100]],
            }
        }
        chronic_resp = {
            "data": {
                "columns": [{"name": "Log Source"}, {"name": "n"}],
                "rows": [["Apache", 5000]],
            }
        }
        cluster_calls = []

        async def fake_execute(*, query, **kwargs):
            if "'Original Log Content' like" in query and "by 'Log Source'" in query:
                return chronic_resp
            if "| cluster" in query:
                cluster_calls.append(query)
                return cluster_resp
            return {"data": {"columns": [], "rows": []}}

        engine = _make_engine()
        engine.execute = AsyncMock(side_effect=fake_execute)
        schema, ih, j2, diff = _make_deps()
        diff.run = AsyncMock(return_value={
            "current": {}, "comparison": {}, "summary": "",
            "delta": [{"dimension": "Apache", "current": 100, "comparison": 50, "pct_change": 100.0}],
        })
        tool = InvestigateIncidentTool(
            query_engine=engine, schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="*", time_range="last_1_hour", top_k=3)

        apache_entries = [s for s in report["anomalous_sources"] if s["source"] == "Apache"]
        assert len(apache_entries) == 1, "Apache must appear exactly once after merge"
        a = apache_entries[0]
        assert a["reasons"] == ["anomaly", "chronic_baseline"]
        assert a["pct_change"] == 100.0
        assert a["error_like_count"] == 5000

        # Drill-down ran exactly once for Apache (one cluster query).
        apache_cluster_calls = [q for q in cluster_calls if "'Apache'" in q]
        assert len(apache_cluster_calls) == 1
```

- [ ] **Step 2: Run it**

```
pytest tests/test_investigate.py::TestChronicBaselineOverlap -v
```

Expected: 1 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/test_investigate.py
git commit -m "Add overlap integration test for chronic+anomaly source"
```

---

## Task 13: Integration test — `quick` mode skips chronic track

**Files:**
- Test: `tests/test_investigate.py`

- [ ] **Step 1: Write the test**

Append to `tests/test_investigate.py`:

```python
class TestChronicBaselineQuickMode:
    @pytest.mark.asyncio
    async def test_quick_mode_does_not_run_chronic_query(self):
        seen_queries = []

        async def fake_execute(*, query, **kwargs):
            seen_queries.append(query)
            return {"data": {"columns": [], "rows": []}}

        engine = _make_engine()
        engine.execute = AsyncMock(side_effect=fake_execute)
        schema, ih, j2, diff = _make_deps()
        tool = InvestigateIncidentTool(
            query_engine=engine, schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="*", time_range="last_1_hour", top_k=3, mode="quick")

        # No chronic ranking query was ever issued.
        chronic_qs = [
            q for q in seen_queries
            if "'Original Log Content' like" in q and "by 'Log Source'" in q
        ]
        assert chronic_qs == []

        # chronic_baseline_sources is [] (the disabled signal, not a not_run dict).
        assert report["chronic_baseline_sources"] == []

        # Summary must not contain chronic-baseline sentence.
        assert "Chronic baseline" not in report["summary"]
```

- [ ] **Step 2: Run it**

```
pytest tests/test_investigate.py::TestChronicBaselineQuickMode -v
```

Expected: 1 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/test_investigate.py
git commit -m "Add integration test: quick mode skips chronic track"
```

---

## Task 14: Integration test — below-threshold filter

**Files:**
- Test: `tests/test_investigate.py`

- [ ] **Step 1: Write the test**

Append to `tests/test_investigate.py`:

```python
class TestChronicBaselineThreshold:
    @pytest.mark.asyncio
    async def test_below_threshold_sources_dropped_before_merge(self):
        chronic_resp = {
            "data": {
                "columns": [{"name": "Log Source"}, {"name": "n"}],
                "rows": [
                    ["Loud", 5000],
                    ["Quiet1", 50],
                    ["Quiet2", 100],
                    ["Quiet3", 200],
                    ["Quiet4", 999],
                ],
            }
        }

        async def fake_execute(*, query, **kwargs):
            if "'Original Log Content' like" in query and "by 'Log Source'" in query:
                return chronic_resp
            return {"data": {"columns": [], "rows": []}}

        engine = _make_engine()
        engine.execute = AsyncMock(side_effect=fake_execute)
        schema, ih, j2, diff = _make_deps()
        tool = InvestigateIncidentTool(
            query_engine=engine, schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(),  # default count_threshold=1000
            budget_tracker=_make_budget(),
        )
        report = await tool.run(query="*", time_range="last_1_hour", top_k=10)

        chronic_sources = [s["source"] for s in report["chronic_baseline_sources"]]
        assert chronic_sources == ["Loud"]
        # Only the over-threshold source reaches drill-down via the merged list.
        merged_chronic = [
            s for s in report["anomalous_sources"]
            if "chronic_baseline" in s.get("reasons", [])
        ]
        assert [s["source"] for s in merged_chronic] == ["Loud"]
```

- [ ] **Step 2: Run it**

```
pytest tests/test_investigate.py::TestChronicBaselineThreshold -v
```

Expected: 1 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/test_investigate.py
git commit -m "Add integration test: below-threshold sources dropped"
```

---

## Task 15: Integration tests — chronic track failure modes

**Files:**
- Test: `tests/test_investigate.py`

Three sub-cases per spec §4.3: timeout, generic exception, BudgetExceededError. Each must populate the right `partial_reasons` entry, leave `chronic_baseline_sources = []`, and not block the anomaly flow.

- [ ] **Step 1: Write the tests**

Append to `tests/test_investigate.py`:

```python
class TestChronicBaselineFailureModes:
    @pytest.mark.asyncio
    async def test_timeout_yields_chronic_baseline_timeout_partial_reason(self):
        async def fake_execute(*, query, **kwargs):
            if "'Original Log Content' like" in query and "by 'Log Source'" in query:
                raise asyncio.TimeoutError()
            return {"data": {"columns": [], "rows": []}}
        engine = _make_engine()
        engine.execute = AsyncMock(side_effect=fake_execute)
        schema, ih, j2, diff = _make_deps()
        diff.run = AsyncMock(return_value={
            "current": {}, "comparison": {}, "summary": "",
            "delta": [{"dimension": "Apache", "current": 100, "comparison": 50, "pct_change": 100.0}],
        })
        tool = InvestigateIncidentTool(
            query_engine=engine, schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="*", time_range="last_1_hour", top_k=3)

        assert "chronic_baseline_timeout" in report["partial_reasons"]
        assert report["chronic_baseline_sources"] == []
        # Anomaly flow unaffected — Apache still in merged list.
        assert any(s["source"] == "Apache" for s in report["anomalous_sources"])

    @pytest.mark.asyncio
    async def test_generic_exception_yields_chronic_baseline_errors_partial_reason(self):
        async def fake_execute(*, query, **kwargs):
            if "'Original Log Content' like" in query and "by 'Log Source'" in query:
                raise RuntimeError("boom")
            return {"data": {"columns": [], "rows": []}}
        engine = _make_engine()
        engine.execute = AsyncMock(side_effect=fake_execute)
        schema, ih, j2, diff = _make_deps()
        diff.run = AsyncMock(return_value={
            "current": {}, "comparison": {}, "summary": "",
            "delta": [{"dimension": "Apache", "current": 100, "comparison": 50, "pct_change": 100.0}],
        })
        tool = InvestigateIncidentTool(
            query_engine=engine, schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="*", time_range="last_1_hour", top_k=3)

        assert "chronic_baseline_errors" in report["partial_reasons"]
        assert report["chronic_baseline_sources"] == []
        assert any(s["source"] == "Apache" for s in report["anomalous_sources"])

    @pytest.mark.asyncio
    async def test_budget_exceeded_yields_budget_exceeded_partial_reason(self):
        from oci_logan_mcp.budget_tracker import BudgetExceededError

        async def fake_execute(*, query, **kwargs):
            if "'Original Log Content' like" in query and "by 'Log Source'" in query:
                raise BudgetExceededError("budget exhausted")
            return {"data": {"columns": [], "rows": []}}
        engine = _make_engine()
        engine.execute = AsyncMock(side_effect=fake_execute)
        schema, ih, j2, diff = _make_deps()
        diff.run = AsyncMock(return_value={
            "current": {}, "comparison": {}, "summary": "",
            "delta": [{"dimension": "Apache", "current": 100, "comparison": 50, "pct_change": 100.0}],
        })
        tool = InvestigateIncidentTool(
            query_engine=engine, schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="*", time_range="last_1_hour", top_k=3)

        assert "budget_exceeded" in report["partial_reasons"]
        assert report["chronic_baseline_sources"] == []
```

- [ ] **Step 2: Run them**

```
pytest tests/test_investigate.py::TestChronicBaselineFailureModes -v
```

Expected: 3 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/test_investigate.py
git commit -m "Add chronic-baseline failure-mode integration tests"
```

---

## Task 16: Integration test — `focus_sources` constrains both tracks

**Files:**
- Test: `tests/test_investigate.py`

- [ ] **Step 1: Write the test**

Append to `tests/test_investigate.py`:

```python
class TestChronicBaselineFocusSources:
    @pytest.mark.asyncio
    async def test_focus_sources_restricts_chronic_query_and_post_filter(self):
        composed_chronic_query = {"q": None}
        # Misbehaving engine returns sources outside the focus list to verify
        # the parser-level post-filter (spec §3.3 belt-and-braces).
        chronic_resp = {
            "data": {
                "columns": [{"name": "Log Source"}, {"name": "n"}],
                "rows": [
                    ["Apache", 5000],
                    ["Outsider", 9000],
                ],
            }
        }

        async def fake_execute(*, query, **kwargs):
            if "'Original Log Content' like" in query and "by 'Log Source'" in query:
                composed_chronic_query["q"] = query
                return chronic_resp
            return {"data": {"columns": [], "rows": []}}

        engine = _make_engine()
        engine.execute = AsyncMock(side_effect=fake_execute)
        schema, ih, j2, diff = _make_deps()
        tool = InvestigateIncidentTool(
            query_engine=engine, schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(
            query="*",
            time_range="last_1_hour",
            top_k=5,
            focus_sources=["Apache", "Nginx"],
        )

        # Query had the in-clause appended.
        assert composed_chronic_query["q"] is not None
        assert "and 'Log Source' in ('Apache', 'Nginx')" in composed_chronic_query["q"]

        # Post-filter dropped 'Outsider' even though the misbehaving engine returned it.
        chronic_sources = [s["source"] for s in report["chronic_baseline_sources"]]
        assert "Outsider" not in chronic_sources
        assert chronic_sources == ["Apache"]

        # Outsider not in merged list either.
        merged_sources = [s["source"] for s in report["anomalous_sources"]]
        assert "Outsider" not in merged_sources
```

- [ ] **Step 2: Run it**

```
pytest tests/test_investigate.py::TestChronicBaselineFocusSources -v
```

Expected: 1 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/test_investigate.py
git commit -m "Add focus_sources integration test for chronic track"
```

---

## Task 17: Downstream-reader audit and None-tolerance verification

**Files:**
- Inspect: `src/oci_logan_mcp/report_generator.py`, MCP handler files surfacing investigate_incident output
- Modify: any file whose reader is non-tolerant
- Test: `tests/test_report_generator.py` (and any other affected test files)

Per spec §4.2: every reader of `anomalous_sources` must tolerate `pct_change=None`, `current_count=None`, `comparison_count=None`. Audit and add tests demonstrating tolerance.

- [ ] **Step 1: Run the audit grep**

```
grep -n "pct_change\|current_count\|comparison_count" src/oci_logan_mcp/report_generator.py
```

Expected: lines around 137, 219, 736 reading `pct_change`. Inspect each:

- Line 137-138: `pct = top.get("pct_change"); pct_text = f" ({pct:+.1f}%)" if isinstance(pct, (int, float)) else ""` — already None-tolerant via `isinstance` guard.
- Line 219-220: same pattern. Already None-tolerant.
- Line 736: docstring/comment only.

```
grep -rn "pct_change\|current_count\|comparison_count" src/oci_logan_mcp/ | grep -v __pycache__
```

Walk every additional hit. For each non-test file: confirm the reader either uses `isinstance`/`or 0` defaulting, OR add a guard.

```
grep -rn "anomalous_sources" src/oci_logan_mcp/
```

Walk every hit. For each, verify that any field access on entries either uses `.get()` with a safe default or guards None.

If any reader is non-tolerant, add the guard. The default render for a missing pct_change is the empty string (`pct_text = ""`); for a missing count, render as "—" or omit the field. Match the pattern already used at line 137-138 of `report_generator.py`.

- [ ] **Step 2: Add a None-tolerance integration test for `report_generator`**

Append to `tests/test_report_generator.py`:

```python
class TestReportGeneratorChronicBaselineNoneTolerance:
    """Per spec §4.2, chronic-only entries in anomalous_sources have
    pct_change/current_count/comparison_count = None. report_generator
    must render these without crashing or producing 'None' literals in
    the output."""

    def _investigation_with_chronic_only_source(self):
        return {
            "summary": "test",
            "seed": {"query": "*", "seed_filter": "*", "time_range": "last_1_hour"},
            "ingestion_health": None,
            "parser_failures": None,
            "anomalous_sources": [
                {
                    "source": "OKE Control Plane Logs",
                    "current_count": None,
                    "comparison_count": None,
                    "pct_change": None,
                    "reasons": ["chronic_baseline"],
                    "error_like_count": 34000,
                    "error_like_share_of_seed": None,
                    "top_error_clusters": [
                        {"Cluster Sample": "kube-apiserver dial tcp ... refused", "Count": 1440},
                    ],
                    "top_entities": [],
                    "timeline": None,
                    "errors": [],
                },
            ],
            "chronic_baseline_sources": [
                {"source": "OKE Control Plane Logs", "error_like_count": 34000, "error_like_share_of_seed": None},
            ],
            "cross_source_timeline": [],
            "next_steps": [],
            "budget": {},
            "partial": False,
            "partial_reasons": [],
            "elapsed_seconds": 1.0,
        }

    def test_exec_summary_does_not_crash_on_none_pct_change(self):
        from oci_logan_mcp.report_generator import ReportGenerator
        gen = ReportGenerator()
        report = gen.build_report(self._investigation_with_chronic_only_source())
        # Just having executed is the assertion. Belt: no literal "None%" in output.
        rendered = str(report)
        assert "None%" not in rendered
        assert "+None" not in rendered

    def test_top_findings_renders_chronic_only_source(self):
        from oci_logan_mcp.report_generator import ReportGenerator
        gen = ReportGenerator()
        report = gen.build_report(self._investigation_with_chronic_only_source())
        rendered = str(report)
        # Source is named in the output.
        assert "OKE Control Plane Logs" in rendered
        # Cluster sample is named in the output (drill-down rendered).
        assert "kube-apiserver" in rendered
```

`ReportGenerator` may have a different public API; if `build_report` isn't the entry point, replace with the actual one (check existing tests in `tests/test_report_generator.py` for the pattern). Existing tests use `tests/test_report_generator.py:39-41` which set `pct_change=250.0` — that's the call shape. If the existing tests use `gen.generate(...)` or similar, mirror that.

- [ ] **Step 3: Run the new tests**

```
pytest tests/test_report_generator.py::TestReportGeneratorChronicBaselineNoneTolerance -v
```

Expected: 2 passed. If they fail because a reader is non-tolerant, fix the reader — add `isinstance` guards mirroring lines 137-138 / 219-220 of `report_generator.py`. Re-run until green.

- [ ] **Step 4: Run the full test suite**

```
pytest -v
```

Expected: all green. Address any other test file that asserts on numeric anomaly fields with non-None expectations on chronic-only entries.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "Audit downstream readers for chronic-only None tolerance"
```

---

## Task 18: Final verification

**Files:**
- None modified; runs the full test suite and a manual report-shape sanity check.

- [ ] **Step 1: Run the full test suite**

```
pytest -v
```

Expected: all green.

- [ ] **Step 2: Verify the new public report shape via a smoke script**

```
python -c "
import asyncio
from unittest.mock import AsyncMock, MagicMock
from oci_logan_mcp.investigate import InvestigateIncidentTool
from oci_logan_mcp.config import Settings
from oci_logan_mcp.budget_tracker import BudgetTracker, BudgetLimits

engine = MagicMock()
engine.execute = AsyncMock(return_value={'data': {'columns': [], 'rows': []}})
schema = MagicMock()
schema.get_log_sources = AsyncMock(return_value=[])
ih = MagicMock(); ih.run = AsyncMock(return_value={'summary': {'sources_healthy': 0, 'sources_stopped': 0, 'sources_unknown': 0}, 'findings': [], 'checked_at': '', 'metadata': {}})
j2 = MagicMock(); j2.run = AsyncMock(return_value={'failures': [], 'total_failure_count': 0})
diff = MagicMock(); diff.run = AsyncMock(return_value={'current': {}, 'comparison': {}, 'delta': [], 'summary': ''})

tool = InvestigateIncidentTool(
    query_engine=engine, schema_manager=schema,
    ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
    settings=Settings(),
    budget_tracker=BudgetTracker(session_id='t', limits=BudgetLimits(enabled=False, max_queries_per_session=100, max_bytes_per_session=0, max_cost_usd_per_session=0)),
)
report = asyncio.run(tool.run(query='*', time_range='last_1_hour', top_k=3, mode='standard'))
assert 'chronic_baseline_sources' in report
print('OK - chronic_baseline_sources:', report['chronic_baseline_sources'])
print('OK - anomalous_sources is a list:', isinstance(report['anomalous_sources'], list))
"
```

Expected: prints `OK - chronic_baseline_sources: []` and `OK - anomalous_sources is a list: True`.

- [ ] **Step 3: Final commit (if anything changed)**

If the smoke script revealed nothing, no commit needed. The work is done.

---

## Self-review checklist (already applied; re-verify before execution)

- **Spec coverage:** every spec section has tasks. §3.1 (track registration) → Tasks 3, 8. §3.2 (query) → Task 4. §3.3 (parser, threshold, focus_sources defensive filter) → Task 5. §3.4 (merge) → Task 6. §3.5 (drill-down through merge) → Task 8 step 3 + Task 11. §4.1 (top-level field) → Task 9. §4.2 (per-source schema, downstream audit) → Tasks 6, 17. §4.3 (failure signalling) → Tasks 8, 15. §4.4 (summary text) → Task 10. §5 (config) → Tasks 1, 2, 3. §6 (threshold rationale) → embedded in defaults (Task 1). §7 (interaction rules) → covered by Task 4 (focus_sources rendering), Task 5 (post-filter), Task 6 (top_k merge), Task 13 (mode gating). §8 test plan → Tasks 1-2 (config), 4 (compose), 5 (parse), 6 (merge), 11 (OKE), 12 (overlap), 13 (quick), 14 (threshold), 15 (failure), 16 (focus_sources), 17 (downstream audit).
- **Type/name consistency:** `_compose_chronic_baseline_query`, `_parse_chronic_response`, `_merge_chronic_with_anomalous`, `_run_chronic_baseline_track`, `chronic_baseline_sources`, `error_like_count`, `error_like_share_of_seed`, `error_like_terms`, `count_threshold`, `run_chronic_baseline`, `chronic_baseline_timeout`, `chronic_baseline_errors` — all spelled identically across tasks.
- **Placeholder scan:** no TBD / TODO / "etc." / "similar to" / "appropriate handling". All steps name exact files, lines, and code.
