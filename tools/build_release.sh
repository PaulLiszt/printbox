#!/usr/bin/env bash
# tools/build_release.sh —— 构建 PrintBox 源码分发包（唯一权威产物）
#
# 输出固定为： dist/printbox-<version>-src.tar.gz
#   <version> 自动取自 pyproject.toml，无需手填，避免“包名/版本对不上”。
# 用法： bash tools/build_release.sh
#
# 设计目标：交付位置固定（dist/）、命名固定（printbox-<ver>-src.tar.gz），
# 不再出现“包飘到工作区根目录 / 源码目录里还残留旧包”的混乱。
set -euo pipefail

# 切到项目根（printserver-firmware/），无论在哪调用本脚本
cd "$(dirname "$0")/.."

# 从 pyproject.toml 读取版本号（匹配 version = "x.y.z"）
VERSION="$(grep -m1 '^version' pyproject.toml | sed -E 's/.*"([^"]+)".*/\1/')"
if [ -z "$VERSION" ]; then
  echo "错误：无法从 pyproject.toml 读取 version" >&2
  exit 1
fi

mkdir -p dist
OUT="dist/printbox-${VERSION}-src.tar.gz"

# 固定排除项：旧包、缓存、git、构建产物、dist 自身（避免把上一版包打进下一版）
tar -czf "$OUT" \
  --exclude='*.tar.gz' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='dist' \
  --exclude='*.egg-info' \
  --exclude='build' \
  .

SIZE="$(du -h "$OUT" | cut -f1)"
echo "构建完成:"
echo "  版本 : $VERSION"
echo "  路径 : $(pwd)/$OUT"
echo "  大小 : $SIZE"
echo "交付物唯一位置已固定为 dist/，请勿把包复制到其它目录散落。"
