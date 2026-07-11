# -*- coding: utf-8 -*-
"""
记忆工具 —— 跨会话长期记忆的检索和权重更新

供 LLM 在 ReAct 循环中调用，用于：
- memory_search:  搜索历史对话记忆
- memory_update:  更新记忆权重（有用/无用）
"""

from tools.base_tool import BaseTool
from typing import Optional


class MemorySearchTool(BaseTool):
    """
    记忆搜索工具 —— 检索历史对话记忆

    LLM 在用户询问"之前讨论过的内容"时调用此工具。
    """

    name: str = "memory_search"
    description: str = (
        "搜索历史对话记忆。"
        "当用户询问之前讨论过的内容、需要回忆上次对话的上下文、"
        "或要查看过去的决策记录时使用。"
        "支持按关键词和日期筛选。"
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词，描述要查找的历史内容。如 '项目架构'、'部署方案'、'用户偏好'",
            },
            "date": {
                "type": "string",
                "description": "按日期筛选（可选），格式 YYYY-MM-DD。不传则搜索全部日期。",
            },
            "limit": {
                "type": "integer",
                "description": "返回结果数量上限（可选），默认 5，最大 20",
            },
        },
        "required": ["query"],
    }

    def __init__(self):
        self._manager = None

    def set_memory_manager(self, manager):
        """注入 MemoryManager 实例（注册时由 register_all_tools 调用）"""
        self._manager = manager

    def execute(self, query: str, date: str = "", limit: int = 5) -> str:
        if self._manager is None:
            return "⏳ 记忆系统未就绪（memory=True 时可用）"

        limit = min(max(1, int(limit)), 20)
        results = self._manager.search(query=query, date=date, limit=limit)

        if not results:
            return (
                "📭 未找到相关记忆。\n"
                "本次对话内容将在结束后自动保存到记忆中。"
            )

        lines = [f"📚 找到 {len(results)} 条相关记忆：\n"]
        for r in results:
            lines.append(
                f"━━━ 记忆 #{r['id']}  [{r['date']}]  "
                f"(权重: {r['weight']}) ━━━\n"
                f"用户问题: {r['user_call']}\n"
                f"内容摘要: {r['summary']}\n"
            )
        lines.append(
            "---\n"
            "💡 如果上述记忆对回答有帮助，可使用 memory_update 工具标记为 useful（权重+1）；"
            "如果没有帮助，可标记为 not_useful（权重-1），帮助系统优化。"
        )
        return "\n".join(lines)


class MemoryUpdateTool(BaseTool):
    """
    记忆权重更新工具 —— 标记记忆有用/无用

    在 memory_search 返回结果后，LLM 判断每条记忆是否对当前回答有帮助，
    然后调用此工具更新权重。权重越高，该记忆在未来的搜索中排名越靠前。
    """

    name: str = "memory_update"
    description: str = (
        "更新记忆的权重。"
        "在使用 memory_search 获取历史记忆后，"
        "如果某条记忆对回答当前问题有帮助，标记为 useful 增加权重；"
        "如果记忆内容与当前问题无关或无用，标记为 not_useful 降低权重。"
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "memory_id": {
                "type": "integer",
                "description": "要更新权重的记忆 ID（来自 memory_search 结果中的 #id）",
            },
            "action": {
                "type": "string",
                "enum": ["useful", "not_useful"],
                "description": "useful=有用（权重+1），not_useful=无用（权重-1）",
            },
        },
        "required": ["memory_id", "action"],
    }

    def __init__(self):
        self._manager = None

    def set_memory_manager(self, manager):
        """注入 MemoryManager 实例"""
        self._manager = manager

    def execute(self, memory_id: int, action: str) -> str:
        if self._manager is None:
            return "⏳ 记忆系统未就绪"

        delta = 1 if action == "useful" else -1
        success = self._manager.update_weight(memory_id, delta)

        if not success:
            return f"❌ 未找到记忆 ID: {memory_id}"

        action_text = "增加" if delta > 0 else "减少"
        return f"✅ 记忆 #{memory_id} 权重已{action_text}（{'有用' if delta > 0 else '无用'}，权重变化 {'+' if delta > 0 else ''}{delta}）"
