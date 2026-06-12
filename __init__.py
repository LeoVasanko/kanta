"""Project-root shim package for local development layout.

This forwards imports to the inner `kanta/` package directory so
`from kanta import ...` works when running tests from the workspace root.
"""

import importlib
from pathlib import Path

_inner_pkg = Path(__file__).with_name("kanta")
if str(_inner_pkg) not in __path__:
    __path__.append(str(_inner_pkg))

_pkg = importlib.import_module(".kanta", __name__)
__all__ = list(getattr(_pkg, "__all__", ()))
globals().update({name: getattr(_pkg, name) for name in __all__})
