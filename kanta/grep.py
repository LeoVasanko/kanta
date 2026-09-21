"""Structural grep matching and match highlighting for change records.

Backs the ``--grep`` option of the ``python -m kanta`` CLI.  Patterns
match against the structure of a transaction — its action, user, and the
dotted paths and values of its diff — never against rendered output
text.  Matching is case-insensitive.

Path terms match element-wise: each dotted element of the term must match
a whole path element (``users`` matches ``users`` anywhere in the path
but not ``foousers``), unless the element uses shell wildcards
(``us*``).  The term's elements match as a contiguous sequence, so
``users.*.email`` matches the path ``users.alice.email``.  Only the
matched elements are highlighted.

Value terms match strings by substring and all other values (booleans,
numbers, null) only in full: ``true`` matches a boolean but ``tru`` does
not.  A term with wildcards matches the whole value text.  Matched
substrings — or the whole scalar — are highlighted.  When a logfmt
formatter (``-k``) prettifies a value or the user, both the raw and the
prettified form are matched.

A matched record carries a :class:`GrepHighlighter`, which the
:kanta.logging formatters consult to wrap exactly the matched regions
with a yellow background.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import json
from collections.abc import Iterator
from typing import Any

from kanta.logging import _USER_PATH, _collect_changes, _get_nested
from kanta.serialization.base import unmarshal
from kanta.structs import ChangeRecord
from kanta.tty import ANSI_RE, ESC, strip_ansi

_GLOB_CHARS = frozenset("*?[")

_MARK_BG = f"{ESC}48;5;220m"  # yellow background (xterm256 #ffd700) for matches
_UNMARK_BG = f"{ESC}49m"  # back to the default background, foreground untouched


def mark_spans(styled: str, spans: list[tuple[int, int]]) -> str:
    """Wrap the given visible-text spans of a styled string with the mark color.

    ``spans`` are ``(start, end)`` offsets into the visible text of
    *styled*; ANSI sequences are not counted.  Overlapping and adjacent
    spans are merged first, so overlapping matches from different patterns
    produce one continuous highlight.  The set/clear codes are inserted at
    the mapped positions in *styled*; only the background attribute is
    touched, leaving foreground colors intact.
    """
    spans = _merge_spans(spans)
    if not spans:
        return styled
    out: list[str] = []
    plain_pos = 0
    prev_end = 0
    for match in ANSI_RE.finditer(styled):
        out.append(_wrap_run(styled[prev_end : match.start()], plain_pos, spans))
        plain_pos += match.start() - prev_end
        out.append(match.group(0))
        prev_end = match.end()
    out.append(_wrap_run(styled[prev_end:], plain_pos, spans))
    return "".join(out)


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Return *spans* sorted, with overlapping and adjacent spans merged."""
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _wrap_run(run: str, plain_start: int, spans: list[tuple[int, int]]) -> str:
    """Wrap the intersections of *spans* with one escape-free text run."""
    if not run:
        return run
    out: list[str] = []
    pos = 0
    for start, end in spans:
        s = max(start - plain_start, 0)
        e = min(end - plain_start, len(run))
        if s >= e or e <= pos:
            continue
        out.append(run[pos:s])
        out.append(f"{_MARK_BG}{run[s:e]}{_UNMARK_BG}")
        pos = e
    out.append(run[pos:])
    return "".join(out)


def _find_spans(text: str, needle: str) -> list[tuple[int, int]]:
    """Return visible-text spans of every case-insensitive occurrence of *needle*."""
    if not needle:
        return []
    haystack = strip_ansi(text).lower()
    needle = needle.lower()
    spans = []
    pos = 0
    while (found := haystack.find(needle, pos)) >= 0:
        end = found + len(needle)
        spans.append((found, end))
        pos = end
    return spans


@dataclasses.dataclass(frozen=True)
class GrepPattern:
    """One parsed ``--grep`` pattern.

    The bare form (``term`` set) matches the action, the user, or any
    dotted path or value in the diff.  The ``path=value`` form
    (``path_term`` and ``value_term`` set) requires both sides to match
    within the same change line; either side may be left empty to match
    values only (``=value``) or paths only (``path=``).
    """

    raw: str
    term: str | None = None
    path_term: str | None = None
    value_term: str | None = None

    @classmethod
    def parse(cls, raw: str) -> GrepPattern:
        """Parse a pattern, splitting the ``path=value`` form on the first ``=``."""
        if "=" in raw:
            path_term, value_term = raw.split("=", 1)
            return cls(raw, path_term=path_term, value_term=value_term)
        return cls(raw, term=raw)


@dataclasses.dataclass(frozen=True)
class _ValueMark:
    """A value-side match.

    ``needle`` is the text to locate in the displayed value ("" = a match
    that marks nothing, e.g. from an empty term).  ``whole`` marks the
    entire displayed value: used when the raw value matched but a logfmt
    formatter displays something else, so no needle can be located.
    """

    needle: str = ""
    whole: bool = False


@dataclasses.dataclass
class _Entry:
    """One matchable ``(path, value)`` line of a record's flattened diff.

    ``anchor`` is set for deleted content: the deleted path whose line is
    displayed for this entry (the entry itself may sit below it).
    """

    path: list[str]
    raw: Any
    text: str
    anchor: list[str] | None = None


def _element_matches(pattern: str, element: str) -> bool:
    """Match one path element: in full, or as a glob when it uses wildcards."""
    pattern = pattern.casefold()
    element = element.casefold()
    if any(char in pattern for char in _GLOB_CHARS):
        return fnmatch.fnmatchcase(element, pattern)
    return element == pattern


def _match_path(term: str, path: list[str]) -> frozenset[int] | None:
    """Match a dotted term against *path* as a contiguous element sequence.

    Returns the indices of the matched elements, or ``None``.  An empty
    term matches anything and marks no elements.
    """
    if not term:
        return frozenset()
    patterns = term.split(".")
    for start in range(len(path) - len(patterns) + 1):
        if all(
            _element_matches(pattern, path[start + i])
            for i, pattern in enumerate(patterns)
        ):
            return frozenset(range(start, start + len(patterns)))
    return None


def _match_value(term: str, raw: Any, text: str) -> _ValueMark | None:
    """Match a term against a value.

    String values match by substring; containers do not match (their
    leaves are matched individually); all other values match only in
    full.  A term with wildcards matches the whole value text.
    """
    if not term:
        return _ValueMark()
    needle = term.casefold()
    haystack = text.casefold()
    if any(char in needle for char in _GLOB_CHARS):
        return _ValueMark(text) if fnmatch.fnmatchcase(haystack, needle) else None
    if isinstance(raw, str):
        return _ValueMark(term) if needle in haystack else None
    if isinstance(raw, (dict, list)):
        return None
    return _ValueMark(text) if needle == haystack else None


def _dual_mark(term: str, raw: Any, text: str, pretty: str | None) -> _ValueMark | None:
    """Match a term against both the raw and the prettified form of a value.

    *pretty* is the logfmt-resolved display text, which is what the log
    shows when set.  A match on the displayed form locates its needle
    there; a match on the raw form alone marks the whole displayed value.
    """
    raw_mark = _match_value(term, raw, text)
    if pretty is None:
        return raw_mark
    pretty_mark = _match_value(term, pretty, pretty)
    if pretty_mark is not None:
        return pretty_mark
    if raw_mark is not None and (raw_mark.needle or raw_mark.whole):
        return _ValueMark(whole=True)
    return raw_mark


def _value_text(value: Any) -> str:
    """Render a value as matchable text, following the display conventions."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, default=str)
        except (TypeError, ValueError):
            pass
    return str(value)


def _leaf_entries(
    path: list[str], value: Any, anchor: list[str] | None
) -> Iterator[_Entry]:
    """Yield an entry for every value under *value*.

    Added, replaced or deleted containers are a single change line in the
    log but hold many values; descending into them lets patterns match
    their content.  List elements are addressed by index (``users.0``).
    """
    items: Iterator[tuple[str, Any]]
    if isinstance(value, dict):
        items = ((str(key), item) for key, item in value.items())
    elif isinstance(value, list):
        items = ((str(index), item) for index, item in enumerate(value))
    else:
        return
    for key, item in items:
        item_path = [*path, key]
        yield _Entry(item_path, item, _value_text(item), anchor)
        yield from _leaf_entries(item_path, item, anchor)


def _record_entries(record: ChangeRecord, previous: dict | None) -> list[_Entry]:
    """Flatten a record's diff into matchable entries.

    Uses the same traversal as the change-log rendering, so paths match
    what the log shows; deleted paths carry their previous value.
    """
    changes: list[tuple[str, list[str], Any]] = []
    _collect_changes(unmarshal(record.diff), [], changes, previous)
    entries: list[_Entry] = []
    for change_type, path, value in changes:
        anchor = None
        if change_type == "delete":
            value = _get_nested(previous, path)
            anchor = path
        entries.append(_Entry(path, value, _value_text(value), anchor))
        entries.extend(_leaf_entries(path, value, anchor))
    return entries


class GrepHighlighter:
    """The matched regions of one record, wrapping rendered text on demand.

    Implements the highlighter hook of the :mod:`kanta.logging`
    formatters: ``path`` for path elements, ``value`` for values,
    ``meta`` for header fields and ``delete`` for deletion markers.
    All wrapping goes through :func:`mark_spans`, so overlapping matches
    merge into one highlight.
    """

    def __init__(self) -> None:
        self._lit_paths: set[str] = set()
        self._lit_deletes: set[str] = set()
        self._value_marks: dict[str, list[_ValueMark]] = {}
        self._meta_marks: dict[str, list[_ValueMark]] = {}

    def _light_elements(self, path: list[str], indices) -> None:
        for i in indices:
            self._lit_paths.add(".".join(path[: i + 1]))

    def add(
        self,
        entry: _Entry,
        elements: frozenset[int] | None,
        vmark: _ValueMark | None,
    ) -> None:
        """Record one entry's match: lit element indices and/or a value mark."""
        marked = vmark is not None and (vmark.needle or vmark.whole)
        if entry.anchor is None:
            if elements:
                self._light_elements(entry.path, elements)
            if marked:
                key = ".".join(entry.path)
                self._value_marks.setdefault(key, []).append(vmark)
            return
        # Deleted content: only the anchor path line is displayed.  Light
        # the genuinely matched elements within it; a match on the removed
        # value or below the anchor marks the deletion marker (✗) instead.
        if elements:
            shown = {i for i in elements if i < len(entry.anchor)}
            if shown:
                self._light_elements(entry.path, shown)
            if len(shown) != len(elements):
                self._lit_deletes.add(".".join(entry.anchor))
        if marked:
            self._lit_deletes.add(".".join(entry.anchor))

    def add_meta(self, field: str, mark: _ValueMark) -> None:
        """Record a header match on ``field`` (``"action"`` or ``"user"``)."""
        if mark.needle or mark.whole:
            self._meta_marks.setdefault(field, []).append(mark)

    def path(self, text: str, path: str) -> str:
        """Wrap a rendered path element when its element matched."""
        if text and path in self._lit_paths:
            return mark_spans(text, [(0, len(strip_ansi(text)))])
        return text

    def delete(self, text: str, path: str) -> str:
        """Wrap the deletion marker when the removed content matched."""
        if text and path in self._lit_deletes:
            return mark_spans(text, [(0, len(strip_ansi(text)))])
        return text

    @staticmethod
    def _apply_marks(text: str, marks: list[_ValueMark]) -> str:
        if not text or not marks:
            return text
        spans: list[tuple[int, int]] = []
        for mark in marks:
            if mark.whole:
                spans.append((0, len(strip_ansi(text))))
            else:
                spans.extend(_find_spans(text, mark.needle))
        return mark_spans(text, spans)

    def value(self, text: str, path: str) -> str:
        """Wrap the matched regions of a rendered value."""
        return self._apply_marks(text, self._value_marks.get(path, []))

    def meta(self, text: str, field: str) -> str:
        """Wrap the matched regions of a rendered header field."""
        return self._apply_marks(text, self._meta_marks.get(field, []))


def _match_entry(
    pattern: GrepPattern, entry: _Entry, logfmt: Any
) -> tuple[frozenset[int] | None, _ValueMark | None] | None:
    """Match one pattern against one entry, returning its match marks.

    Returns ``None`` when the entry does not match.  Otherwise returns the
    matched path-element indices and/or the value mark, ready for
    :meth:`GrepHighlighter.add`.
    """
    pretty = None
    if logfmt is not None:
        pretty = logfmt(entry.raw, ".".join(entry.path))
    if pattern.term is not None:
        elements = _match_path(pattern.term, entry.path)
        vmark = _dual_mark(pattern.term, entry.raw, entry.text, pretty)
        if elements is None and vmark is None:
            return None
    else:
        elements = _match_path(pattern.path_term or "", entry.path)
        vmark = _dual_mark(pattern.value_term or "", entry.raw, entry.text, pretty)
        if elements is None or vmark is None:
            return None
    return elements, vmark


def matches_snapshot(
    state: dict, patterns: list[GrepPattern], logfmt: Any = None
) -> bool:
    """Match *patterns* against a snapshot's full state.

    The state is flattened into the same ``(path, value)`` entries change
    records are matched against, so path and value matching semantics are
    identical; a snapshot simply has no action or user to match.  Returns
    whether every pattern matched somewhere in the state.
    """
    entries = list(_leaf_entries([], unmarshal(state), None))
    for pattern in patterns:
        if not any(_match_entry(pattern, entry, logfmt) for entry in entries):
            return False
    return True


def evaluate(
    record: ChangeRecord,
    previous: dict | None,
    patterns: list[GrepPattern],
    logfmt: Any = None,
) -> GrepHighlighter | None:
    """Match *patterns* against a record, returning its matched regions.

    Returns ``None`` when any pattern matches nowhere in the transaction.
    Otherwise every pattern contributed its matches — action, user, or
    change lines — to the returned highlighter; different patterns may
    match different lines of the same record.

    ``logfmt`` is the optional composed logfmt callable; when given, both
    the raw and the prettified form of each value (and of the user) are
    matched.
    """
    entries = _record_entries(record, previous)
    highlighter = GrepHighlighter()
    for pattern in patterns:
        matched = False
        if pattern.term is not None:
            mark = _match_value(pattern.term, record.a, record.a)
            if mark is not None:
                highlighter.add_meta("action", mark)
                matched = True
            if record.u:
                pretty_user = (
                    logfmt(record.u, _USER_PATH) if logfmt is not None else None
                )
                mark = _dual_mark(pattern.term, record.u, record.u, pretty_user)
                if mark is not None:
                    highlighter.add_meta("user", mark)
                    matched = True
        for entry in entries:
            match = _match_entry(pattern, entry, logfmt)
            if match is None:
                continue
            matched = True
            highlighter.add(entry, *match)
        if not matched:
            return None
    return highlighter
