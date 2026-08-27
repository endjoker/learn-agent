#!/usr/bin/env bash
# 全量验证入口：后端 + 前端 + 静态检查。
# 用法: bash scripts/verify_all.sh          （完整跑）
#       SKIP_FRONTEND=1 bash scripts/verify_all.sh
set -u -o pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
FAIL=0

step() { echo; echo "======== $1 ========"; }

step "1/5 后端: pytest (.venv)"
.venv/bin/python -m pytest tests/ -q 2>&1 | tail -4
[ "${PIPESTATUS[0]}" -ne 0 ] && FAIL=1

step "2/5 后端: ruff (运行时错误类)"
.venv/bin/python -m ruff check core gateway tools memory skills agent.py || FAIL=1

step "3/5 前端: vitest"
if [ "${SKIP_FRONTEND:-0}" != "1" ]; then
  ( cd gateway/webui/frontend && npm run test 2>&1 | tail -4 )
  [ "${PIPESTATUS[0]}" -ne 0 ] && FAIL=1

  step "4/5 前端: tsc"
  ( cd gateway/webui/frontend && npx tsc -b ) || FAIL=1

  step "5/5 前端: eslint (零警告)"
  ( cd gateway/webui/frontend && npx eslint . --max-warnings=0 2>&1 | tail -3 )
  [ "${PIPESTATUS[0]}" -ne 0 ] && FAIL=1
fi

echo
if [ "$FAIL" -eq 0 ]; then echo "✅ 全部通过"; else echo "❌ 存在失败项"; fi
exit $FAIL
