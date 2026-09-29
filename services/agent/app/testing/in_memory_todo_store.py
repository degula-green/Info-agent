"""In-memory TodoStore: tests and local runs without PostgreSQL.

Behaviour mirrors the PostgreSQL implementation, including the idempotency
key lookup that keeps a retried Step from writing a second row.
"""

from __future__ import annotations

import threading

from app.kernel.events import utcnow
from app.kernel.models import TodoRecord

MUTABLE_FIELDS = ("title", "due_at", "due_expression", "timezone", "notes", "status")


class InMemoryTodoStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.todos: dict[str, TodoRecord] = {}
        self.idempotency: dict[str, str] = {}

    def create_todo(self, todo: TodoRecord) -> TodoRecord:
        with self._lock:
            if todo.idempotency_key:
                existing_id = self.idempotency.get(todo.idempotency_key)
                if existing_id is not None:
                    return self.todos[existing_id].model_copy(deep=True)
                self.idempotency[todo.idempotency_key] = todo.todo_id
            self.todos[todo.todo_id] = todo.model_copy(deep=True)
            return self.todos[todo.todo_id].model_copy(deep=True)

    def get_todo(self, todo_id: str) -> TodoRecord | None:
        with self._lock:
            todo = self.todos.get(todo_id)
            return todo.model_copy(deep=True) if todo else None

    def find_todo_by_idempotency_key(self, idempotency_key: str) -> TodoRecord | None:
        with self._lock:
            todo_id = self.idempotency.get(idempotency_key)
            return self.get_todo(todo_id) if todo_id else None

    def list_todos(
        self,
        owner_user_id: str,
        *,
        statuses: list[str] | None = None,
        limit: int = 200,
    ) -> list[TodoRecord]:
        with self._lock:
            wanted = {status for status in (statuses or []) if status}
            items = [
                todo
                for todo in self.todos.values()
                if todo.owner_user_id == owner_user_id
                and (not wanted or todo.status in wanted)
            ]
        # Overdue items are not special-cased: an unfinished to-do still shows.
        items.sort(key=lambda item: (item.due_at is None, item.due_at or item.created_at))
        return [item.model_copy(deep=True) for item in items[: max(0, limit)]]

    def update_todo(
        self,
        todo_id: str,
        *,
        owner_user_id: str,
        changes: dict,
    ) -> TodoRecord | None:
        with self._lock:
            todo = self.todos.get(todo_id)
            if todo is None or todo.owner_user_id != owner_user_id:
                return None
            updated = todo.model_copy(deep=True)
            for field in MUTABLE_FIELDS:
                if field in changes:
                    setattr(updated, field, changes[field])
            updated.updated_at = utcnow()
            if updated.status == "done" and updated.completed_at is None:
                updated.completed_at = updated.updated_at
            if updated.status != "done":
                updated.completed_at = None
            self.todos[todo_id] = updated
            return updated.model_copy(deep=True)

    def delete_todo(self, todo_id: str, *, owner_user_id: str) -> bool:
        with self._lock:
            todo = self.todos.get(todo_id)
            if todo is None or todo.owner_user_id != owner_user_id:
                return False
            if todo.idempotency_key:
                self.idempotency.pop(todo.idempotency_key, None)
            self.todos.pop(todo_id, None)
            return True

