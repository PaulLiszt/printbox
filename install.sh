#!/usr/bin/env bash
# PrintBox 一键安装脚本（Debian / Ubuntu，需 root）。
#
# 用法：
#   curl -sSL https://raw.githubusercontent.com/PaulLiszt/printbox/main/install.sh | sudo bash
#   或在本仓库目录内离线安装：  sudo ./install.sh --local
#
# 流程：创建独立 venv -> 安装 printbox -> 建立 /usr/local/bin/printbox 软链 -> 运行 printbox init
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "错误：请使用 root 运行（sudo bash install.sh）。" >&2
  exit 1
fi

VENV=/opt/printbox/venv
BIN="$VENV/bin"

# 防套娃：若当前目录不是项目根（无 install.sh），但存在嵌套的
# printserver-firmware/ 子目录，则自动切换到真正的项目根，避免
# `scp -r dir host:/tmp/x` 因 /tmp/x 已存在而把项目套一层后装到旧文件。
if [ ! -f "$0" ] || [ ! -f "$(dirname "$0")/install.sh" ]; then
  if [ -f "printserver-firmware/install.sh" ]; then
    echo "==> 检测到嵌套目录，自动切换到项目根 printserver-firmware/ ..."
    cd "printserver-firmware"
  fi
fi

echo "==> 检测系统..."
if ! command -v apt-get >/dev/null 2>&1; then
  echo "错误：仅支持 Debian/Ubuntu 等使用 apt 的发行版。" >&2
  exit 1
fi

echo "==> 更新软件包索引..."
apt-get update
echo "==> 安装 Python 虚拟环境工具..."
DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv python3-pip

echo "==> 创建虚拟环境 $VENV ..."
python3 -m venv --clear "$VENV"
"$BIN/pip" install --upgrade pip >/dev/null

# Debian/Ubuntu 最小 venv 默认不含 setuptools，PEP517 构建隔离会报
# BackendUnavailable / OSError[Errno 2]。预装并改用 --no-build-isolation 复用 venv 内 setuptools。
echo "==> 预装构建依赖 setuptools / wheel（Debian venv 默认缺失）..."
"$BIN/pip" install --no-cache-dir setuptools wheel

if [ "${1:-}" = "--local" ]; then
  echo "==> 从本地源码安装 printbox（--no-build-isolation）..."
  # --force-reinstall --no-deps --no-cache-dir：同一版本号也强制覆盖、不读缓存，确保源码改动生效
  # --no-build-isolation：复用上一步预装的 setuptools，避免裸 venv 构建失败
  (cd "$(dirname "$0")" && "$BIN/pip" install --no-build-isolation --no-cache-dir --force-reinstall --no-deps .)
else
  echo "==> 从 PyPI 安装 printbox..."
  "$BIN/pip" install --no-cache-dir --upgrade printbox
fi

echo "==> 建立命令软链 /usr/local/bin/printbox ..."
ln -sf "$BIN/printbox" /usr/local/bin/printbox

echo "==> 运行 printbox init（配置打印服务器）..."
printbox init

INSTALLED_VER="$("$BIN/pip" show printbox 2>/dev/null | awk -F': ' '/^Version/ {print $2}')"
echo "==> 完成。已安装 printbox 版本: ${INSTALLED_VER}"
echo "==> 管理界面请用本机 IP、<主机名>.local 或 http://printbox.local 打开。"
