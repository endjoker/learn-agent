from __future__ import annotations

from pathlib import Path

import pytest

from gateway.webui.api_workspace import _workspace_path
from gateway.webui.workspace_models import Workspace


def _workspace(root: Path) -> Workspace:
    return Workspace(workspace_id="ws_files", name="files", project_path=str(root))


def test_workspace_path_allows_root_and_nested_files(tmp_path: Path) -> None:
    root, target = _workspace_path(_workspace(tmp_path), "src/main.py")
    assert root == tmp_path.resolve()
    assert target == (tmp_path / "src/main.py").resolve()


def test_workspace_path_rejects_parent_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes workspace root"):
        _workspace_path(_workspace(tmp_path), "../secret.txt")


def test_workspace_path_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "outside"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError, match="escapes workspace root"):
        _workspace_path(_workspace(tmp_path), "outside/secret.txt")
