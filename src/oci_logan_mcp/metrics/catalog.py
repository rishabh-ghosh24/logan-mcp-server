"""Reviewed metric definitions and deterministic catalog lookup."""

from dataclasses import asdict
from datetime import date
from importlib.resources import files
import json
import re
from urllib.parse import urlsplit

import yaml

from .models import MetricDefinition, MetricsError


def _invalid():
    raise MetricsError("catalog_invalid", "The reviewed metric catalog is invalid.")


def _strings(value, *, required=False):
    if not isinstance(value, list) or (required and not value):
        _invalid()
    if any(not isinstance(item, str) or not item.strip() for item in value):
        _invalid()
    return tuple(value)


def _normalize(text):
    return " ".join(text.casefold().split())


class MetricCatalog:
    def __init__(self, definitions, *, version):
        self.version = version
        self._definitions = {entry.metric_key: entry for entry in definitions}

    @classmethod
    def from_package(cls):
        try:
            data = files("oci_logan_mcp") / "data"
            catalog = yaml.safe_load((data / "metric_catalog.yaml").read_text(encoding="utf-8"))
            manifest = json.loads((data / "metric_catalog_manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            raise MetricsError("catalog_unavailable", "The reviewed metric catalog is unavailable.") from None
        return cls.from_documents(catalog, manifest)

    @classmethod
    def from_documents(cls, catalog, manifest):
        try:
            return cls._validated(catalog, manifest)
        except (KeyError, TypeError, AttributeError, ValueError) as error:
            if isinstance(error, MetricsError):
                raise
            raise MetricsError("catalog_invalid", "The reviewed metric catalog is invalid.") from None

    @classmethod
    def _validated(cls, catalog, manifest):
        version = catalog["version"]
        if not isinstance(version, str) or not version or manifest["version"] != version:
            _invalid()
        candidates = manifest["candidates"]
        reviews = {}
        for candidate in candidates:
            key = candidate["metric_key"]
            if key in reviews or candidate["status"] not in ("retained", "corrected", "withheld"):
                _invalid()
            if not isinstance(candidate.get("reason"), str) or not candidate["reason"].strip():
                _invalid()
            reviews[key] = candidate
        definitions = []
        keys = set()
        for row in catalog["definitions"]:
            key = row["metric_key"]
            if key in keys or not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                _invalid()
            keys.add(key)
            review = reviews[key]
            if review["status"] == "withheld":
                _invalid()
            facts = review["verification"]
            namespace = row["namespace"]
            if not isinstance(namespace, str) or not namespace or namespace != facts["namespace"]:
                _invalid()
            if row.get("resource_group") != facts.get("resource_group"):
                _invalid()
            names = _strings(row["metric_names"], required=True)
            if len(names) > 4 or len(set(names)) != len(names):
                _invalid()
            groups = _strings(row.get("group_by_dimensions", []))
            filters = _strings(row.get("allowed_filter_dimensions", []))
            name_dimension = row.get("resource_name_dimension")
            roles = set(groups) | set(filters)
            if name_dimension is not None:
                if not isinstance(name_dimension, str) or not name_dimension:
                    _invalid()
                roles.add(name_dimension)
            sources = set()
            unit = row["unit"]
            if not isinstance(unit, str) or not unit:
                _invalid()
            for name in names:
                metric = facts["metrics"][name]
                dimensions = _strings(metric["dimensions"])
                if unit != metric["unit"] or not roles.issubset(dimensions):
                    _invalid()
                urls = _strings(metric["source_urls"], required=True)
                for url in urls:
                    parsed = urlsplit(url)
                    if parsed.scheme != "https" or parsed.hostname != "docs.oracle.com" or not parsed.path:
                        _invalid()
                date.fromisoformat(metric["reviewed"])
                sources.update(urls)
            aggregations = _strings(row["allowed_aggregations"], required=True)
            if not set(aggregations).issubset({"mean", "max", "min", "sum"}):
                _invalid()
            if row["default_aggregation"] not in aggregations or row["default_interval"] not in ("1m", "5m", "1h", "1d"):
                _invalid()
            if not isinstance(row["label"], str) or not row["label"].strip():
                _invalid()
            definitions.append(MetricDefinition(
                metric_key=key, namespace=namespace, label=row["label"], metric_names=names,
                unit=unit, aliases=_strings(row.get("aliases", [])),
                resource_group=row.get("resource_group"), resource_name_dimension=name_dimension,
                group_by_dimensions=groups, allowed_filter_dimensions=filters,
                allowed_aggregations=aggregations, default_aggregation=row["default_aggregation"],
                default_interval=row["default_interval"], source_urls=tuple(sorted(sources)),
            ))
        if not definitions or keys != {key for key, review in reviews.items() if review["status"] != "withheld"}:
            _invalid()
        return cls(definitions, version=version)

    def require(self, metric_key):
        if not isinstance(metric_key, str) or metric_key not in self._definitions:
            raise MetricsError("catalog_metric_not_found", "Choose an exact metric key from the reviewed catalog.")
        return self._definitions[metric_key]

    def search(self, query, *, namespace=None, resource_group=None, limit=25):
        if not isinstance(query, str) or not query.strip() or type(limit) is not int or not 1 <= limit <= 100:
            raise MetricsError("invalid_arguments", "Provide search text and a limit from 1 to 100.")
        needle = _normalize(query)
        scored = []
        for entry in self._definitions.values():
            if namespace is not None and entry.namespace != namespace:
                continue
            if resource_group is not None and entry.resource_group != resource_group:
                continue
            categories = ([_normalize(entry.metric_key)], list(map(_normalize, entry.metric_names)),
                          list(map(_normalize, entry.aliases)), [_normalize(entry.label)])
            rank = next((i for i, values in enumerate(categories) if needle in values), None)
            values = [value for category in categories for value in category]
            if rank is None:
                rank = 4 if any(value.startswith(needle) for value in values) else (
                    5 if any(needle in value for value in values) else None)
            if rank is not None:
                scored.append((rank, entry.metric_key, entry))
        best = min((row[0] for row in scored), default=None)
        matches = [row[2] for row in sorted(scored, key=lambda row: row[:2]) if row[0] == best]
        kinds = ("key", "metric_name", "alias", "label", "prefix", "substring")
        return {"query_language": "mql", "catalog_version": self.version,
                "matches": [asdict(entry) for entry in matches[:limit]],
                "match_type": kinds[best] if best is not None else None,
                "returned_count": min(limit, len(matches)), "matched_count": len(matches),
                "ambiguous": len(matches) > 1}
