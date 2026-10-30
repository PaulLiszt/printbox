#!/usr/bin/env bash
# tools/printbox-fix-copies.sh —— 盒上 root 一键修复「多份打印翻平方」（请求 N 份 → 实际 N×N 份）
#
# 根因（决定性，0.2.51 坐实）：Debian trixie 的 CUPS 默认会在 PDF 链路里【自动插入 pdftopdf】
# （mime 转换 application/pdf → application/vnd.cups-pdf）。pdftopdf 的职责是按 COPIES 把页数翻倍、
# 处理页码范围、按 PPD 默认纸张归一化页面尺寸。若 PrintBox 的 foo2xqx 包装脚本【再】调一次 pdftopdf
# 处理 COPIES，就会出现两次乘法 → 实际份数 = 请求份数²（2 份→4、3 份→9）。
#
# 修复：按 CUPS 实际拓扑自动选模式：
#   模式 A（CUPS 已自动插入 pdftopdf，绝大多数发行版）：wrapper 不再调 pdftopdf，直接把已被上游
#           归一化的 PDF 透传给 foomatic-rip，foomatic 收原始 COPIES（由它处理这一次）。
#   模式 B（CUPS 未自动插入 pdftopdf，罕见）：wrapper 自己调 pdftopdf(COPIES=N) 做尺寸/份数归一化，
#           foomatic 收 COPIES=1（避免再乘）。
# 两种模式都保证 copies 只被处理【一次】。
#
# 用法（盒上 root）： bash /home/<box>/printbox-fix-copies.sh
# 全程自动；结尾会打印诊断摘要与「受控验证：请求 2 份 → 实际页数」。

set -u
LOG=/tmp/printbox-copies-fix-$(date +%Y%m%d-%H%M%S).log
exec > >(tee -a "$LOG") 2>&1

echo "============================================================"
echo "[$(date +%H:%M:%S)] PrintBox 多份打印修复（诊断 + 修复 + 验证）"
echo "日志: $LOG"
echo "============================================================"

# ---- 找队列 ----
Q=$(lpstat -p 2>/dev/null | awk 'NR==1{print $2}')
if [ -z "$Q" ]; then
  echo "⚠️ 未找到任何打印机队列（lpstat -p 为空）。请先确保打印机能正常识别，再运行本脚本。"
  exit 1
fi
echo "[队列] $Q"
PPD=/etc/cups/ppd/$Q.ppd
W=/usr/lib/cups/filter/printbox-foo2xqx-pdf
if [ ! -f "$PPD" ]; then
  echo "⚠️ PPD 文件不存在: $PPD"
  exit 1
fi

# ---- 诊断 1：PPD 的 cupsFilter 行 ----
echo ""
echo "---- [诊断1] PPD 的 cupsFilter 行 ----"
grep -E '^\*cupsFilter' "$PPD" || echo "  (无 cupsFilter 行)"

# ---- 诊断 2：当前 wrapper 内容 ----
echo "---- [诊断2] 当前 wrapper 是否把 COPIES 传给 foomatic / 是否调 pdftopdf ----"
if [ -f "$W" ]; then
  grep -nE 'FARGS|exec "\$FOOMATIC"|PDFTOPDF|pdftopdf "\$JID"' "$W" | head -20 || echo "  (wrapper 无关键行，可能极旧)"
else
  echo "  wrapper 不存在！"
fi

# ---- 诊断 3：CUPS 是否在 PDF 链路自动插入 pdftopdf（mime 规则）----
echo "---- [诊断3] CUPS mime 是否自动插入 pdftopdf ----"
MIME_OK=no
for f in /usr/share/cups/mime/cupsfilters.convs /usr/share/cups/mime/mime.convs /etc/cups/mime.convs; do
  if [ -f "$f" ] && grep -qE 'application/pdf[[:space:]]+application/vnd\.cups-pdf[[:space:]]+[0-9]+[[:space:]]+pdftopdf' "$f"; then
    MIME_OK=yes; echo "  命中规则文件: $f"; break
  fi
done
echo "  CUPS 自动插入 pdftopdf: $MIME_OK"

# ---- 决策 ----
if [ "$MIME_OK" = "yes" ]; then
  MODE=A
else
  MODE=B
fi
echo "---- 选定包装模式: $MODE ----"

# ---- 写正确 wrapper ----
echo "---- 写正确 wrapper ($W, 模式 $MODE) ----"
FOOMATIC=$(command -v foomatic-rip 2>/dev/null || echo /usr/lib/cups/filter/foomatic-rip)
mkdir -p /usr/lib/cups/filter
if [ "$MODE" = "A" ]; then
  cat > "$W" <<EOF
#!/bin/bash
# PrintBox foo2xqx 包装（模式A：CUPS 已自动插入 pdftopdf，wrapper 不再调用，避免份数翻倍）。
set -u
FOOMATIC="$FOOMATIC"
if [ ! -x "\$FOOMATIC" ]; then
  FOOMATIC=\$(command -v foomatic-rip 2>/dev/null)
  if [ -z "\$FOOMATIC" ]; then
    for d in /usr/lib/cups/filter /usr/libexec/cups/filter /usr/local/lib/cups/filter /usr/bin; do
      [ -x "\$d/foomatic-rip" ] && { FOOMATIC="\$d/foomatic-rip"; break; }
    done
  fi
fi
JID="\$1"; USER="\$2"; TITLE="\$3"; COPIES="\$4"; shift 4
# CUPS 已在链路里用 pdftopdf 处理过份数/页码/尺寸，foomatic 按原始 COPIES 出图即可（只乘一次）。
exec "\$FOOMATIC" "\$JID" "\$USER" "\$TITLE" "\$COPIES" "\$@"
EOF
else
  # 模式 B：CUPS 未自动插入 pdftopdf，wrapper 自己调 pdftopdf 处理份数/页码/尺寸，foomatic 收 COPIES=1。
  cat > "$W" <<EOF
#!/bin/bash
# PrintBox foo2xqx 包装（模式B：CUPS 未自动插入 pdftopdf，wrapper 自调归一化）。
set -u
PDFTOPDF=\$(command -v pdftopdf 2>/dev/null || echo /usr/lib/cups/filter/pdftopdf)
FOOMATIC="$FOOMATIC"
if [ ! -x "\$PDFTOPDF" ]; then
  for d in /usr/lib/cups/filter /usr/libexec/cups/filter /usr/local/lib/cups/filter; do
    [ -x "\$d/pdftopdf" ] && { PDFTOPDF="\$d/pdftopdf"; break; }
  done
fi
if [ ! -x "\$FOOMATIC" ]; then
  for d in /usr/lib/cups/filter /usr/libexec/cups/filter /usr/local/lib/cups/filter /usr/bin; do
    [ -x "\$d/foomatic-rip" ] && { FOOMATIC="\$d/foomatic-rip"; break; }
  done
fi
JID="\$1"; USER="\$2"; TITLE="\$3"; COPIES="\$4"; shift 4
ARGS=(\$@)
INFILE=""
if [ \${#ARGS[@]} -gt 0 ]; then
  last="\${ARGS[\${#ARGS[@]}-1]}"
  [ -n "\$last" ] && [ -e "\$last" ] && INFILE="\$last"
fi
TMP="\$(mktemp /tmp/printbox-f2x-XXXXXX.pdf)" || exec "\$FOOMATIC" "\$JID" "\$USER" "\$TITLE" "\$COPIES" "\${ARGS[@]}"
if "\$PDFTOPDF" "\$JID" "\$USER" "\$TITLE" "\$COPIES" "\${ARGS[@]}" > "\$TMP" 2>/dev/null && [ -s "\$TMP" ] && [ "\$(head -c4 "\$TMP")" = "%PDF" ]; then
  # pdftopdf 已在上面按 COPIES 把页数翻倍；交给 foomatic 必须令其 COPIES=1，否则 foo2xqx 再乘一次 → N²。
  FARGS=("\$JID" "\$USER" "\$TITLE" "1")
  if [ -n "\$INFILE" ]; then
    for ((i=0;i<\${#ARGS[@]}-1;i++)); do FARGS+=("\${ARGS[\$i]}"); done
  else
    FARGS+=("\${ARGS[@]}")
  fi
  cat "\$TMP" | "\$FOOMATIC" "\${FARGS[@]}"
  rc=\$?
  rm -f "\$TMP"
  exit \$rc
fi
rm -f "\$TMP"
exec "\$FOOMATIC" "\$JID" "\$USER" "\$TITLE" "\$COPIES" "\${ARGS[@]}"
EOF
fi
chmod 0755 "$W"
echo "  wrapper 已写入（模式 $MODE）"

# ---- 修正 PPD 指向 wrapper（cupsFilter 行里 foomatic-rip / 旧路径 → 新 wrapper）----
echo "---- 修正 PPD 指向 wrapper ----"
if grep -qE 'foomatic-rip|/usr/local/bin/printbox-foo2xqx-pdf' "$PPD"; then
  sed -i 's#foomatic-rip#/usr/lib/cups/filter/printbox-foo2xqx-pdf#g; s#/usr/local/bin/printbox-foo2xqx-pdf#/usr/lib/cups/filter/printbox-foo2xqx-pdf#g' "$PPD"
  lpadmin -p "$Q" -P "$PPD" 2>&1 && echo "  PPD 已重新加载（lpadmin -P）" || echo "  ⚠️ lpadmin -P 失败，但 PPD 文件已改，重启 cups 后会重新读取"
else
  echo "  PPD 已指向 wrapper，无需修改"
fi

# ---- 重启 cupsd 使新 wrapper/PPD 生效 ----
echo "---- 重启 cupsd ----"
systemctl restart cups 2>&1
sleep 3
if systemctl is-active --quiet cups; then
  echo "  cupsd 已重新启动（active）"
else
  echo "  ⚠️ cupsd 未就绪，请检查 journalctl -u cups"
fi

# ---- 受控验证：lp -n 2 单页 PDF，预期 2 页 ----
echo "---- 受控验证：请求 2 份 → 预期 2 页 ----"
cat > /tmp/printbox-1p.pdf <<'PDF'
%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>endobj
trailer<</Root 1 0 R>>
%%EOF
PDF
# 临时开 debug 以记录 PAGE 行（此命令不触发崩溃，已验证）
cupsctl LogLevel=debug >/dev/null 2>&1
JID=$(lp -d "$Q" -n 2 /tmp/printbox-1p.pdf 2>&1 | grep -oE '[0-9]+' | head -1)
echo "  提交作业 ID: $JID"
sleep 9
cupsctl LogLevel=warn >/dev/null 2>&1
PAGES=$(grep -E "PAGE: $JID " /var/log/cups/error_log 2>/dev/null | wc -l)
echo "  实际物理页数: $PAGES (预期 2)"
if [ "$PAGES" = "2" ]; then
  echo "  ✅ 修复生效：请求 2 份 → 实际 2 页（copies 只乘一次）"
else
  echo "  ⚠️ 仍异常：预期 2 页，实际 $PAGES 页。请把本日志整段发我，进一步定位。"
fi

echo ""
echo "============================================================"
echo "[$(date +%H:%M:%S)] 完成。诊断摘要："
echo "  队列=$Q | 模式=$MODE | 实际页数=$PAGES(预期2) | 日志=$LOG"
echo "下一步：请用 iOS / Win10 / 鸿蒙 复测「多份 + 选某页」（预期：份数=请求份数，页码=选中页）。"
echo "============================================================"
