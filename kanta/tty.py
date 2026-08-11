"""Terminal string building: ANSI colors, display widths, and a line builder.

Colors are stored as bare SGR parameter strings (e.g. ``"1;34"``) without
the ``\\x1b[`` prefix and ``m`` suffix.  The :class:`Line` builder understands
how SGR parameters stack: ``0`` clears everything, other parameters apply
sequentially and the last one of each class wins.  This lets it emit minimal
escape sequences, folding a needed reset into the same sequence as the next
color instead of emitting a separate one.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

ESC = "\x1b["

# Matches a full ANSI escape sequence (color codes, cursor movement, ...).
ANSI_RE = re.compile(r"\x1b\[[0-9;:]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from *text*."""
    return ANSI_RE.sub("", text)


def displaywidth(text: str) -> int:
    """Return the terminal column width of *text*, ignoring ANSI sequences.

    Wide characters (CJK, most emoji) count as two columns; combining and
    zero-width characters count as zero.
    """
    return sum(
        2
        if unicodedata.east_asian_width(c) in "WF"
        else 0
        if unicodedata.category(c) in ("Mn", "Me", "Cf")
        else 1
        for c in strip_ansi(text)
    )


def pad(text: str, width: int, align: str = "left") -> str:
    """Pad *text* to *width* columns by display width.

    *align* is ``"left"`` (padding after), ``"right"`` (padding before), or
    ``"center"``.  Text already at or above *width* is returned unchanged.
    """
    missing = width - displaywidth(text)
    if missing <= 0:
        return text
    if align == "right":
        return " " * missing + text
    if align == "center":
        left = missing // 2
        return " " * left + text + " " * (missing - left)
    return text + " " * missing


class Colors:
    """Kanta's log color palette: bare SGR parameter strings.

    Attributes are looked up when a line is rendered, so assignments such as
    ``colors.action = "36"`` or additions like ``colors.session = "38;5;226"``
    take effect immediately, no matter how the object was imported.  Added
    colors become available on :class:`Line` under the same name.
    """

    action = "1;34"  # Bold blue for the action name
    user = "34"  # Blue for the user display
    target = "38;5;250"  # White for the extra/target display
    sep = "38;5;242"  # Dark grey for separators
    path_prefix = "38;5;242"  # Dark grey for the leading part of a dotted path
    path_final = "38;5;250"  # White for the final path element
    add = "32"  # Green for additions
    delete = "1;31"  # Bold red for deletions
    ellipsis = "38;5;242"  # Dark grey for the truncation ellipsis


colors = Colors()

# SGR attribute classes that carry no class siblings (each clears/sets itself).
_ATTR_CLASSES = frozenset({"1", "2", "3", "4", "7", "9"})


def _parse_sgr(spec: str) -> dict[str, str]:
    """Parse a bare SGR parameter string into a ``{class: group}`` state.

    Applies the stacking rules: ``0`` clears everything, other parameters
    apply sequentially and the last one of each class wins.
    """
    state: dict[str, str] = {}
    tokens = spec.split(";")
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "0":
            state.clear()
        elif token in ("38", "48"):
            cls = "fg" if token == "38" else "bg"
            if i + 1 < len(tokens) and tokens[i + 1] == "5":
                state[cls] = ";".join(tokens[i : i + 3])
                i += 3
                continue
            if i + 1 < len(tokens) and tokens[i + 1] == "2":
                state[cls] = ";".join(tokens[i : i + 4])
                i += 4
                continue
            state[cls] = token
        elif token.isdigit() and (30 <= int(token) <= 37 or 90 <= int(token) <= 97):
            state["fg"] = token
        elif token.isdigit() and (40 <= int(token) <= 47 or 100 <= int(token) <= 107):
            state["bg"] = token
        elif token in _ATTR_CLASSES:
            state[token] = token
        else:
            state[f"other:{token}"] = token
        i += 1
    return state


def _sgr_transition(current: dict[str, str], new: dict[str, str]) -> str:
    """Return the minimal escape sequence moving from *current* to *new*."""
    if current == new:
        return ""
    if not new:
        return f"{ESC}0m" if current else ""
    if not current:
        return f"{ESC}{';'.join(new.values())}m"
    if current.keys() - new.keys():
        # Some attribute must be cleared; fold the reset into one sequence.
        return f"{ESC}0;{';'.join(new.values())}m"
    changed = [group for cls, group in new.items() if current.get(cls) != group]
    return f"{ESC}{';'.join(changed)}m" if changed else ""


class Line:
    """Build a terminal string part by part with colors, width and alignment.

    Calling the builder appends content (arguments are converted to ``str``).
    Attribute access with a color name arms that palette color for the next
    call; the color is reset automatically when that call ends, so a color
    always applies to exactly one call::

        str(Line().user("Alice")(" by "))  # "Alice" blue, " by " plain

    ``width`` and ``align`` keyword arguments pad the content of a call by
    display width.  ``str(line)`` finishes the line, restoring default
    colors if any are active.
    """

    def __init__(self, palette: Colors | None = None) -> None:
        self._palette = palette if palette is not None else colors
        self._parts: list[str] = []
        self._active: dict[str, str] = {}
        self._pending: dict[str, str] = {}

    def __getattr__(self, name: str) -> Line:
        if name.startswith("_"):
            raise AttributeError(name)
        spec = getattr(self._palette, name, None)
        if spec is None:
            raise AttributeError(f"unknown color: {name!r}")
        self._pending = _parse_sgr(spec)
        return self

    def __call__(self, *args: Any, width: int = 0, align: str = "left") -> Line:
        text = "".join(str(arg) for arg in args)
        if width:
            text = pad(text, width, align)
        if self._pending != self._active:
            self._parts.append(_sgr_transition(self._active, self._pending))
            self._active = self._pending
        self._parts.append(text)
        self._pending = {}
        return self

    def __str__(self) -> str:
        if self._active:
            return "".join(self._parts) + f"{ESC}0m"
        return "".join(self._parts)
