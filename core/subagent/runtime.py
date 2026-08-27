"""Durable-friendly one-shot and continuable child agent orchestration."""
from __future__ import annotations
import asyncio
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional
from uuid import uuid4
from core.runtime.models import RuntimeEvent, TaskStatus, utc_now

class SubagentMode(str, Enum):
    ONE_SHOT = "one-shot"
    CONTINUABLE = "continuable"

@dataclass
class SubagentReport:
    child_id: str
    parent_session_id: str
    mode: SubagentMode
    status: str
    summary: str = ""
    artifact_ids: list[str] = field(default_factory=list)
    task_id: str | None = None
    created_at: str = field(default_factory=utc_now)
    completed_at: str | None = None

    def to_dict(self):
        data = asdict(self); data["mode"] = self.mode.value; return data

class SubagentRuntime:
    """Root creates direct children only; children never receive this runtime."""
    def __init__(self, store, *, submit: Callable[..., Awaitable[str]], wait: Callable[..., Awaitable[Any]], cancel: Callable[..., Awaitable[None]], publish: Optional[Callable[[str, dict], None]] = None):
        self.store, self.submit, self.wait, self.cancel = store, submit, wait, cancel
        self.publish = publish or (lambda _event, _data: None)
        self._reports: dict[str, SubagentReport] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    async def create(self, *, parent_session_id: str, parent_session_key: str, prompt: str, mode: str = "one-shot", parent_is_root: bool = True, metadata: dict | None = None) -> SubagentReport:
        if not parent_is_root: raise PermissionError("only the root agent may create a child")
        mode_value = SubagentMode(mode)
        child_id = f"child_{uuid4().hex}"
        report = SubagentReport(child_id=child_id, parent_session_id=parent_session_id, mode=mode_value, status="requested")
        self._reports[child_id] = report
        team_id = (metadata or {}).get("team_id", f"team_{parent_session_id}")
        team = self.store.get_team(team_id)
        if team is None:
            now = report.created_at
            team = {"team_id": team_id, "goal_id": None, "session_id": parent_session_id,
                    "status": "active", "created_at": now, "updated_at": now, "team_json": {}}
            self.store.save_team(team, RuntimeEvent.create("subagent.team_created", session_id=parent_session_id, data={"team_id": team_id}))
        member = {"agent_id": child_id, "team_id": team_id, "status": "requested", "parent_agent_id": "root", "created_at": report.created_at, "updated_at": report.created_at, **report.to_dict()}
        self.store.create_team_member(member, RuntimeEvent.create("subagent.requested", session_id=parent_session_id, data={"child_session_id": child_id, "mode": mode_value.value}), max_children=int((metadata or {}).get("max_children", 4)))
        self.publish("subagent.requested", report.to_dict())
        task_id = await self.submit(session_id=child_id, session_key=f"subagent:{parent_session_key}:{child_id}", prompt=prompt, parent_task_id=(metadata or {}).get("parent_task_id"), metadata={"parent_session_id": parent_session_id, "child_session_id": child_id, "subagent_mode": mode_value.value, "team_id": team_id, **(metadata or {})})
        report.task_id, report.status = task_id, "running"
        member = self.store.get_team_member(child_id)
        if member:
            member.update({"status": report.status, "updated_at": utc_now(), **report.to_dict()})
            self.store.save_team_member(member, RuntimeEvent.create("subagent.started", session_id=parent_session_id, task_id=task_id, data={"child_session_id": child_id}))
        else:
            self.store.append_event(RuntimeEvent.create("subagent.started", session_id=parent_session_id, task_id=task_id, data={"child_session_id": child_id}))
        self._tasks[child_id] = asyncio.create_task(self._observe(child_id), name=f"subagent-{child_id}")
        return report

    async def _observe(self, child_id: str):
        report = self._reports[child_id]
        try:
            result = await self.wait(report.task_id)
            report.status = "completed" if result.status is TaskStatus.COMPLETED else result.status.value
            report.summary = (result.summary or result.visible_text or "")[:4000]
            report.artifact_ids = list(result.artifact_ids)
        except asyncio.CancelledError:
            report.status = "cancelled"; raise
        except Exception as exc:
            report.status, report.summary = "failed", str(exc)
        finally:
            report.completed_at = utc_now()
            payload = report.to_dict()
            member = self.store.get_team_member(child_id)
            if member:
                member.update({"status": report.status, "updated_at": report.completed_at, **payload})
                self.store.save_team_member(member, RuntimeEvent.create("subagent.reported", session_id=report.parent_session_id, task_id=report.task_id, data=payload))
            else:
                self.store.append_event(RuntimeEvent.create("subagent.reported", session_id=report.parent_session_id, task_id=report.task_id, data=payload))
            self.publish("subagent.reported", payload)

    async def continue_child(self, child_id: str, prompt: str) -> SubagentReport:
        report = self.get_report(child_id)
        if not report: raise KeyError(child_id)
        if report.mode is not SubagentMode.CONTINUABLE: raise ValueError("one-shot children cannot be continued")
        if report.status == "running": raise RuntimeError("child is still running")
        task_id = await self.submit(
            session_id=child_id, session_key=f"subagent:continued:{child_id}", prompt=prompt,
            metadata={"parent_session_id": report.parent_session_id, "child_session_id": child_id,
                      "subagent_mode": report.mode.value, "continuation": True})
        report.task_id, report.status, report.completed_at = task_id, "running", None
        member = self.store.get_team_member(child_id)
        if member:
            member.update({"status": report.status, "updated_at": utc_now(), **report.to_dict()})
            self.store.save_team_member(member, RuntimeEvent.create("subagent.continued", session_id=report.parent_session_id, task_id=task_id, data=report.to_dict()))
        self._tasks[child_id] = asyncio.create_task(self._observe(child_id), name=f"subagent-{child_id}")
        self.publish("subagent.continued", report.to_dict())
        return report

    async def wait_report(self, child_id: str, timeout: float | None = None) -> SubagentReport:
        """Wait until the child's observation task settles, then return its report.

        Used by the parent-facing tool/command so the caller can actually consume
        the child's outcome instead of fire-and-forget submission.
        """
        report = self.get_report(child_id)
        if report is None:
            raise KeyError(child_id)
        task = self._tasks.get(child_id)
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout)
            except asyncio.CancelledError:
                # 子任务被取消（cancel_child）：_observe 已把状态写成 cancelled。
                return self.get_report(child_id)
        return self.get_report(child_id)

    async def cancel_child(self, child_id: str, reason: str = "parent_cancelled") -> None:
        report = self.get_report(child_id)
        if not report: return
        if report.task_id: await self.cancel(report.task_id, reason=reason)
        task = self._tasks.get(child_id)
        if task and not task.done(): task.cancel()

    def archive_child(self, child_id: str) -> SubagentReport:
        report = self.get_report(child_id)
        if report is None:
            raise KeyError(child_id)
        if report.mode is SubagentMode.CONTINUABLE and report.status == "running":
            raise ValueError("running continuable child cannot be archived")
        if report.status == "running":
            raise ValueError("running child cannot be archived")
        report.status = "archived"
        member = self.store.get_team_member(child_id)
        if member:
            member.update({"status": "archived", "updated_at": utc_now(), **report.to_dict()})
            self.store.save_team_member(member, RuntimeEvent.create("subagent.archived", session_id=report.parent_session_id, task_id=report.task_id, data=report.to_dict()))
        self.publish("subagent.archived", report.to_dict())
        return report

    def reconcile_interrupted(self) -> int:
        """重启对账：把无重放源的 INTERRUPTED 子任务对应成员置为 interrupted。

        Gateway 重启时 recover_interrupted(requeue=False) 会把进行中的子任务
        （source=subagent，无重放投递上下文）置为 INTERRUPTED，但其
        team_members 状态仍停留在 running，前端会永远显示“运行中”。
        此处扫描全部成员，把任务已 INTERRUPTED 的 running/requested 成员
        置为 interrupted，解除永久 running。应在 Gateway 启动后调用一次。
        """
        reconciled = 0
        offset = 0
        while True:
            members = self.store.list_all_team_members(limit=1000, offset=offset)
            if not members:
                break
            for member in members:
                if member.get("status") not in {"running", "requested"}:
                    continue
                task_id = member.get("task_id")
                if not task_id:
                    continue
                snapshot = self.store.get_task(task_id)
                if snapshot is None or snapshot.record.status is not TaskStatus.INTERRUPTED:
                    continue
                if not {"agent_id", "team_id", "parent_agent_id", "created_at"}.issubset(member):
                    continue
                member["status"] = "interrupted"
                member["updated_at"] = utc_now()
                self.store.save_team_member(
                    member,
                    RuntimeEvent.create(
                        "subagent.interrupted_reconciled",
                        session_id=member.get("parent_session_id") or member.get("session_id"),
                        task_id=task_id,
                        data={"child_session_id": member.get("child_id"), "task_id": task_id}))
                reconciled += 1
            if len(members) < 1000:
                break
            offset += len(members)
        return reconciled

    async def cancel_parent(self, parent_session_id: str, reason: str = "parent_cancelled") -> None:
        for report in self.list_reports(parent_session_id):
            if report.status == "running":
                await self.cancel_child(report.child_id, reason)

    @staticmethod
    def _report_from_member(member: dict) -> SubagentReport | None:
        try:
            return SubagentReport(child_id=member["child_id"], parent_session_id=member["parent_session_id"],
                mode=SubagentMode(member["mode"]), status=member["status"], summary=member.get("summary", ""),
                artifact_ids=list(member.get("artifact_ids") or []), task_id=member.get("task_id"),
                created_at=member.get("created_at") or utc_now(), completed_at=member.get("completed_at"))
        except (KeyError, ValueError):
            return None

    def get_report(self, child_id: str) -> SubagentReport | None:
        report = self._reports.get(child_id)
        if report is not None: return report
        member = self.store.get_team_member(child_id)
        report = self._report_from_member(member) if member else None
        if report is not None: self._reports[child_id] = report
        return report

    def list_reports(self, parent_session_id: str):
        reports = {child_id: report for child_id, report in self._reports.items() if report.parent_session_id == parent_session_id}
        for team in self.store.list_teams(parent_session_id):
            for member in self.store.list_team_members(team["team_id"]):
                report = self._report_from_member(member)
                if report and report.parent_session_id == parent_session_id: reports.setdefault(report.child_id, report)
        return list(reports.values())
