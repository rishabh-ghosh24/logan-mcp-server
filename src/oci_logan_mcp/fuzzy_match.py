"""Fuzzy string matching utilities for field name suggestions."""

from typing import List, Optional, Tuple

try:
    from rapidfuzz import fuzz, process
    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    RAPIDFUZZ_AVAILABLE = False


def find_similar_fields(
    field_name: str,
    available_fields: List[str],
    limit: int = 5,
    threshold: int = 50,
) -> List[str]:
    """Find fields similar to the given field name.

    Uses fuzzy string matching to find similar field names,
    useful for suggesting corrections when a field is not found.

    Args:
        field_name: The field name to match.
        available_fields: List of available field names.
        limit: Maximum number of suggestions to return.
        threshold: Minimum similarity score (0-100) to include.

    Returns:
        List of similar field names, ordered by similarity.
    """
    if not available_fields:
        return []

    if RAPIDFUZZ_AVAILABLE:
        return _fuzzy_match_rapidfuzz(field_name, available_fields, limit, threshold)
    else:
        return _fuzzy_match_simple(field_name, available_fields, limit, threshold)


def _fuzzy_match_rapidfuzz(
    field_name: str,
    available_fields: List[str],
    limit: int,
    threshold: int,
) -> List[str]:
    """Use rapidfuzz for fuzzy matching.

    Args:
        field_name: The field name to match.
        available_fields: List of available field names.
        limit: Maximum results.
        threshold: Minimum score.

    Returns:
        List of similar field names.
    """
    results = process.extract(
        field_name,
        available_fields,
        scorer=fuzz.WRatio,
        limit=limit,
    )

    # Filter by threshold and return just the names
    return [name for name, score, _ in results if score >= threshold]


def _fuzzy_match_simple(
    field_name: str,
    available_fields: List[str],
    limit: int,
    threshold: int,
) -> List[str]:
    """Simple fuzzy matching fallback without rapidfuzz.

    Uses basic string similarity based on common substrings.

    Args:
        field_name: The field name to match.
        available_fields: List of available field names.
        limit: Maximum results.
        threshold: Minimum score (scaled for this algorithm).

    Returns:
        List of similar field names.
    """
    field_lower = field_name.lower()
    scores: List[Tuple[str, float]] = []

    for available in available_fields:
        score = _simple_similarity(field_lower, available.lower())
        if score >= threshold / 100:  # Normalize threshold
            scores.append((available, score))

    # Sort by score descending
    scores.sort(key=lambda x: x[1], reverse=True)

    return [name for name, _ in scores[:limit]]


def _simple_similarity(s1: str, s2: str) -> float:
    """Calculate simple string similarity.

    Uses a combination of:
    - Substring matching
    - Common character count
    - Length similarity

    Args:
        s1: First string (lowercase).
        s2: Second string (lowercase).

    Returns:
        Similarity score between 0 and 1.
    """
    if s1 == s2:
        return 1.0

    # Check if one contains the other
    if s1 in s2 or s2 in s1:
        longer = max(len(s1), len(s2))
        shorter = min(len(s1), len(s2))
        return 0.7 + (0.3 * shorter / longer)

    # Count common characters
    common = sum(1 for c in set(s1) if c in s2)
    total_unique = len(set(s1) | set(s2))

    if total_unique == 0:
        return 0

    char_similarity = common / total_unique

    # Length similarity
    length_sim = min(len(s1), len(s2)) / max(len(s1), len(s2)) if max(len(s1), len(s2)) > 0 else 0

    # Check common substrings (simplified)
    substring_score = _common_substring_score(s1, s2)

    # Weighted combination
    return 0.4 * char_similarity + 0.3 * length_sim + 0.3 * substring_score


def _common_substring_score(s1: str, s2: str) -> float:
    """Calculate score based on longest common substring.

    Args:
        s1: First string.
        s2: Second string.

    Returns:
        Score between 0 and 1.
    """
    if not s1 or not s2:
        return 0

    # Find longest common substring
    max_length = 0
    for i in range(len(s1)):
        for j in range(len(s2)):
            length = 0
            while (
                i + length < len(s1)
                and j + length < len(s2)
                and s1[i + length] == s2[j + length]
            ):
                length += 1
            max_length = max(max_length, length)

    avg_length = (len(s1) + len(s2)) / 2
    return max_length / avg_length if avg_length > 0 else 0


def normalize_field_name(field_name: str) -> str:
    """Normalize a field name for comparison.

    Removes common variations in field naming.

    Args:
        field_name: Field name to normalize.

    Returns:
        Normalized field name.
    """
    # Remove quotes
    name = field_name.strip("'\"")

    # Convert to lowercase
    name = name.lower()

    # Replace common separators with spaces
    for sep in ["_", "-", "."]:
        name = name.replace(sep, " ")

    # Remove extra whitespace
    name = " ".join(name.split())

    return name
