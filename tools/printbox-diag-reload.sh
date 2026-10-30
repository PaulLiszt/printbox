#!/usr/bin/env bash
# PrintBox 决定性验证：cupsd 因 PrintBox 的 cupsctl 重写配置 → 重载崩溃
# 步骤：停 printbox 单元看是否稳 → 抓 cupsd.conf+cupsd -t → 用同款 cupsctl 复现崩溃
#      → 探测真凶（Port 631 与 cups.socket 套接字激活冲突）：禁用 cups.socket 后重试
set -u
LOG=/tmp/printbox-reload-$(date +%Y%m%d-%H%M%S).log
TS(){ date +"%H:%M:%S"; }
log(){ echo "[$(TS)] $*" | tee -a "$LOG"; }
hline(){ echo "------------------------------------------------------------" | tee -a "$LOG"; }
cupsd_crash(){ grep -q "Scheduler shutting down due to program error" /var/log/cups/error_log 2>/dev/null; }

hline
log "PrintBox 重载崩溃决定性验证 ($(date))"
log "日志: $LOG"
hline

log "== A. 列出 printbox 相关 unit 与 cups.socket 状态 =="
systemctl list-units --type=service --all 2>/dev/null | grep -i printbox | tee -a "$LOG" || true
systemctl list-units --type=timer --all 2>/dev/null | grep -i printbox | tee -a "$LOG" || true
log "A2) cups.socket / cups.path 状态:"
systemctl is-enabled cups.socket cups.path 2>/dev/null | tee -a "$LOG" || true
systemctl is-active  cups.socket cups.path 2>/dev/null | tee -a "$LOG" || true
log "A3) cups.socket ListenStream:"
grep -h "ListenStream" /etc/systemd/system/cups.socket /usr/lib/systemd/system/cups.socket 2>/dev/null | tee -a "$LOG" || true

log "== B. 停掉所有 printbox 单元（移除外部 PUT 触发器）=="
for u in printbox-guard.timer printbox-guard.service printbox-web.service printbox-archive.service printbox-printer-hotplug.service printbox-mdns-renew.timer printbox-mdns-renew.service printbox-mdns-alias.service; do
  systemctl stop "$u" 2>/dev/null && log "   stopped $u" || true
  systemctl disable "$u" 2>/dev/null || true
done

log "== C. 开 debug + 启动 cupsd，监视 30s（预期：无 PUT、稳定）=="
systemctl stop cups 2>/dev/null; pkill -x cupsd 2>/dev/null; sleep 2
[ -f /var/log/cups/error_log ] && mv -f /var/log/cups/error_log /var/log/cups/error_log.prev.$(date +%s) 2>/dev/null
: > /var/log/cups/error_log 2>/dev/null
grep -qi '^LogLevel' /etc/cups/cupsd.conf && sed -i 's/^LogLevel.*/LogLevel debug/' /etc/cups/cupsd.conf || echo 'LogLevel debug' >> /etc/cups/cupsd.conf
systemctl start cups 2>/dev/null; sleep 1
CRASH=0; PUT=0
for i in $(seq 1 30); do
  cupsd_crash && { CRASH=1; log "   [$i s] 崩溃"; break; }
  grep -q "PUT /admin/conf/cupsd.conf" /var/log/cups/error_log 2>/dev/null && { PUT=1; log "   [$i s] 仍有外部 PUT（printbox 未完全停？）"; }
  sleep 1
done
[ "$CRASH" = "0" ] && log "   [30s] 未崩溃 —— 停掉 printbox 后 cupsd 稳定 ✅"
log "C 结论: 停 printbox 后崩溃=$([ "$CRASH" = "1" ] && echo 是 || echo 否-稳定); 期间外部PUT=$([ "$PUT" = "1" ] && echo 有 || echo 无)"

log "== D. 抓当前 cupsd.conf（即 PrintBox 写进的）=="
log "----- /etc/cups/cupsd.conf -----"
cat /etc/cups/cupsd.conf | tee -a "$LOG"
log "----- cupsd -t 校验 -----"
cupsd -t 2>&1 | tee -a "$LOG" || true
log "D2) cupsd.conf 是否含 Port/Listen 631:"
grep -nE "^(Port|Listen).*631" /etc/cups/cupsd.conf | tee -a "$LOG" || log "   (无 Port/Listen 631 行)"

log "== E. 用 PrintBox 同款命令复现崩溃：cupsctl PreserveJobFiles=yes PreserveJobHistory=yes =="
: > /var/log/cups/error_log 2>/dev/null
cupsctl PreserveJobFiles=yes PreserveJobHistory=yes 2>&1 | tee -a "$LOG" || true
sleep 8
if cupsd_crash; then
  log "   [E] cupsctl 后 cupsd 崩了 → 重载机制在本配置下致命 ⚠️"
  E_CRASH=1
  log "   ----- 崩溃点前 50 行 -----"
  ln=$(grep -n "Scheduler shutting down" /var/log/cups/error_log | tail -1 | cut -d: -f1)
  [ -n "$ln" ] && s=$((ln-50)) && [ "$s" -lt 1 ] && s=1 && sed -n "${s},${ln}p" /var/log/cups/error_log | tee -a "$LOG"
else
  log "   [E] cupsctl 后 cupsd 未崩 → 重载 OK，问题在 PrintBox 写的特定内容"
  E_CRASH=0
fi

log "== F. 探测真凶：Port 631 与 cups.socket 套接字激活冲突 =="
log "F1) 当前 cups.socket 激活态下，禁用 cups.socket/cups.path，让 cupsd 自己 bind 631，再试 cupsctl"
systemctl disable --now cups.socket cups.path 2>&1 | tee -a "$LOG" || true
systemctl restart cups 2>/dev/null; sleep 3
: > /var/log/cups/error_log 2>/dev/null
cupsctl PreserveJobFiles=yes PreserveJobHistory=yes 2>&1 | tee -a "$LOG" || true
sleep 8
if cupsd_crash; then
  log "   [F] 禁用 cups.socket 后 cupsctl 仍崩 → 不是 socket 冲突，需另查（见 E 段日志）"
  F_CRASH=1
else
  log "   [F] 禁用 cups.socket 后 cupsctl 不再崩 ✅ → 根因坐实：Port 631 与 cups.socket 套接字激活冲突"
  F_CRASH=0
fi

log "== G. 还原 LogLevel 为 warn（保持 cupsd 运行）=="
sed -i 's/^LogLevel.*/LogLevel warn/' /etc/cups/cupsd.conf
log "   已复原 warn；cupsd 当前仍在运行（printbox 单元保持停用，待代码修复）"

hline
log "===== 取证摘要（请整段发我）====="
log "C) 停 printbox 后 30s 是否稳定: $([ "$CRASH" = "1" ] && echo 否-仍崩 || echo 是-稳定)"
log "E) 同款 cupsctl 是否触发崩溃: $([ "${E_CRASH:-1}" = "1" ] && echo 是 || echo 否)"
log "F) 禁用 cups.socket 后 cupsctl 是否还崩: $([ "${F_CRASH:-1}" = "1" ] && echo 是-仍崩 || echo 否-已稳-坐实socket冲突)"
log "完整日志: $LOG"
log "⚠️ printbox 单元已 stop+disable，cupsd 现稳定。修复前请勿 systemctl enable printbox-*.timer"
log "   恢复命令（待交付修复后）: systemctl enable --now printbox-guard.timer printbox-web.service"
hline
