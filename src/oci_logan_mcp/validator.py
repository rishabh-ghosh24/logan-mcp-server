"""Query validation and intelligence service."""

import re
from typing import Optional, List, Tuple
from dataclasses import dataclass, field

from .fuzzy_match import find_similar_fields
from .schema_manager import SchemaManager


@dataclass
class ValidationResult:
    """Result of query validation."""

    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    estimated_cost: str = "low"
    suggested_fix: Optional[str] = None


class QueryValidator:
    """Validates queries and provides intelligent suggestions."""

    OPERATORS = [
        "where", "stats", "timestats", "sort", "head", "tail",
        "fields", "rename", "eval", "lookup", "join", "dedup",
        "rex", "replace", "cluster", "classify", "link", "bucket",
    ]

    def __init__(self, schema_manager: SchemaManager):
        """Initialize query validator."""
        self.schema_manager = schema_manager

    async def validate(
        self,
        query: str,
        time_start: Optional[str] = None,
        time_end: Optional[str] = None,
    ) -> ValidationResult:
        """Validate a query before execution."""
        errors = []
        warnings = []
        suggestions = []
        suggested_fix = query

        if not query or not query.strip():
            return ValidationResult(
                valid=False,
                errors=["Query cannot be empty"],
                warnings=[],
                suggestions=["Enter a search term or use * to match all records"],
                estimated_cost="low",
            )

        field_errors, field_suggestions, fixed_query = await self._validate_fields(query)
        errors.extend(field_errors)
        suggestions.extend(field_suggestions)
        if fixed_query != query:
            suggested_fix = fixed_query

        syntax_errors = self._validate_syntax(query)
        errors.extend(syntax_errors)

        time_warnings = self._validate_time_range(time_start, time_end)
        warnings.extend(time_warnings)

        estimated_cost = self._estimate_cost(query, time_start, time_end)

        if estimated_cost == "high":
            warnings.append(
                "Query may return large result set or take significant time. "
                "Consider adding filters or reducing time range."
            )

        return ValidationResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            suggestions=suggestions,
            estimated_cost=estimated_cost,
            suggested_fix=suggested_fix if suggested_fix != query else None,
        )

    async def _validate_fields(self, query: str) -> Tuple[List[str], List[str], str]:
        """Validate field references in query."""
        errors = []
        suggestions = []
        fixed_query = query

        field_pattern = r"'([^']+)'"
        referenced_fields = re.findall(field_pattern, query)

        if not referenced_fields:
            return errors, suggestions, fixed_query

        try:
            available_fields = await self.schema_manager.get_all_field_names()
        except Exception:
            return errors, suggestions, fixed_query

        if not available_fields:
            return errors, suggestions, fixed_query

        available_set = set(f.lower() for f in available_fields)

        for ref_field in referenced_fields:
            if ref_field.lower() not in available_set:
                similar = find_similar_fields(ref_field, available_fields, limit=3)

                if similar:
                    best_match = similar[0]
                    errors.append(
                        f"Field '{ref_field}' not found. Did you mean '{best_match}'?"
                    )
                    suggestions.append(f"Replace '{ref_field}' with '{best_match}'")
                    fixed_query = fixed_query.replace(f"'{ref_field}'", f"'{best_match}'")
                else:
                    errors.append(
                        f"Field '{ref_field}' not found and no similar fields found."
                    )

        return errors, suggestions, fixed_query

    def _validate_syntax(self, query: str) -> List[str]:
        """Basic syntax validation."""
        errors = []

        single_quotes = query.count("'")
        if single_quotes % 2 != 0:
            errors.append("Unbalanced single quotes in query")

        if query.count("(") != query.count(")"):
            errors.append("Unbalanced parentheses in query")

        if "| |" in query or query.strip().endswith("|"):
            errors.append("Empty pipe segment in query")

        query_lower = query.lower()
        typo_checks = [
            ("wehre", "where"),
            ("stast", "stats"),
            ("timestas", "timestats"),
            ("felds", "fields"),
        ]
        for typo, correct in typo_checks:
            if typo in query_lower:
                errors.append(f"Possible typo: '{typo}' should be '{correct}'")

        return errors

    def _validate_time_range(
        self, time_start: Optional[str], time_end: Optional[str]
    ) -> List[str]:
        """Validate time range."""
        warnings = []

        if not time_start or not time_end:
            return warnings

        try:
            from datetime import datetime

            start = datetime.fromisoformat(time_start.replace("Z", "+00:00"))
            end = datetime.fromisoformat(time_end.replace("Z", "+00:00"))

            delta = end - start
            if delta.days > 7:
                warnings.append(
                    f"Time range spans {delta.days} days. "
                    "Consider reducing for better performance."
                )
        except Exception:
            pass

        return warnings

    def _estimate_cost(
        self, query: str, time_start: Optional[str], time_end: Optional[str]
    ) -> str:
        """Estimate query cost/complexity."""
        query_lower = query.lower()

        has_filter = "where" in query_lower or "=" in query
        has_aggregation = any(
            op in query_lower for op in ["stats", "timestats", "count"]
        )
        is_wildcard_only = query.strip() == "*"
        has_limit = "head" in query_lower or "tail" in query_lower

        if is_wildcard_only and not has_limit:
            return "high"
        elif has_aggregation and has_filter:
            return "low"
        elif has_filter or has_limit:
            return "low"
        elif has_aggregation:
            return "medium"
        else:
            return "medium"

    def get_query_suggestions(self, partial_query: str) -> List[str]:
        """Get suggestions for completing a partial query."""
        suggestions = []
        query_lower = partial_query.lower().strip()

        if query_lower.endswith("|"):
            suggestions.extend([f"| {op}" for op in self.OPERATORS[:5]])

        if not query_lower or query_lower == "*":
            suggestions.extend(
                [
                    "* | stats count by 'Log Source'",
                    "'Error' | stats count",
                    "* | timestats count span=1hour",
                    "* | head 100",
                ]
            )

        return suggestions
