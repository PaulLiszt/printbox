#!/usr/bin/env bash
# tools/printbox-diag-crash.sh —— 抓取「打印时 cupsd 崩溃」的真实信号，并尝试最可能的安全修复
# 现象根因（已确认）：作业打到一半 cupsd 崩（"Scheduler shutting down due to program error"），
#   重启后把未完成的作业重跑一遍 => 双打；客户端 IPP 连接随崩断裂(Broken pipe) => 客户端重发 => 死循环。
#   与 ErrorPolicy 无关（abort-job 救不了「调度器崩溃重启」）。
#
# 本脚本：
#   A) 不改任何配置，抓取崩溃信号（dmesg segfault / journalctl / coredump / error_log 崩溃前上下文）
#   B) 尝试最可能的安全修复：禁用 usblp 内核模块（HP USB 打印机常见崩溃源）
#   C) 复测一次打印，看崩溃是否消失
# 全部输出同时落盘 /tmp/printbox-crashdiag-<ts>.log
#
# 用法：bash /home/<box>/printbox-diag-crash.sh

set -u
QUEUE="${PRINTBOX_QUEUE:-hp_laserjet_p1505n_printbox}"
LOG="/tmp/printbox-crashdiag-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG")
exec 2>&1

hr(){ printf '%s\n' "------------------------------------------------------------"; }
log(){ printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
step(){ hr; log "== $* =="; }

# 抓取 journalctl 当前游标（用于只看「之后」的新日志）
CUR="$(journalctl -u cups -n 1 --show-cursor --no-pager 2>/dev/null | grep -oP '(?<=-- cursor: ).*')"
TPAGE="/usr/share/cups/data/testprint"

do_print(){
  if [ -f "$TPAGE" ]; then lp -d "$QUEUE" "$TPAGE" 2>&1; else printf 'diag\n' | lp -d "$QUEUE" 2>&1; fi
}

step "A. 基线：打印前状态 + 崩溃信号来源检查"
log "  usblp 模块: $(lsmod 2>/dev/null | grep -i usblp || echo '未加载')"
log "  colord 服务: $(systemctl is-active colord 2>/dev/null || echo 未知)"
log "  最近 AppArmor 拒绝(cups 相关):"
dmesg 2>/dev/null | grep -i apparmor | grep -iE 'cups|usb' | tail -n 3 | sed 's/^/    /' || true
[ -z "$(dmesg 2>/dev/null | grep -i apparmor | grep -iE 'cups|usb' | tail -n1)" ] && log "    (无)"
log "  cupsd 版本: $(cupsd -v 2>/dev/null | head -n1 || echo 未知)"
log "  CUPS 包版本: $(dpkg -l 2>/dev/null | grep -iE '^ii\s+cups' | awk '{print $2, $3}' | tr '\n' ' ')"

step "B. 触发一次打印并捕获崩溃（先不改动任何配置）"
REQ="$(do_print)"; log "  $REQ"
RID="$(echo "$REQ" | grep -oE "${QUEUE}-[0-9]+" | head -n1)"
log "  受控作业 ID: ${RID:-未知}，等待 12s 让 cupsd 崩溃+重启…"
for i in $(seq 1 6); do sleep 2; S="$(lpstat -o "$QUEUE" 2>&1)"; log "  [$i] lpstat -o: ${S:-<空=已离开队列>}"; done

step "C. 抓取崩溃信号"
log "  [C1] journalctl -u cups（本次新增，过滤崩溃关键词）:"
if [ -n "$CUR" ]; then
  journalctl -u cups --after-cursor="$CUR" --no-pager 2>/dev/null | grep -iE 'segfault|shutting down|signal|abort|panic|assert|core|trace|general protection|traps' | tail -n 30 | sed 's/^/    /'
else
  journalctl -u cups --no-pager 2>/dev/null | grep -iE 'segfault|shutting down|signal|abort|panic|assert' | tail -n 30 | sed 's/^/    /'
fi
log "  [C2] dmesg 中的 cupsd 崩溃:"
dmesg -T 2>/dev/null | grep -iE 'cupsd|segfault|general protection|traps|page fault' | tail -n 15 | sed 's/^/    /'
log "  [C3] coredumpctl:"
if command -v coredumpctl >/dev/null 2>&1; then coredumpctl list cupsd 2>/dev/null | tail -n 5 | sed 's/^/    /'; else log "    (未安装 coredumpctl)"; fi
log "  [C4] error_log 中『Scheduler shutting down』前的上下文（最后 1 次崩溃前 25 行）:"
LN="$(grep -n 'Scheduler shutting down' /var/log/cups/error_log 2>/dev/null | tail -n1 | cut -d: -f1)"
if [ -n "$LN" ]; then
  START=$((LN-25)); [ "$START" -lt 1 ] && START=1
  sed -n "${START},${LN}p" /var/log/cups/error_log 2>/dev/null | sed 's/^/    /'
else
  log "    (error_log 未找到该字样)"
fi

step "D. 尝试最可能的安全修复：禁用 usblp 内核模块（HP USB 打印机经典崩溃源）"
if lsmod 2>/dev/null | grep -qi usblp; then
  log "  usblp 当前已加载，写入黑名单并卸载…"
  echo 'blacklist usblp' > /etc/modprobe.d/blacklist-usblp.conf
  if modprobe -r usblp 2>/dev/null; then log "    [OK] 已卸载 usblp（重启后也不会再加载）"; else log "    [!] 卸载失败（设备占用中），但黑名单已写入，重启后生效"; fi
else
  log "  usblp 未加载 —— 崩溃另有原因，本步跳过（信号见 C 段）。"
fi

step "E. 复测：再次打印，确认崩溃是否消失"
sleep 2
REQ2="$(do_print)"; log "  $REQ2"
for i in $(seq 1 6); do sleep 2; S="$(lpstat -o "$QUEUE" 2>&1)"; log "  [$i] lpstat -o: ${S:-<空=已离开队列>}"; done
log "  复测期间 journalctl 新增崩溃关键词:"
if [ -n "$CUR" ]; then
  journalctl -u cups --after-cursor="$CUR" --no-pager 2>/dev/null | grep -iE 'segfault|shutting down' | tail -n 10 | sed 's/^/    /'
else
  journalctl -u cups --no-pager 2>/dev/null | grep -iE 'segfault|shutting down' | tail -n 10 | sed 's/^/    /'
fi

hr
log "===== 摘要（请整段发我）====="
log "cupsd 版本: $(cupsd -v 2>/dev/null | head -n1 || echo 未知)"
log "usblp 原状态: $(lsmod 2>/dev/null | grep -i usblp || echo 未加载) | 处理: 已写黑名单$(lsmod 2>/dev/null | grep -qi usblp || echo 并卸载)"
log "崩溃信号(C段): (见上方 C1~C4，重点：dmesg 是否有 'cupsd[PID]: segfault at ...' 或 'traps')"
log "复测是否仍崩溃(E段): 见上方 E 段最后的 journalctl 输出"
log "物理观察：两次受控打印各出几张纸？___  是否还看到『Scheduler shutting down』？___"
hr
log "完整日志: $LOG"
