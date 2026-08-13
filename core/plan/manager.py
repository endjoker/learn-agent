"""Plan lifecycle, dependency scheduling and durable progress updates."""

from __future__ import annotations

from typing import Any, Optional

from core.runtime.models import RuntimeEvent, utc_now
from core.runtime.sqlite_store import RuntimeStore

from .models import Plan, PlanStatus, PlanTask, PlanTaskStatus


class PlanManager:
    def __init__(self, store: RuntimeStore):
        self.store = store

    def create_preview(self, session_id: str, raw_plan: dict[str, Any], *, source_prompt: str,
                       title: str = "执行方案") -> Plan:
        tasks = self._tasks_from_raw(raw_plan)
        plan = Plan.create(session_id=session_id, title=title, tasks=tasks,
                           source_prompt=source_prompt)
        self._save(plan, "plan.created")
        plan.transition(PlanStatus.AWAITING_APPROVAL)
        self._save(plan, "plan.awaiting_approval")
        return plan

    def get(self, plan_id: str) -> Optional[Plan]:
        data = self.store.get_plan(plan_id)
        return Plan.from_dict(data) if data else None

    def list(self, session_id: str, *, limit: int = 100,
             include_archived: bool = False) -> list[Plan]:
        plans = [Plan.from_dict(item) for item in self.store.list_plans(session_id, limit=limit)]
        return plans if include_archived else [plan for plan in plans if not plan.archived_at]

    def list_recoverable(self, *, limit: int = 1000) -> list[Plan]:
        statuses = {PlanStatus.APPROVED.value, PlanStatus.ACTIVE.value}
        return [Plan.from_dict(item) for item in self.store.list_plans_by_status(statuses, limit=limit)]

    def archive_terminal_for_session(self, session_id: str) -> int:
        """Hide completed/cancelled Plan cards without deleting audit data.

        A clear operation must never cancel or alter a live Plan.  Terminal
        snapshots remain durable for audit/recovery but are omitted from the
        normal WebUI list after they have been cleared by the user.
        """
        count = 0
        for plan in self.list(session_id, limit=1000, include_archived=True):
            if not plan.is_terminal or plan.archived_at:
                continue
            now = utc_now()
            plan.archived_at = now
            plan.updated_at = now
            self._save(plan, "plan.archived")
            count += 1
        return count

    def archive_terminal(self, plan_id: str) -> Plan:
        """Hide one terminal Plan card without deleting its audit record."""
        plan = self._require(plan_id)
        if not plan.is_terminal:
            raise ValueError("only a completed, failed, or cancelled Plan can be cleared")
        if plan.archived_at:
            return plan
        now = utc_now()
        plan.archived_at = now
        plan.updated_at = now
        self._save(plan, "plan.archived")
        return plan

    def approve(self, plan_id: str, *, actor: str = "user") -> Plan:
        plan = self._require(plan_id)
        plan.transition(PlanStatus.APPROVED)
        plan.approval_actor = actor
        plan.approved_at = utc_now()
        self._save(plan, "plan.approved", {"actor": actor})
        return plan

    def activate(self, plan_id: str) -> Plan:
        plan = self._require(plan_id)
        plan.transition(PlanStatus.ACTIVE)
        self._refresh_ready(plan)
        self._refresh_terminal(plan)
        event = "plan.active" if plan.status is PlanStatus.ACTIVE else f"plan.{plan.status.value}"
        self._save(plan, event)
        return plan

    def pause(self, plan_id: str) -> Plan:
        plan = self._require(plan_id)
        plan.transition(PlanStatus.PAUSED)
        self._save(plan, "plan.paused")
        return plan

    def cancel(self, plan_id: str) -> Plan:
        plan = self._require(plan_id)
        plan.transition(PlanStatus.CANCELLED)
        for task in plan.tasks:
            if not task.is_terminal:
                task.status = PlanTaskStatus.CANCELLED
                task.updated_at = utc_now()
        self._save(plan, "plan.cancelled")
        return plan

    def ready_tasks(self, plan_id: str) -> list[PlanTask]:
        plan = self._require(plan_id)
        if plan.status is not PlanStatus.ACTIVE:
            return []
        self._refresh_ready(plan)
        return [task for task in plan.tasks if task.status is PlanTaskStatus.READY]

    def assign_task(self, plan_id: str, plan_task_id: str, task_id: str) -> Plan:
        plan = self._require(plan_id)
        if plan.status is not PlanStatus.ACTIVE:
            raise ValueError("only active Plans can assign tasks")
        task = plan.task(plan_task_id)
        if task.status is not PlanTaskStatus.READY:
            raise ValueError("only ready PlanTasks can be assigned")
        task.status = PlanTaskStatus.ASSIGNED
        task.task_id = task_id
        task.attempts += 1
        task.updated_at = utc_now()
        self._save(plan, "plan.task_assigned", {"plan_task_id": task.plan_task_id, "task_id": task_id})
        return plan

    def start_task(self, plan_id: str, plan_task_id: str) -> Plan:
        plan = self._require(plan_id)
        task = plan.task(plan_task_id)
        if task.status is not PlanTaskStatus.ASSIGNED:
            raise ValueError("only assigned PlanTasks can start")
        task.status = PlanTaskStatus.IN_PROGRESS
        task.updated_at = utc_now()
        self._save(plan, "plan.task_started", {"plan_task_id": task.plan_task_id, "task_id": task.task_id})
        return plan

    def finish_task(self, plan_id: str, plan_task_id: str, *, success: bool,
                    summary: str = "", blocked_reason: Optional[dict[str, Any]] = None,
                    artifact_ids: Optional[list[str]] = None,
                    quality_report: Optional[list[dict[str, Any]]] = None) -> Plan:
        plan = self._require(plan_id)
        if plan.status in PlanStatus.terminal():
            return plan
        if plan.status not in {PlanStatus.ACTIVE, PlanStatus.PAUSED}:
            raise ValueError("only active or paused Plans can finish a running task")
        task = plan.task(plan_task_id)
        if task.status not in {PlanTaskStatus.ASSIGNED, PlanTaskStatus.IN_PROGRESS, PlanTaskStatus.WAITING}:
            raise ValueError("PlanTask is not running")
        task.result_summary = summary
        if artifact_ids is not None:
            task.artifact_ids = list(dict.fromkeys(artifact_ids))
        if quality_report is not None:
            task.quality_report = list(quality_report)
        task.updated_at = utc_now()
        if success:
            task.status = PlanTaskStatus.COMPLETED
            task.blocked_reason = None
            event = "plan.task_completed"
        elif task.attempts <= task.retry_limit:
            task.status = PlanTaskStatus.READY
            task.blocked_reason = None
            event = "plan.task_retry_ready"
        else:
            task.status = PlanTaskStatus.BLOCKED if blocked_reason else PlanTaskStatus.FAILED
            task.blocked_reason = blocked_reason
            event = "plan.task_blocked" if blocked_reason else "plan.task_failed"
            self._block_downstream(plan, task.plan_task_id, task.result_summary)
        self._refresh_ready(plan)
        # A paused Plan records the terminal result of its already-running task,
        # but it must remain paused until the user explicitly resumes it.
        if plan.status is PlanStatus.ACTIVE:
            self._refresh_terminal(plan)
        self._save(plan, event, {"plan_task_id": task.plan_task_id, "task_id": task.task_id})
        return plan

    def _require(self, plan_id: str) -> Plan:
        plan = self.get(plan_id)
        if plan is None:
            raise KeyError(plan_id)
        return plan

    def _save(self, plan: Plan, event_type: str, data: Optional[dict[str, Any]] = None) -> None:
        payload = {"plan_id": plan.plan_id, "status": plan.status.value, "progress": plan.progress}
        payload.update(data or {})
        self.store.save_plan(plan.to_dict(), RuntimeEvent.create(event_type, session_id=plan.session_id, data=payload))

    @staticmethod
    def _tasks_from_raw(raw_plan: dict[str, Any]) -> list[PlanTask]:
        if not isinstance(raw_plan, dict) or not isinstance(raw_plan.get("steps"), list):
            raise ValueError("plan must contain a steps array")
        tasks: list[PlanTask] = []
        previous: Optional[str] = None
        for index, raw in enumerate(raw_plan["steps"], start=1):
            if not isinstance(raw, dict):
                raise ValueError("each plan step must be an object")
            description = raw.get("description")
            if not isinstance(description, str) or not description.strip():
                raise ValueError("each plan step needs a description")
            task_id = str(raw.get("id") or f"step_{index}").strip()
            depends = raw.get("depends_on")
            if depends is None:
                depends = [previous] if previous else []
            if not isinstance(depends, list) or not all(isinstance(item, str) for item in depends):
                raise ValueError("depends_on must be a list of task IDs")
            task = PlanTask(
                plan_task_id=task_id, description=description, depends_on=tuple(depends),
                weight=raw.get("weight", 1), acceptance=raw.get("acceptance", []),
                retry_limit=raw.get("retry_limit", 0),
            )
            tasks.append(task)
            previous = task_id
        if not tasks:
            raise ValueError("plan needs at least one step")
        return tasks

    @staticmethod
    def _refresh_ready(plan: Plan) -> None:
        by_id = {task.plan_task_id: task for task in plan.tasks}
        changed = False
        for task in plan.tasks:
            if task.status is not PlanTaskStatus.PENDING:
                continue
            if all(by_id[dependency].status is PlanTaskStatus.COMPLETED for dependency in task.depends_on):
                task.status = PlanTaskStatus.READY
                task.updated_at = utc_now()
                changed = True
        if changed:
            plan.updated_at = utc_now()

    @staticmethod
    def _block_downstream(plan: Plan, failed_task_id: str, reason: str) -> None:
        dependents = {task.plan_task_id for task in plan.tasks if failed_task_id in task.depends_on}
        while dependents:
            current = dependents.pop()
            task = plan.task(current)
            if task.status is PlanTaskStatus.PENDING:
                task.status = PlanTaskStatus.BLOCKED
                task.blocked_reason = {"type": "dependency_failed", "message": reason or failed_task_id}
                task.updated_at = utc_now()
                dependents.update(item.plan_task_id for item in plan.tasks if current in item.depends_on)

    @staticmethod
    def _refresh_terminal(plan: Plan) -> None:
        if not all(task.is_terminal for task in plan.tasks):
            return
        if all(task.status is PlanTaskStatus.COMPLETED for task in plan.tasks):
            plan.status = PlanStatus.COMPLETED
        else:
            plan.status = PlanStatus.FAILED
        plan.updated_at = utc_now()

