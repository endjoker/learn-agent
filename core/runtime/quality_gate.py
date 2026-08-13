"""Deterministic quality checks for durable Plan workflows."""
from __future__ import annotations
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from .artifacts import ArtifactStore

@dataclass(frozen=True)
class QualityReport:
    passed: bool
    checks: tuple[dict[str, Any], ...]
    def to_list(self) -> list[dict[str, Any]]: return list(self.checks)

class QualityGate:
    def __init__(self, artifacts: ArtifactStore, *, project_root: str | Path | None = None):
        self.artifacts = artifacts
        from core.config_loader import _find_project_root
        self.project_root = Path(project_root or _find_project_root()).resolve()
    def evaluate(self, criteria, *, session_id: str, text: str = "") -> QualityReport:
        checks=[]
        for index, raw in enumerate(criteria or []):
            item=dict(raw) if isinstance(raw,dict) else {"type":"invalid"}
            kind=str(item.get("type") or "").lower(); required=bool(item.get("required",True)); ok=False
            if kind == "artifact":
                name=str(item.get("name") or ""); artifact_id=str(item.get("artifact_id") or "")
                ok=any((artifact_id and a.artifact_id == artifact_id) or (name and a.name == name) for a in self.artifacts.list(session_id=session_id, limit=1000)); message="artifact found" if ok else "required artifact is missing"
            elif kind in {"text","contains"}:
                expected=str(item.get("contains") or item.get("text") or ""); ok=bool(expected) and expected in text; message="text matched" if ok else "required text was not found"
            elif kind == "command":
                ok,message=self._run_command(item)
            elif kind == "manual":
                ok=bool(item.get("approved",False)); message="manual approval recorded" if ok else "manual approval is required"
            else: message=f"unsupported quality criterion: {kind or 'missing'}"
            checks.append({"index":index,"type":kind,"required":required,"passed":ok,"message":message})
        return QualityReport(all(c["passed"] or not c["required"] for c in checks),tuple(checks))
    def _run_command(self,item):
        argv=item.get("argv")
        if not isinstance(argv,list) or not argv or not all(isinstance(x,str) and x for x in argv): return False,"command criterion requires non-empty argv array"
        exe=Path(argv[0]); allowed=False
        if exe.is_absolute() or any(ch in argv[0] for ch in ("/","\\")):
            exe=(exe if exe.is_absolute() else self.project_root/exe).resolve()
            venv=(self.project_root/".venv"/"Scripts"/"python.exe").resolve()
            allowed=exe == venv
            argv=[str(exe),*argv[1:]]
        elif argv[0].lower()=="git" and argv[1:2] in (["diff"],["status"]): allowed=True
        if not allowed: return False,"command must use project .venv Python or read-only git"
        try:
            run=subprocess.run(argv,cwd=self.project_root,capture_output=True,text=True,timeout=max(1,min(int(item.get("timeout_seconds",300)),600)),shell=False)
        except Exception as exc: return False,f"quality command failed to start: {exc}"
        output=(run.stdout+run.stderr)[-1000:]
        return run.returncode==0,("command passed" if run.returncode==0 else f"command exited {run.returncode}: {output}")
