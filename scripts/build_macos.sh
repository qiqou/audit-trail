#!/usr/bin/env bash
# 审迹 macOS Apple Silicon 一键构建。
# 正式发布固定：macOS 14、Python 3.14.6、Node.js 24.19.0、pnpm 11.5.0。
# 设 AUDIT_TRAIL_BUILD_MODE=candidate 可在较新 macOS/Node 生成明确标识的候选包，
# 但它不能替代 macOS 14 + Node 24.19.0 的正式发布验证。

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

fail() { printf '失败：%s\n' "$1" >&2; exit 1; }
step() { printf '\n==> %s\n' "$1"; }

OFFICIAL_MACOS_MAJOR="14"
OFFICIAL_NODE_VERSION="24.19.0"
BUILD_MODE="${AUDIT_TRAIL_BUILD_MODE:-release}"
[[ "$BUILD_MODE" == "release" || "$BUILD_MODE" == "candidate" ]] \
  || fail "AUDIT_TRAIL_BUILD_MODE 仅支持 release 或 candidate"
IS_CANDIDATE=0
mark_candidate() {
  IS_CANDIDATE=1
  [[ "$BUILD_MODE" == "candidate" ]] || fail "$1；如仅需当前机器候选包，请显式设置 AUDIT_TRAIL_BUILD_MODE=candidate"
  printf '提示：%s；将生成候选包，不能替代正式目标机验证。\n' "$1" >&2
}

[[ "$(uname -s)" == "Darwin" ]] || fail "此脚本只能在 macOS 上执行"
MACOS_VERSION="$(sw_vers -productVersion)"
MACOS_MAJOR="${MACOS_VERSION%%.*}"
[[ "$MACOS_MAJOR" -ge "$OFFICIAL_MACOS_MAJOR" ]] || fail "构建机至少需要 macOS $OFFICIAL_MACOS_MAJOR"
[[ "$MACOS_MAJOR" == "$OFFICIAL_MACOS_MAJOR" ]] || mark_candidate "当前 macOS $MACOS_VERSION，不是正式验证环境 macOS $OFFICIAL_MACOS_MAJOR"
[[ "$(uname -m)" == "arm64" ]] || fail "发布包仅支持 Apple Silicon（arm64）"

command -v python3.14 >/dev/null || fail "未找到 Python 3.14.6"
PYVER=$(python3.14 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
[[ "$PYVER" == "3.14" ]] || fail "需要 Python 3.14.x（当前 $PYVER）"
command -v node >/dev/null || fail "未找到 Node.js $OFFICIAL_NODE_VERSION"
NODE_VERSION="$(node --version | sed 's/^v//')"
[[ "$NODE_VERSION" == "$OFFICIAL_NODE_VERSION" ]] || mark_candidate "当前 Node.js $NODE_VERSION，不是锁定构建版本 $OFFICIAL_NODE_VERSION"
command -v pnpm >/dev/null || fail "未找到 pnpm 11.5.0"
[[ "$(pnpm --version)" == "11.5.0" ]] || fail "需要 pnpm 11.5.0"

VENV_DIR="$ROOT_DIR/.venv-macos-arm64"
PYTHON_BIN="$VENV_DIR/bin/python"

step "创建独立 Python 3.14.6 虚拟环境"
[[ -x "$PYTHON_BIN" ]] || python3.14 -m venv "$VENV_DIR"
[[ "$("$PYTHON_BIN" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')" == "3.14.6" ]] \
  || fail "现有 .venv-macos-arm64 不是 Python 3.14.6；请删除该目录后重建"

step "安装带哈希的锁定依赖"
"$PYTHON_BIN" -m pip install --disable-pip-version-check --require-hashes -r requirements-dev.txt

step "构建 Vue 前端"
pnpm --dir frontend-v3 install --frozen-lockfile
pnpm --dir frontend-v3 build

step "执行质量门禁"
"$VENV_DIR/bin/ruff" check .
"$VENV_DIR/bin/pytest" tests/ -q --disable-warnings

step "PyInstaller 打包 Apple Silicon 应用"
"$VENV_DIR/bin/pyinstaller" --noconfirm 审迹.spec
APP_PATH="$ROOT_DIR/dist/审迹.app"
[[ -d "$APP_PATH" ]] || fail "未生成审迹.app"

step "生成安装包与发布清单"
ARCHIVE_NAME="审迹-macos-arm64.zip"
if [[ "$IS_CANDIDATE" == "1" ]]; then
  ARCHIVE_NAME="审迹-macos-arm64-candidate-macos${MACOS_MAJOR}-node${NODE_VERSION%%.*}.zip"
fi
rm -f "dist/$ARCHIVE_NAME"
ditto -c -k --sequesterRsrc --keepParent "$APP_PATH" "dist/$ARCHIVE_NAME"
{
  printf '构建类别：%s\n' "$([[ "$IS_CANDIDATE" == "1" ]] && printf 'candidate' || printf 'release')"
  printf '应用版本：%s\n' "$(grep -E '^APP_VERSION = ' backend/version.py | sed -E 's/.*"([^"]+)".*/\1/')"
  printf '构建主机：macOS %s / %s\n' "$MACOS_VERSION" "$(uname -m)"
  printf '构建工具：Python %s / Node %s / pnpm %s\n' "$("$PYTHON_BIN" --version | awk '{print $2}')" "$NODE_VERSION" "$(pnpm --version)"
  printf '正式验证环境：macOS %s / Node %s\n' "$OFFICIAL_MACOS_MAJOR" "$OFFICIAL_NODE_VERSION"
} > "dist/build-provenance.txt"
"$PYTHON_BIN" scripts/release_manifest.py dist

printf '\n构建完成：%s\n' "$ROOT_DIR/dist/$ARCHIVE_NAME"
[[ "$IS_CANDIDATE" == "0" ]] || printf '注意：这是候选包；仍须在 macOS %s / Node %s 完成正式构建和目标机验证。\n' "$OFFICIAL_MACOS_MAJOR" "$OFFICIAL_NODE_VERSION" >&2
