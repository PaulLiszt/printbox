#!/usr/bin/env bash
# tools/printbox-fix-printloop.sh —— 修复「同一作业被重复/无限打印」
# （iOS 打 2 份、鸿蒙不停打印的根因：队列未设 ErrorPolicy=abort-job，CUPS 默认
#  retry-job 在后端每作业报错后反复重打同一 job-ID）
#
# 本脚本（盒上 root 跑一条命令）：
#   0) 立即停掉正在洪流的作业（cupsdisable + cancel -a），止血
#   1) 读出 /etc/cups/printers.conf 里队列真实的 ErrorPolicy（唯一真相源）
#   2) 用 P1505n 精确驱动重建队列（避免误用 P1505 导致后端握手异常）
#   3) 独立、显式地强制 ErrorPolicy=abort-job，并复核已落盘
#   4) 自检 + 打 1 张测试页，确认不再循环
#
# 用法：
#   bash /home/<box>/printbox-fix-printloop.sh
#   跑完把「===== 回贴摘要 =====」整段发我。

set -u
QUEUE="${PRINTBOX_QUEUE:-hp_laserjet_p1505n_printbox}"
DEFAULT_URI="usb://HP/LaserJet%20P1505n?serial=KQ12ZY6"
LOG="/tmp/printbox-fixloop-$(date +%Y%m%d-%H%M%S).log"

hr(){ printf '%s\n' "------------------------------------------------------------"; }
log(){ printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
step(){ hr; log "== $* =="; }

# 读队列真实 ErrorPolicy（来自 printers.conf，唯一真相源；不是 PPD 选项）
read_error_policy(){
  awk -v q="$QUEUE" '
    $1=="<Printer" && $2==q">"      {f=1; next}
    $1=="<DefaultPrinter" && $2==q">" {f=1; next}
    /^<\/(Printer|DefaultPrinter)>/ {f=0}
    f && $1=="ErrorPolicy"          {print $2}
  ' /etc/cups/printers.conf 2>/dev/null
}

step "0/6 立即止血：停掉正在洪流的作业"
cupsdisable "$QUEUE" >/dev/null 2>&1 || true
sleep 1
if cancel -a "$QUEUE" >/dev/null 2>&1; then log "  已 cancel -a $QUEUE（清除排队/重打中的作业）"; else log "  (当前无活动作业)"; fi
cupsenable "$QUEUE" >/dev/null 2>&1 || true
accept "$QUEUE"    >/dev/null 2>&1 || true

step "1/6 当前队列真实 ErrorPolicy（printers.conf）"
EP_NOW="$(read_error_policy)"
log "  修复前 ErrorPolicy = '${EP_NOW:-<未设置=走 CUPS 默认 retry-job>}'"
if grep -qi '^ErrorPolicy' /etc/cups/cupsd.conf 2>/dev/null; then
  log "  cupsd.conf 全局默认: $(grep -i '^ErrorPolicy' /etc/cups/cupsd.conf)"
fi

step "2/6 探测设备 URI + 选 P1505n 精确驱动"
URI="$(lpinfo -v 2>/dev/null | grep -i 'usb://HP' | awk '{print $2}' | head -n1)"
[ -z "$URI" ] && URI="$(lpinfo -v 2>/dev/null | grep -i 'hp:/usb' | awk '{print $2}' | head -n1)"
[ -z "$URI" ] && URI="$DEFAULT_URI"
log "  URI = $URI"

# 优先精确 P1505n 驱动 id；找不到才退到任意 p1505 驱动
DRV="$(lpinfo -m 2>/dev/null | grep -i 'p1505n' | awk '{print $1}' | head -n1)"
[ -z "$DRV" ] && DRV="$(lpinfo -m 2>/dev/null | grep -i 'p1505' | awk '{print $1}' | head -n1)"
[ -z "$DRV" ] && { log "  [FAIL] lpinfo -m 无 P1505 驱动，请回贴日志"; exit 2; }
DRV_STRIP="$(echo "$DRV" | sed 's/:0\//:\//')"   # 去代价前缀 :N/ → :/
log "  驱动 id = $DRV  (去前缀: $DRV_STRIP)"

step "3/6 用 P1505n 精确驱动重建队列 + 强制 abort-job"
lpadmin -x "$QUEUE" >/dev/null 2>&1 || true
sleep 1
for cand in "$DRV_STRIP" "$DRV"; do
  [ -z "$cand" ] && continue
  log "  尝试 lpadmin -m $cand"
  lpadmin -p "$QUEUE" -E -v "$URI" -m "$cand" -o printer-error-policy=abort-job 2>&1 | sed 's/^/    /'
  if [ -s "/etc/cups/ppd/$QUEUE.ppd" ]; then log "  [OK] 驱动 $cand 装出真实 PPD"; break; fi
  log "  [WARN] $cand 半残，删除重试"
  lpadmin -x "$QUEUE" >/dev/null 2>&1 || true
  sleep 1
done
# 关键：独立、显式再设一次 abort-job（不依赖上面 -m 的 -o 是否生效）
lpadmin -p "$QUEUE" -o printer-error-policy=abort-job 2>&1 | sed 's/^/    /'
lpadmin -p "$QUEUE" -o printer-is-shared=true 2>/dev/null
lpoptions -d "$QUEUE" 2>/dev/null

step "4/6 复核 printers.conf 中 ErrorPolicy 是否已落盘"
EP_NEW="$(read_error_policy)"
log "  修复后 ErrorPolicy = '${EP_NEW:-<空>}'"
if [ "$EP_NEW" = "abort-job" ]; then
  log "  [OK] abort-job 已确认落盘"
else
  log "  [FAIL] 仍非 abort-job（当前='${EP_NEW}'）。尝试重启 cups 后再设一次。"
  systemctl restart cups >/dev/null 2>&1
  sleep 3
  lpadmin -p "$QUEUE" -o printer-error-policy=abort-job 2>&1 | sed 's/^/    /'
  EP_NEW="$(read_error_policy)"
  log "  重启后 ErrorPolicy = '${EP_NEW:-<空>}'"
fi

step "5/6 自检"
lpstat -p "$QUEUE" 2>&1 | sed 's/^/    /'
if [ -f /etc/cups/ppd/$QUEUE.ppd ] && command -v cupstestppd >/dev/null 2>&1; then
  log "  cupstestppd: $(cupstestppd /etc/cups/ppd/$QUEUE.ppd 2>&1 | tail -n1)"
fi

step "6/6 打 1 张测试页，确认不再循环"
if [ -f /usr/share/cups/data/testprint ]; then
  lp -d "$QUEUE" /usr/share/cups/data/testprint 2>&1 | sed 's/^/    /'
else
  printf 'PrintBox test\n%s\n' "$(date)" | lp -d "$QUEUE" 2>&1 | sed 's/^/    /'
fi
sleep 3
log "  当前作业（应只有 1 个，且不再持续增长）："
lpstat -o "$QUEUE" 2>&1 | sed 's/^/    /' || true

hr
log "===== 回贴摘要 ====="
log "队列: $QUEUE"
log "驱动: $DRV"
log "ErrorPolicy（修复前）: '${EP_NOW:-<未设置>}'"
log "ErrorPolicy（修复后）: '${EP_NEW:-<未设置>}'"
log "cups 状态: $(systemctl is-active cups 2>/dev/null)"
log "lpstat -p:"
lpstat -p "$QUEUE" 2>&1 | sed 's/^/    /'
log "判断：测试页只出 1 张、lpstat -o 不再增长 = 无限重打已消除。"
log "若鸿蒙仍循环，则是客户端（鸿蒙）自身重试，需在鸿蒙侧排查，与 CUPS 无关。"
hr
