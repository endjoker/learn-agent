import re

# === 1. Fix permission.py ===
with open("core/permission.py", "r", encoding="utf-8") as f:
    content = f.read()

# Add _is_operation_external method before check()
old_check = '''    def is_workspace_trusted(self) -> bool:
        """当前是否已信任工作区"""
        return self._workspace_trusted

    def check(self, tool_name: str, params: dict = None) -> str:
        """
        检查工具调用的权限

        参数:
            tool_name: 工具名称
            params:    工具参数字典

        返回:
            "allow" — 直接执行
            "ask"   — 需要用户确认
            "deny"  — 直接拒绝
        """
        if params is None:
            params = {}

        rule = self._rules.get(tool_name)

        # 未设置规则 → 默认 ask（安全优先）
        if rule is None:
            return ALLOW if self._workspace_trusted else ASK

        # 固定权限
        if isinstance(rule, str):
            if self._workspace_trusted and rule == ASK:
                return ALLOW  # 工作区信任：将固定 ask 升为 allow
            return rule

        # 动态规则（回调函数）
        if callable(rule):
            level = rule(tool_name, params)
            # 工作区信任：将 ask 升为 allow（deny 保持）
            if self._workspace_trusted and level == ASK:
                return ALLOW
            return level

        return ASK'''

new_check = '''    def is_workspace_trusted(self) -> bool:
        """当前是否已信任工作区"""
        return self._workspace_trusted

    def _is_operation_external(self, params: dict) -> bool:
        """
        检查操作是否涉及工作区外的路径。

        检查 params 中所有可能的路径参数（file_path/path/dest/paths），
        只要有一个在工作区外，就返回 True。
        """
        for key in ("file_path", "path", "dest"):
            val = params.get(key)
            if val:
                p = resolve_path(val, self.workspace)
                if not is_within_workspace(p, self.workspace):
                    return True
        paths_list = params.get("paths", [])
        if isinstance(paths_list, list):
            for p_str in paths_list:
                p = resolve_path(p_str, self.workspace)
                if not is_within_workspace(p, self.workspace):
                    return True
        return False

    def check(self, tool_name: str, params: dict = None) -> str:
        """
        检查工具调用的权限

        参数:
            tool_name: 工具名称
            params:    工具参数字典

        返回:
            "allow" — 直接执行
            "ask"   — 需要用户确认
            "deny"  — 直接拒绝
        """
        if params is None:
            params = {}

        rule = self._rules.get(tool_name)

        # 未设置规则 → 默认 ask（安全优先）
        if rule is None:
            return ALLOW if self._workspace_trusted else ASK

        # 固定权限
        if isinstance(rule, str):
            if self._workspace_trusted and rule == ASK:
                return ALLOW  # 无路径的固定 ask（如 python/http）升为 allow
            return rule

        # 动态规则（回调函数）
        if callable(rule):
            level = rule(tool_name, params)
            # 工作区信任：ask 升级时区分路径是否在工作区内
            if self._workspace_trusted and level == ASK:
                if self._is_operation_external(params):
                    return ASK  # 工作区外操作仍需确认
                return ALLOW  # 工作区内操作放行
            return level

        return ASK'''

if old_check in content:
    content = content.replace(old_check, new_check)
    with open("core/permission.py", "w", encoding="utf-8") as f:
        f.write(content)
    print("permission.py: OK")
else:
    print("permission.py: PATTERN NOT FOUND")
