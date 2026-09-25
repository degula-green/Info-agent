"""Step checkpointing and resume bookkeeping."""

from __future__ import annotations

from datetime import datetime, timezone

from app.kernel.models import Checkpoint, Plan, PlanStep, TaskRecord


def build_checkpoint(task: TaskRecord, plan: Plan, steps: list[PlanStep]) -> Checkpoint:
    completed = [step.step_id for step in sorted(steps, key=lambda item: item.order) if step.status == "succeeded"]
    next_step = next(
        (step.step_id for step in sorted(steps, key=lambda item: item.order) if step.status in {"pending", "ready"}),
        None,
    )
    return Checkpoint(
        task_id=task.task_id,
        plan_id=plan.plan_id,
        plan_version=plan.version,
        completed_step_ids=completed,
        next_step_id=next_step,
        task_status=task.status,
        updated_at=datetime.now(timezone.utc),
    )


def next_pending_step(steps: list[PlanStep]) -> PlanStep | None:
    """Resume point after a crash: first step that is not finished."""

    for step in sorted(steps, key=lambda item: item.order):
        if step.status in {"pending", "ready"}:
            return step
    return None


def all_steps_finished(steps: list[PlanStep]) -> bool:
    return bool(steps) and all(
        step.status in {"succeeded", "skipped"} for step in steps
    )
