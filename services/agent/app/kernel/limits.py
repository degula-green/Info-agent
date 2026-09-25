"""Execution budgets for the Agent runtime."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ExecutionLimits:
    max_steps: int = 8
    max_execution_seconds: float = 300.0
    max_step_attempts: int = 3
    retry_backoff_seconds: list[float] = field(default_factory=lambda: [1.0, 5.0, 20.0])
    approval_expires_seconds: float = 3600.0
    lease_seconds: float = 120.0

    @classmethod
    def from_settings(cls, settings) -> "ExecutionLimits":
        return cls(
            max_steps=int(settings.task_max_steps),
            max_execution_seconds=float(settings.task_max_execution_seconds),
            max_step_attempts=int(settings.task_max_retries),
            retry_backoff_seconds=list(settings.retry_backoff_seconds),
            approval_expires_seconds=float(settings.approval_expires_seconds),
            lease_seconds=float(settings.task_lease_seconds),
        )

    def backoff_for(self, attempt: int) -> float:
        if not self.retry_backoff_seconds:
            return 0.0
        index = min(max(attempt - 1, 0), len(self.retry_backoff_seconds) - 1)
        return float(self.retry_backoff_seconds[index])
