#!/usr/bin/env bash
# 审迹 macOS 14 Apple Silicon 一键构建。
# 固定：macOS 14 arm64、Python 3.11.11、Node.js 22.12.0、pnpm 11.5.0。
# 产物不签名/不公证，适用于已确认的小范围内部使用。

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

fail() { printf '失败：%s\n' "$1" >&2; exit 1; }
step() { printf '\n==> %s\n' "$1"; }

[[ "$(uname -s)" == "Darwin" ]] || fail "此脚本只能在 macOS 上执行"
[[ "$(sw_vers -productVersion | cut -d. -f1)" == "14" ]] || fail "发布构建机必须为 macOS 14"
[[ "$(uname -m)" == "arm64" ]] || fail "发布包仅支持 Apple Silicon（arm64）"

command -v python3.11 >/dev/null || fail "未找到 Python 3.11.11"
[[ "$(python3.11 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')" == "3.11.11" ]] || fail "需要 Python 3.11.11"
command -v node >/dev/null || fail "未找到 Node.js 22.12.0"
[[ "$(node --version | sed 's/^v//')" == "22.12.0" ]] || fail "需要 Node.js 22.12.0"
command -v pnpm >/dev/null || fail "未找到 pnpm 11.5.0"
[[ "$(pnpm --version)" == "11.5.0" ]] || fail "需要 pnpm 11.5.0"

VENV_DIR="$ROOT_DIR/.venv-macos-arm64"
PYTHON_BIN="$VENV_DIR/bin/python"

step "创建独立 Python 3.11.11 虚拟环境"
[[ -x "$PYTHON_BIN" ]] || python3.11 -m venv "$VENV_DIR"
[[ "$("$PYTHON_BIN" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')" == "3.11.11" ]] \
  || fail "现有 .venv-macos-arm64 不是 Python 3.11.11；请删除该目录后重建"

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

step "生成发布清单和安装包"
"$PYTHON_BIN" scripts/release_manifest.py dist
ARCHIVE_NAME="审迹-macos-arm64.zip"
rm -f "dist/$ARCHIVE_NAME"
ditto -c -k --sequesterRsrc --keepParent "$APP_PATH" "dist/$ARCHIVE_NAME"

printf '\n构建完成：%s\n' "$ROOT_DIR/dist/$ARCHIVE_NAME"
