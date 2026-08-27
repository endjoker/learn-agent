# -*- coding: utf-8 -*-
"""Glue readonly 档 search query 凭据打码测试。

readonly 档允许 search 外发查询；query 若携带上下文中的 API Key/Token 会
原样发给外部搜索引擎。Glue._wrap_search_credential_sanitizer 给 search 工具
包一层 guard.sanitize_output（复用 SECRET_PATTERNS，不复制实现），本文件
验证打码生效、非字符串参数透传、包装幂等。
"""

import unittest
from types import SimpleNamespace

from gateway.webui.glue import Glue
from tools.registry import ToolRegistry


class _StubSearchTool:
    """最小 search 工具桩（不发起网络请求）。"""

    def __init__(self):
        self.name = "search"
        self.received = None
        self._cred_sanitized = False

    def execute(self, query: str, max_results: int = 5) -> str:
        self.received = {"query": query, "max_results": max_results}
        return f"results for {query}"

    # registry 只需要 get_tool/list_tool_names
    def _register_into(self, registry: ToolRegistry) -> None:
        registry._tools[self.name] = self


def _agent_with_search() -> SimpleNamespace:
    tool = _StubSearchTool()
    registry = ToolRegistry()
    tool._register_into(registry)
    return SimpleNamespace(tool_registry=registry), tool


class ReadonlySearchSanitizerTests(unittest.TestCase):
    def test_query_credentials_are_masked(self):
        agent, tool = _agent_with_search()
        wrapped = Glue._wrap_search_credential_sanitizer(agent)
        self.assertEqual(wrapped, 1)
        out = tool.execute(
            query="how to use key sk-abc12345678901234567890 in python",
            max_results=3)
        # 打码后 query 不再含明文凭据；非字符串参数原样透传
        self.assertEqual(tool.received["max_results"], 3)
        self.assertNotIn("sk-abc12345678901234567890", tool.received["query"])
        self.assertIn("sk-****", tool.received["query"])
        self.assertIn("sk-****", out)

    def test_wrapping_is_idempotent(self):
        agent, tool = _agent_with_search()
        self.assertEqual(Glue._wrap_search_credential_sanitizer(agent), 1)
        first_execute = tool.execute
        # 模式来回切换重复调用 → 不叠加包装
        self.assertEqual(Glue._wrap_search_credential_sanitizer(agent), 0)
        self.assertIs(tool.execute, first_execute)

    def test_noop_when_search_tool_absent(self):
        agent, _ = _agent_with_search()
        agent.tool_registry._tools.clear()
        self.assertEqual(Glue._wrap_search_credential_sanitizer(agent), 0)


if __name__ == "__main__":
    unittest.main()
