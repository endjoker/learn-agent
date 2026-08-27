#!/usr/bin/env bash
# JKagent 一键安装脚本
#
# 用法:
#   bash scripts/install.sh              # 安装 + 构建前端 + 完成提示
#   bash scripts/install.sh --run        # 安装完成后直接启动网关
#   bash scripts/install.sh --skip-frontend   # 跳过前端构建(使用仓库自带 static)
#
# 行为:
#   1. 检查 Python ≥3.10 与 Node ≥20.19(构建前端需要)
#   2. 创建/复用 .venv,安装 Python 依赖(完整 extra)
#   3. 构建 Web UI 前端并同步到网关静态目录
#   4. 如 config.json 不存在 → 从 config.example.json 生成最小可用配置
#      (WebUI 设置页可在线改模型/密钥,init 向导变为可选)
#   5. --run 时启动网关,打印 WebUI 地址
#
# 幂等:重复执行安全;已装依赖快速通过。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RUN_AFTER_INSTALL=0
SKIP_FRONTEND=0
for arg in "$@"; do
  case "$arg" in
    --run) RUN_AFTER_INSTALL=1 ;;
    --skip-frontend) SKIP_FRONTEND=1 ;;
    -h|--help)
      sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg(支持 --run / --skip-frontend)"; exit 2 ;;
  esac
done

step() { printf "\n\033[1;34m======== %s ========\033[0m\n" "$*"; }
ok()   { printf "  \033[32m✔\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m⚠\033[0m %s\n" "$*"; }
fail() { printf "  \033[31m✘ %s\033[0m\n" "$*"; exit 1; }

step "1/5 环境检查"

PYTHON_BIN="${PYTHON_BIN:-python3}"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "未找到 python3,请先安装 Python ≥3.10"
PY_VERSION="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
"$PYTHON_BIN" - <<'EOF' || fail "Python 版本过低(需 ≥3.10)"
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
EOF
ok "Python $PY_VERSION"

# venv:不存在则创建;存在则校验可用
if [ ! -x ".venv/bin/python" ]; then
  "$PYTHON_BIN" -m venv .venv || fail "创建虚拟环境失败"
  ok "已创建 .venv"
else
  ok "复用已有 .venv"
fi
# shellcheck disable=SC1091
source .venv/bin/activate

PIP_QUIET="-q"
if [ "${VERBOSE:-0}" = "1" ]; then PIP_QUIET=""; fi

step "2/5 安装 Python 依赖(完整 extra)"
pip install $PIP_QUIET --upgrade pip
# 先尝试可复现锁定;失败(版本漂移/平台差异)回退宽松安装
if [ -f requirements.lock ]; then
  if pip install $PIP_QUIET -r requirements.lock 2>/dev/null; then
    ok "requirements.lock 安装完成"
  else
    warn "锁定安装失败,回退 pyproject 安装"
    pip install $PIP_QUIET -e ".[all]" || fail "Python 依赖安装失败"
    ok "pyproject 安装完成"
  fi
else
  pip install $PIP_QUIET -e ".[all]" || fail "Python 依赖安装失败"
  ok "pyproject 安装完成"
fi
pip install $PIP_QUIET -e . || fail "项目本体安装失败"
ok "jkagent 安装完成(jkagent-gateway 命令可用)"

step "3/5 构建 Web UI 前端"
FRONTEND_DIR="gateway/webui/frontend"
STATIC_INDEX="$ROOT/gateway/webui/static/index.html"
NEED_BUILD=1
# 已有构建产物且引用的 assets 都在 → 跳过(除非 FORCE_FRONTEND=1)
if [ -f "$STATIC_INDEX" ] && [ "$SKIP_FRONTEND" = "0" ] && [ "${FORCE_FRONTEND:-0}" != "1" ]; then
  if "$PYTHON_BIN" - "$STATIC_INDEX" "$ROOT/gateway/webui/static" <<'EOF'
import re, sys
from pathlib import Path
index, assets = Path(sys.argv[1]), Path(sys.argv[2])
html = index.read_text(encoding="utf-8")
missing = [a for a in re.findall(r'assets/([\w.\-]+)', html)
           if not (assets / "assets" / a).exists()]
sys.exit(1 if missing else 0)
EOF
  then
    NEED_BUILD=0
    ok "检测到完整构建产物,跳过前端构建(--skip-frontend 可显式跳过;FORCE_FRONTEND=1 强制)"
  else
    warn "构建产物不完整,重新构建"
  fi
fi
if [ "$SKIP_FRONTEND" = "1" ]; then
  NEED_BUILD=0
  warn "已跳过前端构建(--skip-frontend)"
fi
if [ "$NEED_BUILD" = "1" ]; then
  command -v node >/dev/null 2>&1 || fail "未找到 node(前端构建需要 Node ≥20.19);或使用 --skip-frontend 跳过"
  NODE_VERSION="$(node -p 'process.versions.node')"
  node -e 'process.exit(process.versions.node.split(".")[0] >= 20 ? 0 : 1)' \
    || fail "Node 版本过低(需 ≥20.19,当前 $NODE_VERSION)"
  ok "Node $NODE_VERSION"
  (cd "$FRONTEND_DIR" \
    && [ -f package-lock.json ] && npm ci --no-audit --no-fund \
    && npm run build \
    && npm run sync:static) || fail "前端构建/同步失败"
  ok "前端构建完成并同步到 gateway/webui/static/"
fi

step "4/5 初始化配置"
if [ -f config.json ]; then
  ok "config.json 已存在(保持不动;模型/密钥可在 WebUI 设置页在线修改)"
else
  if [ -f config.example.json ]; then
    cp config.example.json config.json
    ok "已从 config.example.json 生成 config.json(最小可用骨架)"
    warn "模型/密钥尚未配置:启动后打开 WebUI → 设置页在线填写;或运行 python agent.py init 走向导"
  else
    warn "未找到 config.example.json,跳过(运行 python agent.py init 生成)"
  fi
fi

step "5/5 完成"
cat <<EOF

  安装完成 ✅

  启动网关:
      source .venv/bin/activate
      jkagent-gateway run

  然后浏览器访问:  http://127.0.0.1:9120/ui/
  (首次使用在 WebUI「设置」页配置模型与 API Key;会话内 /model 可切换)
EOF

if [ "$RUN_AFTER_INSTALL" = "1" ]; then
  step "启动网关(--run)"
  PORT="${PORT:-9120}"
  echo "  WebUI: http://127.0.0.1:${PORT}/ui/  (Ctrl+C 停止)"
  exec jkagent-gateway run --port "$PORT"
fi
