"""Retired persistence shim (C2 契约② 收尾，2026-08)。

文件转录（sessions/*.json）已停写：``MessageStore.save_session()`` 在
file_persistence_enabled=False（现默认且无生产调用方开启）时是 no-op，
本适配器的 save() 因此同样为 no-op。保留类与 append/messages 委托仅为
兼容既有 import（core.agent_runtime.__init__ 导出）；会话持久化的唯一
权威是 gateway 的 SQLite 统一会话（ConversationStore），独立 Agent 明确
不持久化。不要再在新代码中调用 save()。
"""
from __future__ import annotations


class PersistenceAdapter:
    """Deprecated: transcript persistence retired; save() is a no-op."""

    def __init__(self, agent):
        self.agent = agent

    def save(self) -> None:
        # 持久化退役：不再触发任何落盘（save_session 亦为 no-op）。
        return None

    def append(self, message: dict) -> None:
        self.agent.messages.append(message)

    def messages(self):
        return self.agent.messages
