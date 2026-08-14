"""Executor factory — select LocalExecutor or DaytonaExecutor via EXECUTOR (I10)."""

from __future__ import annotations

from packages.domain.enums import ExecutorKind
from packages.executors.base import ExecutorUnavailableError
from packages.executors.daytona import DaytonaExecutor
from packages.executors.local import LocalExecutor


def get_executor(
    *,
    settings: object | None = None,
    timeout_seconds: int = 20,
) -> LocalExecutor | DaytonaExecutor:
    """Build the executor named by settings.executor / EXECUTOR.

    Never imports Atlas secrets into DaytonaExecutor — only the Daytona API key
    is passed through when EXECUTOR=daytona.
    """
    if settings is None:
        from apps.api.settings import get_settings

        settings = get_settings()

    kind = getattr(settings, "executor", ExecutorKind.LOCAL)
    target_slots = int(getattr(settings, "daytona_target_sandboxes", 8))

    if kind == ExecutorKind.DAYTONA:
        api_key = getattr(settings, "daytona_api_key", None)
        if not api_key:
            raise ExecutorUnavailableError(
                "DAYTONA_API_KEY is required when EXECUTOR=daytona"
            )
        return DaytonaExecutor(
            api_key,
            target_slots=target_slots,
            timeout_seconds=timeout_seconds,
        )

    return LocalExecutor(target_slots=target_slots, timeout_seconds=timeout_seconds)
