#!/usr/bin/env bash
# tools/upgrade_box.sh —— 盒上用源码包升级 printbox（离线，固定流程）
#
# 用法（盒上，需 root）：
#   bash upgrade_box.sh <printbox-X.Y.Z-src.tar.gz> [VENV]
#     VENV 默认 /opt/printbox/venv
#
# 这是“从开发机 scp 源码包 → 盒上升级”的唯一标准步骤，等价于手动执行的：
#   pip install setuptools wheel
#   pip install --no-build-isolation --force-reinstall --no-deps <src>
#   printbox repair
# 写成脚本是为了不再手敲、不再漏步、不再绕开 --no-build-isolation。
set -euo pipefail

TARBALL="${1:?用法: bash upgrade_box.sh <printbox-X.Y.Z-src.tar.gz> [VENV]}"
VENV="${2:-/opt/printbox/venv}"

if [ ! -f "$TARBALL" ]; then
  echo "错误：找不到包 $TARBALL" >&2
  exit 1
fi
if [ ! -x "$VENV/bin/pip" ]; then
  echo "错误：venv 不存在或无 pip: $VENV" >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "==> 解包到临时目录 $TMP"
tar -xzf "$TARBALL" -C "$TMP" --strip-components=1

# Debian/Ubuntu 的最小 venv 默认不带 setuptools，PEP517 构建隔离会报
#   BackendUnavailable: Cannot import 'setuptools.build_meta'
#   OSError [Errno 2] No such file or directory
# 必须预装 setuptools/wheel，并用 --no-build-isolation 复用 venv 内版本。
export TMPDIR="${TMPDIR:-/root/tmp}"
mkdir -p "$TMPDIR"

echo "==> 预装 setuptools / wheel（Debian venv 默认缺失）"
"$VENV/bin/pip" install --no-cache-dir setuptools wheel

echo "==> 安装源码包（强制覆盖 + 不读缓存 + 不拉依赖 + 不复用隔离构建）"
"$VENV/bin/pip" install --no-build-isolation --no-cache-dir --force-reinstall --no-deps "$TMP"

echo "==> 版本校验（应显示新版本号）"
"$VENV/bin/printbox" version

echo "==> 修复：清幽灵队列 + 自愈空 URI + 重新发布 AirPrint"
"$VENV/bin/printbox" repair

echo "==> 升级完成。请用本机 IP 或 <主机名>.local 打开 WebUI；"
echo "    概览应只列 1 台打印机（hp_laserjet_p1505n_printbox）。"
