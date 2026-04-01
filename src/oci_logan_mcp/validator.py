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
        """Validate field references in query.

        Only validates quoted tokens that appear in field positions:
        - Left side of comparisons: 'Field' = value
        - After BY/GROUP BY: stats count by 'Field'
        - After WHERE: where 'Field' ...
        - After FIELDS: fields 'Field', ...
        - After SORT: sort 'Field'
        - After DEDUP: dedup 'Field'
        - After RENAME ... AS: rename 'X' as 'NewName' (validates 'X', not 'NewName')

        Skips quoted tokens that are string literals or aliases:
        - Right side of comparisons: = 'value', in ('value1', 'value2')
        - After AS: stats count as 'Alias'
        - Inside IN lists: in ('val1', 'val2')
        """
        errors = []
        suggestions = []
        fixed_query = query

        referenced_fields = self._extract_field_references(query)

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

    def _extract_field_references(self, query: str) -> List[str]:
        """Extract only field-position quoted tokens from a query.

        Returns quoted tokens that are likely field names, excluding
        string literals (right side of =, values inside IN lists)
        and aliases (after AS).
        """
        fields = []

        # Find all quoted tokens with their positions
        token_pattern = re.compile(r"'([^']+)'")

        for match in token_pattern.finditer(query):
            token = match.group(1)
            start = match.start()

            # Get the text before this token (lowercased, stripped)
            prefix = query[:start].rstrip().lower()

            # Skip: right side of comparison operators (= 'value', != 'value', like 'value')
            if re.search(r'(=|!=|<>|>=|<=|>|<|like|not\s+like)\s*$', prefix):
                continue

            # Skip: after AS keyword (aliases like: stats count as 'Alias')
            if re.search(r'\bas\s*$', prefix):
                continue

            # Skip: inside IN (...) lists
            if self._is_inside_in_list(query, start):
                continue

            # Skip: after NOT IN (edge case)
            if re.search(r'\bnot\s+in\s*\(\s*$', prefix):
                continue

            # What's left should be field references
            fields.append(token)

        return fields

    def _is_inside_in_list(self, query: str, pos: int) -> bool:
        """Check if position is inside an IN (...) list.

        Looks backward from pos for an unmatched opening paren
        preceded by 'in'.
        """
        # Find the nearest unmatched '(' before this position
        depth = 0
        i = pos - 1
        while i >= 0:
            if query[i] == ')':
                depth += 1
            elif query[i] == '(':
                if depth == 0:
                    # Found unmatched '(' — check if preceded by 'in'
                    before_paren = query[:i].rstrip().lower()
                    if re.search(r'\bin\s*$', before_paren):
                        return True
                    return False
                depth -= 1
            i -= 1
        return False

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
