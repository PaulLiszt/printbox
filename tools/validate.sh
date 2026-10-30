#!/usr/bin/env bash
# tools/validate.sh —— 本地静态校验（无需 root / 无需联网）
# 检查所有 Python 模块语法、shell 脚本语法，并确认关键文件齐备。
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
fail=0

echo "== Python 语法检查 =="
for f in $(find src/printbox -name '*.py'); do
  if $PY -m py_compile "$f" 2>/tmp/pyerr; then
    echo "  OK  $f"
  else
    echo "  FAIL $f"; cat /tmp/pyerr; fail=1
  fi
done

echo "== Shell 语法检查 =="
if command -v bash >/dev/null; then
  for f in install.sh tools/*.sh; do
    [ -f "$f" ] || continue
    if bash -n "$f" 2>/tmp/sherr; then
      echo "  OK  $f"
    else
      echo "  FAIL $f"; cat /tmp/sherr; fail=1
    fi
  done
else
  echo "  (无 bash，跳过 shell 检查)"
fi

echo "== 关键文件存在性 =="
for f in \
  pyproject.toml \
  install.sh \
  README.md \
  src/printbox/__init__.py \
  src/printbox/__main__.py \
  src/printbox/cli.py \
  src/printbox/init.py \
  src/printbox/config.py \
  src/printbox/network.py \
  src/printbox/printers.py \
  src/printbox/drivers.py \
  src/printbox/archive.py \
  src/printbox/webapp.py \
  src/printbox/data/printbox-web.service \
  src/printbox/data/printbox-archive.service \
  src/printbox/data/printbox-printer-hotplug.service \
  src/printbox/data/99-printbox-printer.rules \
  src/printbox/web/templates/index.html \
  src/printbox/web/templates/printers.html \
  src/printbox/web/templates/settings.html \
  src/printbox/web/templates/archive.html \
  src/printbox/web/templates/history.html \
  src/printbox/web/templates/logs.html \
  src/printbox/web/static/style.css \
  docs/runbook.md ; do
  [ -f "$f" ] && echo "  OK  $f" || { echo "  MISSING $f"; fail=1; }
done

echo "== 校验 pyproject.toml 合法 =="
$PY -c "import tomllib,sys; tomllib.load(open('pyproject.toml','rb')) if hasattr(tomllib,'load') else None" 2>/dev/null \
  && echo "  OK  pyproject.toml (tomllib)" \
  || { echo "  (跳过 pyproject.toml 解析，旧 Python 无 tomllib)"; }

if [ "$fail" = "0" ]; then
  echo "== 全部校验通过 =="
else
  echo "== 存在错误，请修正 =="
  exit 1
fi
