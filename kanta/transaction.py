"""Transaction context manager for Kanta."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any

from kanta.diff import compute_diff
from kanta.exceptions import DataIntegrityError
from kanta.logging import log_change
from kanta.serialization import restore_data_in_place, struct_to_dict

_logger = logging.getLogger(__name__)


@contextmanager
def transaction(
    impl,
    action: str,
    *,
    user: str | None = None,
    user_display: str | None = None,
    resolver: Any = None,
):
    """Wrap writes in a transaction and yield the live db object."""
    if impl.in_transaction:
        raise RuntimeError(
            "Nested or simultaneous transactions are not supported "
            "(don't await inside transactions)."
        )

    current_dict = struct_to_dict(impl.data, serializer=impl.serializer)

    if current_dict != impl.statedict:
        is_bootstrap = action in {"bootstrap"}
        if not (is_bootstrap and not impl.statedict):
            diff = compute_diff(impl.statedict, current_dict)
            if diff:
                _logger.critical(
                    "Database state modified outside of transaction! "
                    "This indicates a bug where changes occurred without a transaction wrapper.\n"
                    "Changes detected: %s",
                    diff,
                )
                raise DataIntegrityError(
                    "Database state modified outside of transaction",
                    db_path=impl.db_path,
                    action=action,
                    diff=diff,
                )

    impl.in_transaction = True
    impl.transaction_snapshot = current_dict

    try:
        yield impl.data
        new_dict = struct_to_dict(impl.data, serializer=impl.serializer)
        diff = compute_diff(impl.statedict, new_dict)
        if diff:
            impl.queue_change(action, new_dict, user=user)
            log_change(action, diff, user_display, impl.statedict, resolver)
            impl.statedict = new_dict
    except Exception:
        _logger.warning("Transaction '%s' failed, rolling back changes", action)
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
