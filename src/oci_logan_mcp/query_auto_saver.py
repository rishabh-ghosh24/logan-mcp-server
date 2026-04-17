"""Auto-save interesting queries to learned_queries.yaml after successful execution.

Runs transparently — no LLM involvement required.  Every successful query is
scored for "interestingness"; those above the threshold are persisted via
UserStore.save_query() and available to any future LLM/IDE session.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional, Tuple

from .context_manager import ContextManager
from .user_store import UserStore

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------
# Trivial-query patterns (never auto-save)
# ----------------------------------------------------------------
_TRIVIAL_PATTERNS: list[re.Pattern] = [
    re.compile(r"^\s*\*\s*$"),                          # bare *
    re.compile(r"^\s*\*\s*\|\s*head\s+\d+\s*$", re.I), # * | head N
    re.compile(r"^\s*\*\s*\|\s*stats\s+count\s*$", re.I),  # * | stats count
]

# ----------------------------------------------------------------
# Category keyword mapping
# ----------------------------------------------------------------
_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "security": [
        "failed password", "sudo", "authentication", "login", "ssh",
        "unauthorized", "secure logs", "401", "forbidden", "denied",
    ],
    "errors": [
        "error", "critical", "fatal", "exception", "traceback",
        "severity", "500",
    ],
    "audit": [
        "audit", "change", "created", "deleted", "modified", "action",
    ],
    "performance": [
        "slow", "timeout", "latency", "response time", "duration", "ms",
    ],
    "network": [
        "vcn", "flow", "network", "ip", "port", "firewall", "vpn",
        "subnet", "vnic",
    ],
}

# Advanced commands that signal query complexity
_ADVANCED_COMMANDS = {
    "eval", "rename", "regex", "dedup", "distinct", "cluster",
    "classify", "link", "eventstats", "delta", "lookup", "nlp",
    "addfields",
}


class QueryAutoSaver:
    """Auto-saves interesting queries to learned_queries.yaml."""

    def __init__(
        self,
        context_manager: ContextManager,
        user_store: UserStore,
    ) -> None:
        self.context_manager = context_manager
        self.user_store = user_store

    # ----------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------

    def process_successful_query(
        self, query: str, result: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Evaluate a successful query and auto-save if interesting.

        Returns the saved/updated entry, or None if skipped.
        """
        try:
            # 1. Already saved? → bump use_count only
            if self.user_store.record_usage(query):
                return None

            # 2. Trivial? → skip
            score = self._compute_interest_score(query)
            if score < 2:
                return None

            # 3. Generate metadata from query text
            name, description, category = self._generate_metadata(query)

            # 4. Save via user_store
            saved = self.user_store.save_query(
                name=name,
                query=query.strip(),
                description=f"[auto-saved] {description}",
                category=category,
                tags=["auto-saved"],
                interest_score=score,
            )
            logger.info(f"Auto-saved query: {name} (score={score})")
            return saved

        except Exception as exc:
            # Never let auto-save failures break query execution
            logger.debug(f"Auto-save skipped: {exc}")
            return None

    # ----------------------------------------------------------------
    # Interest scoring
    # ----------------------------------------------------------------

    def _compute_interest_score(self, query: str) -> int:
        """Score query complexity. Needs >= 2 to be auto-saved."""
        q = query.strip()

        # Reject trivial patterns outright
        for pattern in _TRIVIAL_PATTERNS:
            if pattern.match(q):
                return 0

        score = 0
        q_lower = q.lower()
        stages = [s.strip() for s in q.split("|")]

        # Has stats or timestats aggregation
        if re.search(r"\bstats\b", q_lower) or re.search(r"\btimestats\b", q_lower):
            score += 1

        # Has where filter
        if re.search(r"\bwhere\b", q_lower):
            score += 1

        # Has specific Log Source filter
        if re.search(r"'log source'\s*=\s*'[^']+'", q_lower):
            score += 1

        # Has sort + head (top-N pattern)
        if re.search(r"\bsort\b", q_lower) and re.search(r"\bhead\b", q_lower):
            score += 1

        # Has advanced commands (max +2)
        advanced_count = 0
        for cmd in _ADVANCED_COMMANDS:
            if re.search(rf"\b{cmd}\b", q_lower):
                advanced_count += 1
        score += min(advanced_count, 2)

        # Has 3+ pipe stages
        if len(stages) >= 3:
            score += 1

        return score

    # ----------------------------------------------------------------
    # Metadata generation
    # ----------------------------------------------------------------

    def _generate_metadata(self, query: str) -> Tuple[str, str, str]:
        """Generate (name, description, category) from query text."""
        q = query.strip()
        q_lower = q.lower()
        stages = [s.strip() for s in q.split("|")]

        # --- Extract components ---
        source = self._extract_source(q)
        action = self._extract_action(stages)
        groupby = self._extract_groupby(q)
        limit = self._extract_limit(q)
        filter_field = self._extract_filter(q)

        # --- Build name ---
        parts: list[str] = []
        if source:
            parts.append(self._slugify(source))
        if filter_field:
            parts.append(self._slugify(filter_field))
        if action:
            parts.append(action)
        if groupby:
            parts.append(f"by_{self._slugify(groupby)}")
        if limit:
            parts.insert(0, f"top_{limit}")

        name = "_".join(parts) if parts else "query"
        name = name[:60]  # max length

        # Deduplicate name if collision
        name = self._unique_name(name)

        # --- Build description ---
        desc_parts: list[str] = []
        if action:
            desc_parts.append(action.replace("_", " ").title())
        if source:
            desc_parts.append(f"from {source}")
        if filter_field:
            desc_parts.append(f"filtered by {filter_field}")
        if groupby:
            desc_parts.append(f"grouped by {groupby}")
        if limit:
            desc_parts.append(f"(top {limit})")

        description = " ".join(desc_parts) if desc_parts else "Auto-saved query"

        # --- Infer category ---
        category = self._infer_category(q_lower)

        return name, description, category

    def _extract_source(self, query: str) -> Optional[str]:
        """Extract Log Source name if explicitly filtered."""
        m = re.search(r"'Log Source'\s*=\s*'([^']+)'", query, re.I)
        return m.group(1) if m else None

    def _extract_action(self, stages: list[str]) -> str:
        """Determine the primary action from pipe stages."""
        for stage in stages:
            sl = stage.strip().lower()
            if sl.startswith("timestats"):
                return "trend"
            if sl.startswith("stats"):
                # Extract the aggregation function
                m = re.match(r"stats\s+(\w+)", sl)
                func = m.group(1) if m else "count"
                return func
        return "search"

    def _extract_groupby(self, query: str) -> Optional[str]:
        """Extract the group-by field name."""
        m = re.search(r"\bby\s+'([^']+)'", query, re.I)
        return m.group(1) if m else None

    def _extract_limit(self, query: str) -> Optional[str]:
        """Extract head N limit."""
        m = re.search(r"\bhead\s+(\d+)", query, re.I)
        return m.group(1) if m else None

    def _extract_filter(self, query: str) -> Optional[str]:
        """Extract where clause field if present."""
        m = re.search(r"\bwhere\s+'?(\w[\w\s]*?)'?\s*(?:=|like|contains|regex|in\b)", query, re.I)
        return m.group(1).strip() if m else None

    def _infer_category(self, query_lower: str) -> str:
        """Infer category from query keywords."""
        for category, keywords in _CATEGORY_KEYWORDS.items():
            for kw in keywords:
                if kw in query_lower:
                    return category
        return "general"

    def _slugify(self, text: str) -> str:
        """Convert text to a slug suitable for a name."""
        slug = text.lower().strip()
        slug = re.sub(r"[^a-z0-9]+", "_", slug)
        slug = slug.strip("_")
        return slug[:30]

    def _unique_name(self, base_name: str) -> str:
        """Ensure name doesn't collide with existing learned queries."""
        existing_names = {q["name"] for q in self.user_store.list_queries()}
        if base_name not in existing_names:
            return base_name

        for i in range(2, 100):
            candidate = f"{base_name}_v{i}"
            if candidate not in existing_names:
                return candidate
        return f"{base_name}_{id(base_name)}"
