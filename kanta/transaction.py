"""Transaction context manager for Kanta."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from kanta.diff import diff
from kanta.exceptions import DataIntegrityError
from kanta.callbacks import InjectionContext
from kanta.logging import _USER_PATH, LogEvent, emit_event, transaction_logger
from kanta.serialization import restore_data_in_place, struct_to_dict

_logger = logging.getLogger(__name__)


def _build_logfmt(impl, previous: dict, current: dict):
    """Build the logfmt chain for a state transition."""
    return impl.callback_registry.build_logfmt(
        InjectionContext(
            previous_state=previous,
            current_state=current,
            kanta=impl._kanta,
        )
    )


def _resolve_user(logfmt, user: str | None) -> str | None:
    """Resolve *user* for display via the logfmt chain (raw as fallback)."""
    if user is None:
        return None
    resolved = logfmt(user, _USER_PATH)
    return resolved if resolved is not None else user


@contextmanager
def transaction(
    impl,
    action: str,
    *,
    user: str | None = None,
    extra: Any = None,
    mtime: bool | datetime = True,
    log: bool | logging.Logger = True,
    logdiff: bool = True,
):
    """Wrap writes in a transaction and yield the live db object."""
    if impl.readonly:
        raise DataIntegrityError(
            "Cannot start transaction in read-only mode",
            db_path=impl.filename,
            action=action,
        )

    if impl.in_transaction:
        raise RuntimeError(
            "Nested or simultaneous transactions are not supported "
            "(don't await inside transactions)."
        )

    current_dict = struct_to_dict(impl.data, serializer=impl.serializer)

    if current_dict != impl.statedict:
        is_bootstrap = action in {"bootstrap"}
        if not (is_bootstrap and not impl.statedict):
            delta = diff(impl.statedict, current_dict)
            if delta:
                _logger.critical(
                    "Database state modified outside of transaction! "
                    "This indicates a bug where changes occurred without a transaction wrapper.\n"
                    "Changes detected: %s",
                    delta,
                )
                raise DataIntegrityError(
                    "Database state modified outside of transaction",
                    db_path=impl.db_path,
                    action=action,
                    diff=delta,
                )

    impl.in_transaction = True
    impl.transaction_snapshot = current_dict

    try:
        yield impl.data
        new_dict = struct_to_dict(impl.data, serializer=impl.serializer)
        delta = diff(impl.statedict, new_dict)
        if delta:
            if impl.callback_registry.has("validate"):
                impl.callback_registry.invoke_sync(
                    "validate",
                    InjectionContext(data=impl.data, kanta=impl._kanta),
                )
            previous = impl.statedict
            record = impl.queue_change(action, new_dict, user=user, mtime=mtime)
            if record is not None:
                if log is not False:
                    logfmt = _build_logfmt(impl, previous, new_dict)
                    logger = (
                        log if isinstance(log, logging.Logger) else transaction_logger
                    )
                    emit_event(
                        LogEvent(
                            kind="change",
                            logger=logger,
                            kanta=impl._kanta,
                            action=action,
                            user=_resolve_user(logfmt, user),
                            extra=extra,
                            diff=record.diff,
                            previous=previous,
                            current=new_dict,
                            logfmt=logfmt,
                            show_diff=logdiff,
                        ),
                        impl.callback_registry.logemit_handlers,
                    )
    except Exception as exc:
        resolved_user = None
        if user is not None:
            logfmt = _build_logfmt(impl, impl.statedict, impl.statedict)
            resolved_user = _resolve_user(logfmt, user)
        emit_event(
            LogEvent(
                kind="aborted",
                logger=transaction_logger,
                level=logging.WARNING,
                kanta=impl._kanta,
                action=action,
                user=resolved_user,
                extra=extra,
                error=exc,
            ),
            impl.callback_registry.logemit_handlers,
        )
        if impl.transaction_snapshot is not None:
            impl.data = restore_data_in_place(
                impl.data,
                impl.transaction_snapshot,
                impl.data_type,
                serializer=impl.serializer,
            )
        raise
    finally:
        impl.in_transaction = False
        impl.transaction_snapshot = None
