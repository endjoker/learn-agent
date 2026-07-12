"""
任务清单 —— 会话级任务拆分与进度管理

用于复杂任务处理：
  1. 从 LLM 的 PLAN 输出解析出有序任务列表
  2. 管理任务状态推进（pending → in_progress → completed）
  3. 渲染为 system prompt 片段，让 LLM 感知进度
  4. 序列化到 session JSON 实现持久化

典型流程：
  task_list = TaskList.from_plan_text("[1] 分析需求\n[2] 设计接口")
  task_list.get_current()       → TaskItem(id=1, status="in_progress")
  task_list.mark_done(1, "完成")
  task_list.get_current()       → TaskItem(id=2, status="in_progress")
  task_list.is_all_done()       → False
"""

import re
from typing import List, Optional, Dict


class TaskItem:
    """单个任务单元"""

    def __init__(
        self,
        id: int,
        description: str,
        status: str = "pending",
        result: str = "",
    ):
        self.id = id
        self.description = description
        self.status = status        # pending / in_progress / completed / skipped / failed
        self.result = result

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "description": self.description,
            "status": self.status,
            "result": self.result,
        }

    @staticmethod
    def from_dict(data: Dict) -> "TaskItem":
        return TaskItem(
            id=data["id"],
            description=data["description"],
            status=data.get("status", "pending"),
            result=data.get("result", ""),
        )

    def __repr__(self) -> str:
        return f"TaskItem({self.id}, {self.description[:20]}, {self.status})"


class TaskList:
    """任务清单"""

    def __init__(self, tasks: Optional[List[TaskItem]] = None):
        self.tasks: List[TaskItem] = tasks or []
        self._current_idx: int = 0
        # 初始化第一个任务的 status
        if self.tasks and self.tasks[0].status == "pending":
            self.tasks[0].status = "in_progress"

    # ============================================================
    # 状态管理
    # ============================================================

    def get_current(self) -> Optional[TaskItem]:
        """返回当前待执行的任务"""
        if 0 <= self._current_idx < len(self.tasks):
            return self.tasks[self._current_idx]
        return None

    def mark_done(self, task_id: int, result: str = "") -> bool:
        """
        标记指定任务完成，自动推进到下一个。

        返回:
            True 表示成功推进到下一个任务
        """
        for task in self.tasks:
            if task.id == task_id:
                task.status = "completed"
                task.result = result
                # 推进到下一个
                next_idx = self._current_idx + 1
                if next_idx < len(self.tasks):
                    self._current_idx = next_idx
                    self.tasks[next_idx].status = "in_progress"
                    return True
                else:
                    # 全部完成
                    self._current_idx = len(self.tasks)
                    return False
        return False

    def mark_skipped(self, task_id: int, reason: str = ""):
        """跳过指定任务，如果是当前任务则自动推进到下一个"""
        for i, task in enumerate(self.tasks):
            if task.id == task_id:
                task.status = "skipped"
                task.result = reason
                # 如果是当前任务，推进到下一个
                if i == self._current_idx:
                    next_idx = self._current_idx + 1
                    if next_idx < len(self.tasks):
                        self._current_idx = next_idx
                        self.tasks[next_idx].status = "in_progress"
                break

    def is_all_done(self) -> bool:
        """是否所有任务都已完成（含跳过、失败）"""
        if not self.tasks:
            return True
        done = {"completed", "skipped", "failed"}
        return all(t.status in done for t in self.tasks)

    @property
    def progress_str(self) -> str:
        """进度字符串：'2/5'"""
        done = sum(1 for t in self.tasks if t.status in ("completed", "skipped", "failed"))
        return f"{done}/{len(self.tasks)}"

    @property
    def total(self) -> int:
        return len(self.tasks)

    # ============================================================
    # 渲染
    # ============================================================

    STATUS_ICONS = {
        "pending": "□",
        "in_progress": "▶",
        "completed": "✅",
        "skipped": "⏭️",
        "failed": "❌",
    }

    def to_prompt_section(self) -> str:
        """
        渲染为 system prompt 片段（注入到 <SYSTEM_TASK_LIST> 标签中）。

        LLM 通过这个片段了解当前进度和下一步工作。
        """
        if not self.tasks:
            return ""

        lines = ["<SYSTEM_TASK_LIST>"]
        lines.append(f"【当前任务清单 - 进度 {self.progress_str}】")

        for task in self.tasks:
            icon = self.STATUS_ICONS.get(task.status, "□")
            marker = " ← 当前任务" if task.status == "in_progress" else ""
            line = f"  {icon} [{task.id}] {task.description}{marker}"
            if task.result:
                line += f" → {task.result}"
            lines.append(line)

        lines.append("")
        lines.append("执行规则：")
        lines.append("- 聚焦当前任务（标记 ▶），完成后再推进下一个")
        lines.append("- 每完成一步，先输出 COMPLETE_TASK: N（N 为任务序号，可跟结果摘要）")
        lines.append("  然后继续下一步，如任务已完成则输出最终汇总")
        lines.append("- 所有任务完成后输出最终汇总")
        lines.append("</SYSTEM_TASK_LIST>")

        return "\n".join(lines)

    # ============================================================
    # 序列化
    # ============================================================

    def to_dict(self) -> Dict:
        return {
            "tasks": [t.to_dict() for t in self.tasks],
            "current_idx": self._current_idx,
        }

    @staticmethod
    def from_dict(data: Dict) -> "TaskList":
        tl = TaskList(
            tasks=[TaskItem.from_dict(t) for t in data.get("tasks", [])],
        )
        tl._current_idx = data.get("current_idx", 0)
        return tl

    # ============================================================
    # 从 LLM 的 PLAN 文本创建
    # ============================================================

    @staticmethod
    def from_plan_text(plan_text: str) -> "TaskList":
        """
        解析 LLM 输出的 PLAN 文本，生成任务清单。

        支持格式：
          [1] 分析需求
          [2] 设计接口
          或
          1. 分析需求
          2. 设计接口
        """
        tasks = []
        # 匹配 [N] 或 N. 开头的内容
        pattern = re.compile(r"(?:\[(\d+)\]|(\d+)\.)\s*(.+)")
        for line in plan_text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            m = pattern.search(line)
            if m:
                num = int(m.group(1) or m.group(2))
                desc = m.group(3).strip()
                tasks.append(TaskItem(id=num, description=desc))

        return TaskList(tasks=tasks)

    def to_summary(self) -> str:
        """
        渲染为完成总结（供任务结束时的终端输出，非 system prompt）
        """
        if not self.tasks:
            return "（无任务）"
        done = sum(1 for t in self.tasks if t.status == "completed")
        skipped = sum(1 for t in self.tasks if t.status == "skipped")
        lines = [f"📋 任务完成总结（共 {len(self.tasks)} 项，✅ {done} 完成"]
        if skipped:
            lines[-1] += f"，⏭️  {skipped} 跳过"
        lines[-1] += "）："
        for task in self.tasks:
            icon = self.STATUS_ICONS.get(task.status, "□")
            line = f"  {icon} [{task.id}] {task.description}"
            if task.result:
                line += f" → {task.result[:120]}"
            lines.append(line)
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"TaskList({len(self.tasks)} tasks, current={self._current_idx})"
