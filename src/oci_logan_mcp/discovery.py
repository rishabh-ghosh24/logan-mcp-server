"""Keyword discovery over catalog rows: users describe sources in words, not exact names.

Token rules were verified live on testtenancy (eight phrases, the live source ranked first each
time). A token hits a word by equality or prefix; substring matches need five or more characters so
that "ebs" never matches "webserver". Every token of the keyword must hit.
"""
import difflib
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

STOP_WORDS = {"log", "logs", "oci", "the", "a", "an", "of", "for", "service", "my", "our"}
ALIASES = {"k8s": "kubernetes", "oke": "kubernetes", "pg": "postgresql", "db": "database",
           "lb": "load balancer", "adb": "autonomous", "vm": "host"}
DESCRIPTION_LIMIT = 160
DEFAULT_LIMIT = 50
MAX_LIMIT = 500

_CAMEL_RE = re.compile(r"([a-z0-9])([A-Z])|([A-Z]+)([A-Z][a-z])")
_WORD_RE = re.compile(r"[a-z0-9]+")


def tokens(text: Optional[str]) -> List[str]:
    """Lower-case words of the text, with camelCase split (the unsplit word kept too), stop words
    dropped and aliases expanded."""
    text = text or ""
    split = _CAMEL_RE.sub(lambda m: " ".join(g for g in m.groups() if g), text)
    out: List[str] = []
    for raw in _WORD_RE.findall((text + " " + split).lower()):
        for word in ALIASES.get(raw, raw).split():
            if word not in STOP_WORDS and word not in out:
                out.append(word)
    return out


def _hit(token: str, word: str) -> bool:
    return word == token or word.startswith(token) or (len(token) >= 5 and token in word)


def matches(wanted: Sequence[str], haystack: Sequence[str]) -> bool:
    return all(any(_hit(token, word) for word in haystack) for token in wanted)


def score_source(row: Dict[str, Any], wanted: Sequence[str], keyword: str) -> int:
    """4 exact display name, 3 name match, 2 description match, 0 no match."""
    display = row.get("display_name") or ""
    if keyword and display.strip().lower() == keyword.strip().lower():
        return 4
    name_words = tokens(f"{display} {row.get('name') or ''}")
    if matches(wanted, name_words):
        return 3
    if matches(wanted, name_words + tokens(row.get("description"))):
        return 2
    return 0


def rank_sources(rows: Iterable[Dict[str, Any]], keyword: Optional[str],
                 volume: Optional[Dict[str, int]] = None) -> List[Dict[str, Any]]:
    """Sources matching the keyword (all when empty): exact name first, then sources with records
    in the window, then match score, then volume, then name."""
    wanted = tokens(keyword)
    volume = volume or {}
    scored = []
    for row in rows:
        score = score_source(row, wanted, keyword or "") if wanted else 1
        if score:
            records = volume.get(row.get("display_name") or "", 0)
            scored.append((score != 4, records == 0, -score, -records, (row.get("display_name") or "").lower(), row))
    scored.sort(key=lambda item: item[:5])
    return [item[5] for item in scored]


def filter_rows(rows: Sequence[Dict[str, Any]], keyword: Optional[str],
                columns: Sequence[str]) -> List[Dict[str, Any]]:
    """Rows whose named columns together contain every keyword token, sorted by the first column.
    Without a keyword the rows come back as given."""
    wanted = tokens(keyword)
    if not wanted:
        return list(rows)
    hits = [row for row in rows
            if matches(wanted, tokens(" ".join(str(row.get(column) or "") for column in columns)))]
    return sorted(hits, key=lambda row: str(row.get(columns[0]) or "").lower())


def envelope(items_key: str, rows: Sequence[Dict[str, Any]], *, keyword: Optional[str],
             limit: int, names: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """The listing shape every discovery tool returns."""
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    shown = list(rows[:limit])
    out: Dict[str, Any] = {items_key: shown, "total": len(rows), "returned": len(shown),
                           "truncated": len(rows) > len(shown), "keyword": keyword or None}
    if keyword and not rows and names:
        out["did_you_mean"] = close_names(keyword, names)
    return out


def close_names(keyword: str, names: Sequence[str], limit: int = 3) -> List[str]:
    """Names that look like the keyword, judged against the whole name and each of its words."""
    wanted = keyword.strip().lower()
    scored = []
    for name in names:
        words = [name.lower()] + name.lower().split()
        ratio = max(difflib.SequenceMatcher(None, wanted, word).ratio() for word in words)
        if ratio >= 0.6:
            scored.append((-ratio, name))
    return [name for _, name in sorted(scored)[:limit]]


def compact_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Drop empty values and cut long descriptions; the content that remains is unchanged."""
    out = {}
    for key, value in row.items():
        if value is None or value == "" or value == [] or value == {}:
            continue
        if key == "description" and isinstance(value, str) and len(value) > DESCRIPTION_LIMIT:
            value = value[:DESCRIPTION_LIMIT - 3] + "..."
        out[key] = value
    return out
