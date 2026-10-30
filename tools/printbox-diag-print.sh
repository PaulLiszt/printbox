#!/usr/bin/env bash
# tools/printbox-diag-print.sh —— 抓取「双打 / 客户端死循环」的真实证据
# 做法：
#   1) 仅本会话临时开启 CUPS debug 日志（cupsctl，结束自动复原为 warn，不改 cupsd.conf）
#   2) 受控打印 1 次（脚本直接 lp），监视 20s 看作业是否「完成并离开队列」
#   3) 抓取本次打印的 CUPS error_log 片段（backend 是否挂起 / 是否发了 2 个 PAGE / 完成信号）
#   4) 空闲监视 60s：请用 iOS / 鸿蒙 在此窗口各打印一次，捕获客户端是否连环重发
#   5) 自分析并落盘，结尾打印可直接回贴的摘要
#
# 注意：本脚本【不改动队列配置】，只临时调日志级别（已复原），可放心反复跑。
# 用法：
#   bash /home/<box>/printbox-diag-print.sh

set -u
QUEUE="${PRINTBOX_QUEUE:-hp_laserjet_p1505n_printbox}"
DIAG="/tmp/printbox-diag-$(date +%Y%m%d-%H%M%S).log"

# 同时输出到屏幕与文件
exec > >(tee -a "$DIAG")
exec 2>&1

hr(){ printf '%s\n' "------------------------------------------------------------"; }
log(){ printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
step(){ hr; log "== $* =="; }

printbox_ep(){
  awk -v q="$QUEUE" '
    $1=="<Printer" && $2==q">"      {f=1; next}
    $1=="<DefaultPrinter" && $2==q">" {f=1; next}
    /^<\/(Printer|DefaultPrinter)>/ {f=0}
    f && $1=="ErrorPolicy"          {print $2}
  ' /etc/cups/printers.conf 2>/dev/null
}

step "0. 准备：临时开启 CUPS debug 日志"
cupsctl LogLevel=debug >/dev/null 2>&1 || true
log "  已开启 CUPS debug（结束自动复原为 warn）。当前日志级别: $(cupsctl 2>/dev/null | grep -i LogLevel || echo 未知)"

step "1. 当前队列与策略快照（基线）"
log "  ErrorPolicy: $(printbox_ep)"
log "  lpstat -p $QUEUE:"
lpstat -p "$QUEUE" 2>&1 | sed 's/^/    /'
log "  PPD 默认 copies: $(grep -i '^\*DefaultCopies' /etc/cups/ppd/$QUEUE.ppd 2>/dev/null || echo '(无该键)')"
log "  lpoptions 生效 copies: $(lpoptions -p "$QUEUE" -l 2>/dev/null | grep -i 'copies' || echo '(未显式设置)')"
log "  URI: $(lpstat -v "$QUEUE" 2>/dev/null | awk '{print $NF}')"

step "2. 受控打印（脚本直接 lp，仅 1 次）"
BEFORE=$(wc -l < /var/log/cups/error_log 2>/dev/null || echo 0)
if [ -f /usr/share/cups/data/testprint ]; then
  REQ="$(lp -d "$QUEUE" /usr/share/cups/data/testprint 2>&1)"
else
  REQ="$(printf 'PrintBox diag\n%s\n' "$(date)" | lp -d "$QUEUE" 2>&1)"
fi
log "  $REQ"
RID="$(echo "$REQ" | grep -oE "${QUEUE}-[0-9]+" | head -n1)"
log "  本次受控作业 ID: ${RID:-未知}"

step "3. 监视受控作业状态（每 2s，共 20s）"
COMPLETED=0
for i in $(seq 1 10); do
  SNAP="$(lpstat -o "$QUEUE" 2>&1)"
  if [ -z "$SNAP" ]; then
    if [ "$i" -gt 1 ]; then COMPLETED=1; log "  [$i] lpstat -o 为空 => 作业已离开队列（很可能已完成）"; break; fi
  else
    log "  [$i] lpstat -o: $SNAP"
  fi
  sleep 2
done
if [ "$COMPLETED" = "0" ]; then
  log "  => 20s 后作业仍在队列（backend 可能未结束 / 挂起，这正是客户端重发的根因）"
fi

step "4. 抓取本次受控打印的 CUPS 日志片段"
log "  error_log 新增行（过滤 $RID / backend / usb / PAGE / Completed / Ready / PID / error / fail）："
tail -n +"$((BEFORE+1))" /var/log/cups/error_log 2>/dev/null \
  | grep -iE "$RID|backend|usb|PAGE|Completed|Ready to print|Sent job|PID|error|fail" \
  | sed 's/^/    /' | head -n 80

step "5. 空闲监视 60s —— 请用 iOS / 鸿蒙 在此窗口各打印一次，以捕获客户端是否连环重发"
log "  开始 60s 窗口，现在请用手机打印…"
for i in $(seq 1 30); do
  sleep 2
  CUR="$(lpstat -o "$QUEUE" 2>/dev/null)"
  N="$(echo "$CUR" | grep -cE "${QUEUE}-[0-9]")"
  if [ "$N" -gt 0 ]; then
    log "  [手机窗口 $i] 当前活动作业数=$N"
    echo "$CUR" | sed 's/^/      /'
  fi
done
log "  窗口结束。若期间作业 ID 持续递增（如 -104 → -105 → -106…）=> 客户端连环重发，与 CUPS 服务端无关。"

step "6. 复原日志级别"
cupsctl LogLevel=warn >/dev/null 2>&1 || true
log "  CUPS 日志级别已复原为 warn"

hr
log "===== 自分析摘要（请整段发我）====="
log "队列: $QUEUE"
log "ErrorPolicy: $(printbox_ep)"
log "受控作业 ID: ${RID:-未知}"
log "受控作业 20s 内是否完成离开队列: $([ "$COMPLETED" = "1" ] && echo 是 || echo 否=挂起)"
log "PPD 默认 copies: $(grep -i '^\*DefaultCopies' /etc/cups/ppd/$QUEUE.ppd 2>/dev/null || echo '(无)')"
log "物理观察（你来看）：受控打印实际出了几张纸？___  手机窗口期间是否出现连环新作业？___"
hr
log "完整日志已存: $DIAG"
