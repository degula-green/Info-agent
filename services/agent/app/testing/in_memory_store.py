from __future__ import annotations

from collections import defaultdict

from app.kernel.models import Observation, Plan, TaskEnvelope


class InMemoryTaskStore:
    def __init__(self) -> None:
        self._tasks: dict[str, TaskEnvelope] = {}
        self._plans: dict[str, Plan] = {}
        self._observations: dict[str, list[Observation]] = defaultdict(list)

    def save_task(self, task: TaskEnvelope) -> None:
        self._tasks[task.task_id] = task.model_copy(deep=True)

    def get_task(self, task_id: str) -> TaskEnvelope | None:
        task = self._tasks.get(task_id)
        return task.model_copy(deep=True) if task is not None else None

    def save_plan(self, plan: Plan) -> None:
        self._plans[plan.plan_id] = plan.model_copy(deep=True)

    def get_plan(self, plan_id: str) -> Plan | None:
        plan = self._plans.get(plan_id)
        return plan.model_copy(deep=True) if plan is not None else None

    def save(self, observation: Observation) -> None:
        stored = observation.model_copy(deep=True)
        existing = self._observations[observation.task_id]
        for index, item in enumerate(existing):
            if item.observation_id == observation.observation_id:
                existing[index] = stored
                break
        else:
            existing.append(stored)

    def list_for_task(self, task_id: str) -> list[Observation]:
        return [observation.model_copy(deep=True) for observation in self._observations.get(task_id, [])]
