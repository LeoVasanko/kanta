"""Unified decorator-based callback registry for Kanta.

Callbacks are registered once and invoked with arguments filled by their
annotation types.  Unknown arguments are only permitted when they have a
default value.

Log formatters are a special case: they are called per value being rendered
and receive the value plus an optional ``path`` string.  They return
``str | None``; ``None`` means "fall through to the next formatter".
"""

from __future__ import annotations

import inspect
import types
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Union, get_args, get_origin

from kanta.exceptions import DatabaseError
from kanta.migrations import MigrationResult

DictPre = Annotated[dict, "pre"]
DictPost = Annotated[dict, "post"]


class LogFmt:
    """Base class for stateful logfmt callbacks.

    Subclasses only need to override :meth:`resolve`.  The framework injects
    ``previous_state`` and ``current_state`` through ``__init__``.
    """

    def __init__(
        self,
        previous: DictPre | None = None,
        current: DictPost | None = None,
    ) -> None:
        self.previous_state = previous
        self.current_state = current

    def __call__(self, value: Any, path: str) -> str | None:
        return self.resolve(value, path)

    def resolve(self, value: Any, path: str) -> str | None:
        """Resolve *value* into a display string.

        The default implementation returns ``None`` so other formatters are
        tried.
        """
        return None


@dataclass
class InjectionContext:
    """Runtime values available for injection into callbacks."""

    kanta: Any | None = None
    data: Any | None = None
    error: DatabaseError | None = None
    previous_state: dict | None = None
    current_state: dict | None = None
    migration_result: MigrationResult | None = None


@dataclass
class _CallbackRegistration:
    callback: Callable[..., Any]
    params: list[tuple[str, type]]
    is_async: bool = False


@dataclass
class _LogFmtFunctionSpec:
    callback: Callable[..., Any]
    value_type: type | Any
    has_path: bool
    inject_params: list[tuple[str, type]]
    path: str | None = None


@dataclass
class _LogFmtClassSpec:
    cls: type[LogFmt]
    inject_params: list[tuple[str, type]]
    path: str | None = None


class CallbackRegistry:
    """Stores and invokes callbacks, resolving arguments by annotation."""

    def __init__(
        self,
        *,
        kanta_class: type | None = None,
        data_type: type | None = None,
    ) -> None:
        self._kanta_class = kanta_class
        self._data_type = data_type
        self._callbacks: dict[str, list[_CallbackRegistration]] = {
            "bootstrap": [],
            "fatal_error": [],
            "logmigr": [],
        }
        self._logfmt_callbacks: list[_LogFmtFunctionSpec | _LogFmtClassSpec] = []

    def register(
        self,
        kind: str,
        callback: Callable[..., Any],
        *,
        path: str | None = None,
    ) -> Callable[..., Any]:
        """Register *callback* for *kind* after validating its signature."""
        if kind == "logfmt":
            if inspect.isclass(callback):
                self._logfmt_callbacks.append(
                    self._validate_logfmt_class(callback, path=path)
                )
            else:
                self._logfmt_callbacks.append(
                    self._validate_logfmt_function(callback, path=path)
                )
            return callback

        if kind not in self._callbacks:
            raise ValueError(f"unknown callback kind: {kind}")

        if inspect.isclass(callback):
            raise TypeError(f"{kind} callbacks must be functions, not classes")
        if not callable(callback):
            raise TypeError(f"{kind} callback must be callable")

        params = self._validate_function(callback, kind)
        is_async = inspect.iscoroutinefunction(callback)

        self._callbacks[kind].append(
            _CallbackRegistration(
                callback=callback,
                params=params,
                is_async=is_async,
            )
        )
        return callback

    async def invoke(
        self,
        kind: str,
        ctx: InjectionContext,
        *,
        on_error: Callable[[Exception, Callable[..., Any]], bool | None] | None = None,
    ) -> list[Any]:
        """Invoke all callbacks of *kind* with arguments from *ctx*.

        If *on_error* is provided it is called for each exception and may return
        ``False`` to stop invoking further callbacks.  When *on_error* is not
        provided the first exception is raised immediately.
        """
        results: list[Any] = []
        for reg in self._callbacks[kind]:
            try:
                kwargs = self._build_kwargs(reg.params, ctx)
                result = reg.callback(**kwargs)
                if inspect.isawaitable(result):
                    result = await result
                results.append(result)
            except Exception as exc:
                if on_error is None:
                    raise
                if on_error(exc, reg.callback) is False:
                    break
        return results

    def has(self, kind: str) -> bool:
        """Return True if any callback of *kind* is registered."""
        if kind == "logfmt":
            return bool(self._logfmt_callbacks)
        return bool(self._callbacks[kind])

    def build_logfmt(self, ctx: InjectionContext) -> Callable[[Any, str], str | None]:
        """Build a chained formatter from registered logfmt callbacks."""
        formatters: list[tuple[Callable[[Any, str], str | None], str | None]] = []
        for spec in self._logfmt_callbacks:
            if isinstance(spec, _LogFmtClassSpec):
                kwargs = self._build_kwargs(spec.inject_params, ctx)
                instance: Callable[[Any, str], str | None] = spec.cls(**kwargs)
                formatters.append((instance, spec.path))
            else:
                kwargs = self._build_kwargs(spec.inject_params, ctx)

                def make_formatter(
                    callback: Callable[..., Any] = spec.callback,
                    value_type: type | Any = spec.value_type,
                    has_path: bool = spec.has_path,
                    state_kwargs: dict[str, Any] = kwargs,
                ) -> Callable[[Any, str], str | None]:
                    def formatter(value: Any, path: str) -> str | None:
                        if value_type is str and not isinstance(value, str):
                            return None
                        call_kwargs = dict(state_kwargs)
                        if has_path:
                            call_kwargs["path"] = path
                        return callback(value, **call_kwargs)

                    return formatter

                formatters.append((make_formatter(), spec.path))

        def format_value(value: Any, path: str) -> str | None:
            for fn, pattern in formatters:
                if pattern is not None and path != pattern:
                    continue
                resolved = fn(value, path)
                if resolved is not None:
                    return resolved
            return None

        return format_value

    def _validate_function(
        self,
        callback: Callable[..., Any],
        kind: str,
    ) -> list[tuple[str, type]]:
        sig = inspect.signature(callback)
        params: list[tuple[str, type]] = []
        for name, param in sig.parameters.items():
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                raise TypeError(
                    f"{kind} callback {callback.__name__} must not use "
                    f"*args or **kwargs"
                )

            if param.annotation is inspect.Parameter.empty:
                if param.default is inspect.Parameter.empty:
                    raise TypeError(
                        f"{kind} callback {callback.__name__} has parameter "
                        f"'{name}' without an annotation or default value"
                    )
                continue

            ann = self._resolve_raw_annotation(param.annotation, callback)
            if not self._is_allowed(kind, ann):
                if param.default is inspect.Parameter.empty:
                    raise TypeError(
                        f"{kind} callback {callback.__name__} has parameter "
                        f"'{name}' with unsupported annotation {ann!r}. "
                        f"Allowed: {self._allowed_message(kind)}"
                    )
                continue

            params.append((name, ann))

        return params

    def _validate_logfmt_function(
        self,
        callback: Callable[..., Any],
        *,
        path: str | None = None,
    ) -> _LogFmtFunctionSpec:
        sig = inspect.signature(callback)
        if inspect.iscoroutinefunction(callback):
            raise TypeError("logfmt callbacks must not be async")

        params = list(sig.parameters.items())
        if not params:
            raise TypeError(
                f"logfmt callback {callback.__name__} must accept a value parameter"
            )

        value_name, value_param = params[0]
        if value_param.kind in (value_param.VAR_POSITIONAL, value_param.VAR_KEYWORD):
            raise TypeError(
                f"logfmt callback {callback.__name__} must not use *args or **kwargs"
            )
        if value_param.annotation is inspect.Parameter.empty:
            raise TypeError(
                f"logfmt callback {callback.__name__} value parameter "
                f"'{value_name}' must be annotated as str or Any"
            )
        value_ann = self._resolve_raw_annotation(value_param.annotation, callback)
        value_bare = self._unwrap_optional(value_ann)
        if value_bare is str:
            value_type = str
        elif value_bare is Any:
            value_type = Any
        else:
            raise TypeError(
                f"logfmt callback {callback.__name__} value parameter "
                f"'{value_name}' must be annotated as str or Any, got {value_ann!r}"
            )

        has_path = False
        inject_params: list[tuple[str, type]] = []
        for name, param in params[1:]:
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                raise TypeError(
                    f"logfmt callback {callback.__name__} must not use "
                    f"*args or **kwargs"
                )
            if param.annotation is inspect.Parameter.empty:
                if param.default is inspect.Parameter.empty:
                    raise TypeError(
                        f"logfmt callback {callback.__name__} has parameter "
                        f"'{name}' without an annotation or default value"
                    )
                continue

            ann = self._resolve_raw_annotation(param.annotation, callback)
            if name == "path" and self._unwrap_optional(ann) is str:
                has_path = True
                continue
            if self._is_allowed("logfmt", ann):
                inject_params.append((name, ann))
                continue
            if param.default is inspect.Parameter.empty:
                raise TypeError(
                    f"logfmt callback {callback.__name__} has parameter "
                    f"'{name}' with unsupported annotation {ann!r}. "
                    f"Allowed: str path, {self._allowed_message('logfmt')}"
                )

        if sig.return_annotation is not inspect.Signature.empty:
            return_ann = self._resolve_raw_annotation(sig.return_annotation, callback)
            if not self._is_optional_str(return_ann):
                raise TypeError(
                    f"logfmt callback {callback.__name__} must return str | None, "
                    f"got {return_ann!r}"
                )

        return _LogFmtFunctionSpec(
            callback=callback,
            value_type=value_type,
            has_path=has_path,
            inject_params=inject_params,
            path=path,
        )

    def _validate_logfmt_class(
        self,
        cls: type[LogFmt],
        *,
        path: str | None = None,
    ) -> _LogFmtClassSpec:
        if not issubclass(cls, LogFmt):
            raise TypeError("logfmt classes must inherit from kanta.callbacks.LogFmt")
        if inspect.iscoroutinefunction(cls.__init__):
            raise TypeError("logfmt class __init__ must not be async")

        sig = inspect.signature(cls.__init__)
        inject_params: list[tuple[str, type]] = []
        first = True
        for name, param in sig.parameters.items():
            if first and name == "self":
                first = False
                continue
            first = False

            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                raise TypeError(
                    f"logfmt class {cls.__name__}.__init__ must not use "
                    f"*args or **kwargs"
                )
            if param.annotation is inspect.Parameter.empty:
                if param.default is inspect.Parameter.empty:
                    raise TypeError(
                        f"logfmt class {cls.__name__}.__init__ has parameter "
                        f"'{name}' without an annotation or default value"
                    )
                continue

            ann = self._resolve_raw_annotation(param.annotation, cls.__init__)
            if self._is_allowed("logfmt", ann):
                inject_params.append((name, ann))
                continue
            if param.default is inspect.Parameter.empty:
                raise TypeError(
                    f"logfmt class {cls.__name__}.__init__ has parameter "
                    f"'{name}' with unsupported annotation {ann!r}. "
                    f"Allowed: {self._allowed_message('logfmt')}"
                )

        resolve = getattr(cls, "resolve", None)
        if resolve is None:
            raise TypeError(f"logfmt class {cls.__name__} must define a resolve method")
        resolve_sig = inspect.signature(resolve)
        resolve_params = list(resolve_sig.parameters.items())
        if not resolve_params or resolve_params[0][0] != "self":
            raise TypeError(
                f"logfmt class {cls.__name__}.resolve must have 'self' as first parameter"
            )
        if len(resolve_params) < 2:
            raise TypeError(
                f"logfmt class {cls.__name__}.resolve must accept a value parameter"
            )

        value_name, value_param = resolve_params[1]
        value_ann = self._resolve_raw_annotation(value_param.annotation, resolve)
        value_bare = self._unwrap_optional(value_ann)
        if value_bare not in (inspect.Parameter.empty, str, Any):
            raise TypeError(
                f"logfmt class {cls.__name__}.resolve value parameter "
                f"'{value_name}' must be annotated as str or Any, got {value_ann!r}"
            )

        path_found = False
        for name, param in resolve_params[2:]:
            path_ann = self._resolve_raw_annotation(param.annotation, resolve)
            path_bare = self._unwrap_optional(path_ann)
            if name == "path" and path_bare in (inspect.Parameter.empty, str):
                path_found = True
                break
        if not path_found:
            raise TypeError(
                f"logfmt class {cls.__name__}.resolve must accept a 'path: str' parameter"
            )

        if resolve_sig.return_annotation is not inspect.Signature.empty:
            return_ann = self._resolve_raw_annotation(
                resolve_sig.return_annotation, resolve
            )
            if not self._is_optional_str(return_ann):
                raise TypeError(
                    f"logfmt class {cls.__name__}.resolve must return str | None, "
                    f"got {return_ann!r}"
                )

        return _LogFmtClassSpec(cls=cls, inject_params=inject_params, path=path)

    def _build_kwargs(
        self,
        params: list[tuple[str, type]],
        ctx: InjectionContext,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        for name, ann in params:
            value = self._resolve_annotation(ann, ctx)
            if value is _UNRESOLVED:
                raise RuntimeError(f"no value available for annotation {ann!r}")
            kwargs[name] = value
        return kwargs

    def _is_allowed(self, kind: str, ann: Any) -> bool:
        bare = self._unwrap_optional(ann)
        if self._matches_state_annotation(bare, "pre"):
            return kind == "logfmt"
        if self._matches_state_annotation(bare, "post"):
            return kind == "logfmt"
        if bare is DatabaseError:
            return kind == "fatal_error"
        if bare is MigrationResult:
            return kind == "logmigr"
        if self._data_type is not None and bare is self._data_type:
            return kind == "bootstrap"
        if self._kanta_class is not None and bare is self._kanta_class:
            return kind in {"bootstrap", "fatal_error", "logfmt", "logmigr"}
        return False

    def _allowed_message(self, kind: str) -> str:
        parts: list[str] = []
        if kind == "bootstrap":
            if self._data_type is not None:
                parts.append(self._data_type.__name__)
        if kind in {"bootstrap", "fatal_error", "logfmt"}:
            if self._kanta_class is not None:
                parts.append(self._kanta_class.__name__)
        if kind == "fatal_error":
            parts.append("DatabaseError")
        if kind == "logmigr":
            parts.append("MigrationResult")
        if kind == "logfmt":
            parts.append("Annotated[dict, 'pre']")
            parts.append("Annotated[dict, 'post']")
        return ", ".join(parts) if parts else "none"

    def _resolve_annotation(self, ann: Any, ctx: InjectionContext) -> Any:
        bare = self._unwrap_optional(ann)
        if self._matches_state_annotation(bare, "pre"):
            return ctx.previous_state
        if self._matches_state_annotation(bare, "post"):
            return ctx.current_state
        if bare is DatabaseError:
            return ctx.error
        if bare is MigrationResult:
            return ctx.migration_result
        if self._data_type is not None and bare is self._data_type:
            return ctx.data
        if self._kanta_class is not None and bare is self._kanta_class:
            return ctx.kanta
        return _UNRESOLVED

    def _resolve_raw_annotation(
        self,
        raw_ann: Any,
        callback: Callable[..., Any],
    ) -> Any:
        if isinstance(raw_ann, str):
            try:
                return eval(raw_ann, callback.__globals__)
            except Exception as exc:
                raise TypeError(
                    f"could not resolve annotation {raw_ann!r} for "
                    f"{callback.__name__}: {exc}"
                ) from exc
        return raw_ann

    @staticmethod
    def _matches_state_annotation(ann: Any, marker: str) -> bool:
        origin = get_origin(ann)
        if origin is not Annotated:
            return False
        args = get_args(ann)
        if not args:
            return False
        return args[0] is dict and marker in args[1:]

    @staticmethod
    def _unwrap_optional(ann: Any) -> Any:
        origin = get_origin(ann)
        if origin not in (Union, types.UnionType):
            return ann
        args = [arg for arg in get_args(ann) if arg is not type(None)]
        return args[0] if len(args) == 1 else ann

    @staticmethod
    def _is_optional_str(ann: Any) -> bool:
        origin = get_origin(ann)
        if origin not in (Union, types.UnionType):
            return ann is str
        args = get_args(ann)
        return type(None) in args and any(arg is str for arg in args)


class _Unresolved:
    pass


_UNRESOLVED = _Unresolved()
