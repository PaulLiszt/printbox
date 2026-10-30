#!/usr/bin/env bash
# PrintBox 崩溃根因取证（纯诊断，不改动队列；临时开 debug 日志，结束复原）
# 目标：抓出 cupsd 崩溃的真实信号（SEGV/ABRT/断言），而非再猜一次。
set -u
QUEUE="hp_laserjet_p1505n_printbox"
LOG=/tmp/printbox-rootcause-$(date +%Y%m%d-%H%M%S).log
TS(){ date +"%H:%M:%S"; }
log(){ echo "[$(TS)] $*" | tee -a "$LOG"; }
hline(){ echo "------------------------------------------------------------" | tee -a "$LOG"; }

hline
log "PrintBox 崩溃根因取证 ($(date))"
log "日志同时写入: $LOG"
hline

log "== 0. 取证前快照（不改配置）=="
log "0.1) cupsd 当前是否受 AppArmor 约束 + 最近 10 分钟有无 DENIED："
aa-status 2>/dev/null | grep -i cupsd || true
dmesg 2>/dev/null | grep -i "apparmor.*cupsd.*DENIED" | tail -5 || true
log "0.2) systemctl 重启计数（NRestarts，>0 表示在崩→重启循环）："
systemctl show cups -p NRestarts,Result 2>/dev/null | tee -a "$LOG"
log "0.3) cupsd.conf LogLevel 现状："
grep -i '^LogLevel' /etc/cups/cupsd.conf 2>/dev/null | tee -a "$LOG" || echo "  (未显式设置，用默认)"

log "== 1. 停 cupsd + 清空 error_log，准备干净取证 =="
systemctl stop cups 2>/dev/null; sleep 2
# 备份并清空，避免旧日志干扰
[ -f /var/log/cups/error_log ] && mv -f /var/log/cups/error_log /var/log/cups/error_log.prev.$(date +%s) 2>/dev/null
: > /var/log/cups/error_log 2>/dev/null || true
# 临时开 debug（结束复原为 warn）
if grep -qi '^LogLevel' /etc/cups/cupsd.conf; then
  sed -i 's/^LogLevel.*/LogLevel debug/' /etc/cups/cupsd.conf
else
  echo 'LogLevel debug' >> /etc/cups/cupsd.conf
fi
log "   已设 LogLevel debug，error_log 已清空"

log "== 2. 启动 cupsd，空闲监视 25s 抓崩溃 =="
systemctl start cups 2>/dev/null
sleep 1
CRASH=0
for i in $(seq 1 25); do
  if grep -q "Scheduler shutting down due to program error" /var/log/cups/error_log 2>/dev/null; then
    CRASH=1
    log "   [$i s] 检测到崩溃（空闲态）"
    break
  fi
  sleep 1
done
[ "$CRASH" = "0" ] && log "   [25s] 空闲态未检测到崩溃（可能需更长或仅作业触发）"

log "== 3. 抓崩溃真相：error_log 崩溃点前 70 行（含断言/信号线索）=="
if grep -n "Scheduler shutting down due to program error" /var/log/cups/error_log >/dev/null 2>&1; then
  grep -n "Scheduler shutting down due to program error" /var/log/cups/error_log | while IFS=: read -r ln _; do
    s=$((ln-70)); [ "$s" -lt 1 ] && s=1
    echo "----- error_log 崩溃点前 70 行 (行 $s~$ln) -----" | tee -a "$LOG"
    sed -n "${s},${ln}p" /var/log/cups/error_log | tee -a "$LOG"
  done
else
  log "   error_log 未出现该崩溃行；完整 error_log 末尾 40 行："
  tail -40 /var/log/cups/error_log | tee -a "$LOG"
fi

log "== 4. 抓 systemd 记录的退出信号（SEGV/ABRT）=="
log "4.1) systemctl status cups（看 'Main process exited' 行）："
systemctl status cups --no-pager -l 2>/dev/null | head -25 | tee -a "$LOG"
log "4.2) journalctl -u cups 近 15 分钟，过滤退出信号关键词："
journalctl -u cups --since "-15 min" --no-pager 2>/dev/null > /tmp/_jc.txt
grep -iE "SEGV|ABRT|exited|killed|signal|core|dumped|Assertion|segfault|trap|code=" /tmp/_jc.txt | tail -30 | tee -a "$LOG" || echo "    (journalctl 无匹配行)"

log "== 5. dmesg 内核侧崩溃（segfault/oom/killed）=="
dmesg 2>/dev/null | grep -iE "cups|segfault|oom|killed process|general protection|BUG" | tail -20 | tee -a "$LOG" || echo "    (dmesg 无匹配)"

log "== 6. 还原 LogLevel 为 warn =="
sed -i 's/^LogLevel.*/LogLevel warn/' /etc/cups/cupsd.conf
log "   已复原 LogLevel warn（重启 cupsd 生效）"
systemctl restart cups 2>/dev/null; sleep 2

hline
log "===== 取证摘要（请整段发我）====="
log "空闲是否崩溃: $([ "$CRASH" = "1" ] && echo 是 || echo 否)"
log "关键看：第3段 error_log 崩溃前 70 行里有无 'ASSERT'/'Assertion'/'Freeing'/信号名；"
log "       第4段 systemctl status 的 'Main process exited, code=..., status=11/SEGV 或 6/ABRT'；"
log "       第5段 dmesg 有无 'cupsd[PID]: segfault at ...'。"
log "完整日志: $LOG"
hline
