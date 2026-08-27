"""Focused tests for the complete prompt preview payload."""

from types import SimpleNamespace

from gateway.webui.prompt_preview import build_preview
from tools import ToolRegistry
from tools.base_tool import BaseTool


class PreviewTool(BaseTool):
    name = "preview_tool"
    description = "用于验证预览工具 schema。"
    parameters = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }

    def execute(self, **kwargs):
        return kwargs.get("value", "")


def test_preview_returns_runtime_prompt_and_provider_schema(tmp_path):
    prompt_dir = tmp_path / "prompt"
    prompt_dir.mkdir()
    (prompt_dir / "AGENT.md").write_text("项目规则内容", encoding="utf-8")
    (prompt_dir / "MEMORY.md").write_text("跨会话记忆内容", encoding="utf-8")

    registry = ToolRegistry()
    registry.register_tool(PreviewTool())
    profile = SimpleNamespace(
        name="preview-agent",
        system_prompt="Agent 自定义身份",
        tools=["preview_tool"],
        skills=[],
        mcp_servers=[],
    )
    result = build_preview(
        profile,
        tool_registry=registry,
        framework_root=str(tmp_path),
        project_root=str(tmp_path),
        working_directory=str(tmp_path),
        memory_path=str(tmp_path / "workspace-memory"),
        memory_instruction="优先检索记忆",
    )

    assert "<SYSTEM_STATIC_CONTEXT>" in result["full_prompt"]
    assert "<SYSTEM_DYNAMIC_CONTEXT>" in result["full_prompt"]
    assert "项目规则内容" in result["full_prompt"]
    assert "跨会话记忆内容" in result["full_prompt"]
    assert "工作区长期记忆" in result["full_prompt"]
    assert result["expected_prompt_hash"].startswith("sha256:")
    assert result["provider_tools_count"] >= 1
    assert any(
        item.get("function", {}).get("name")
        for item in result["provider_tools"]
    )
