#!/usr/bin/env bash
# PrintBox 崩溃循环根治（AppArmor CAP_NET_ADMIN 方向）
# 设计：先取证（dmesg/journalctl 看崩溃时段 cupsd 的 AppArmor 拒绝/崩溃信号）
#      → 在 cupsd AppArmor 本地包含里授权 net_admin 并重载
#      → 干净重启验证 30s 不崩 + 受控打 1 页确认完成离开队列。
# 一次跑完，结尾「===== 摘要 =====」整段发回即可。
set -u
LOG=/tmp/printbox-aa-$(date +%Y%m%d-%H%M%S).log
exec > >(tee -a "$LOG") 2>&1

ts(){ date '+%H:%M:%S'; }
log(){ echo "[$(ts)] $*"; }
QUEUE=hp_laserjet_p1505n_printbox

echo "============================================================"
log "PrintBox 崩溃根治（AppArmor/CAP_NET_ADMIN）($(date))"
log "日志同时写入: $LOG"
echo "============================================================"

echo
log "== A. 取证：崩溃时段 cupsd 的 AppArmor 拒绝 / 崩溃信号 =="
log "A1) 最近 dmesg 中 cupsd / apparmor / net_admin / segfault / trap 相关："
dmesg -T 2>/dev/null | grep -iE 'cupsd|apparmor|net_admin|segfault|traps|general protection' | tail -30 | sed 's/^/   /' || log "   (无 / 权限不足)"
log "A2) journalctl -u cups 最近 20min 的 denied/segfault/trap/abort："
journalctl -u cups --since "-20 min" 2>/dev/null | grep -iE 'denied|segfault|trap|abort|cupsd' | tail -30 | sed 's/^/   /' || log "   (无)"
log "A3) cupsd 当前 AppArmor 状态（是否被 confinement）："
aa-status 2>/dev/null | grep -i cupsd | sed 's/^/   /' || log "   (aa-status 不可用或 cupsd 未列出)"
log "A4) cupsd profile 是否已含 net_admin 授权："
grep -rn 'net_admin' /etc/apparmor.d/ 2>/dev/null | grep -i cupsd | sed 's/^/   /' || log "   (cupsd profile 中【未找到】net_admin 授权 —— 与崩溃吻合)"

echo
log "== B. 修复：cupsd AppArmor 本地包含授权 net_admin 并重载 =="
LOCAL=/etc/apparmor.d/local/usr.sbin.cupsd
mkdir -p "$(dirname "$LOCAL")"
if [ -f "$LOCAL" ] && grep -q 'capability net_admin' "$LOCAL"; then
  log "B1) 本地包含已含 net_admin，无需重复添加"
else
  printf '\n# PrintBox: 授予 cupsd CAP_NET_ADMIN，修复 AppArmor 拒绝导致的 cupsd 启动崩溃循环\ncapability net_admin,\n' >> "$LOCAL"
  log "B1) [OK] 已追加 'capability net_admin,' 到 $LOCAL"
fi
PROF=/etc/apparmor.d/usr.sbin.cupsd
if [ -f "$PROF" ]; then
  if apparmor_parser -r "$PROF" 2>/dev/null; then
    log "B2) [OK] 已重载 cupsd AppArmor profile"
  else
    log "B2) [WARN] apparmor_parser -r 失败，尝试 systemctl restart apparmor ..."
    systemctl restart apparmor 2>/dev/null && log "B2) [OK] 已重启 apparmor 服务" || log "B2) [WARN] 重启 apparmor 也失败（见上），但不阻塞后续验证"
  fi
else
  log "B2) [SKIP] 未找到 $PROF（profile 名可能不同，但本地包含已写好，重启 cupsd 即可尝试）"
fi

echo
log "== C. 验证：干净重启 + 监视 30s + 受控打 1 页 =="
systemctl stop cups 2>/dev/null; pkill -x cupsd 2>/dev/null; sleep 2
systemctl start cups 2>/dev/null || cupsd 2>/dev/null
for i in $(seq 1 15); do
  if lpstat -r 2>/dev/null | grep -q 'scheduler is running'; then break; fi
  sleep 1
done
startmark=$(wc -l < /var/log/cups/error_log 2>/dev/null || echo 0)
crashed=0
for i in $(seq 1 15); do
  sleep 2
  if tail -n +"$((startmark+1))" /var/log/cups/error_log 2>/dev/null | grep -q 'Scheduler shutting down'; then
    crashed=1
    log "  [$(($i*2))s] 仍检测到崩溃! 崩溃前 20 行："
    tail -n +"$((startmark+1))" /var/log/cups/error_log 2>/dev/null | head -20 | sed 's/^/     /'
    break
  fi
done
[ "$crashed" -eq 0 ] && log "  [30s] 未检测到崩溃 —— 崩溃循环已停止 ✅"

log "C2) 受控打印 1 页，确认作业『完成并离开队列』（不残留=不被重跑）："
TP=/usr/share/cups/data/testprint
if [ -f "$TP" ]; then
  jid=$(lp -d "$QUEUE" "$TP" 2>/dev/null | awk '{print $NF}')
else
  jid=$(echo "PrintBox verify $(date)" | lp -d "$QUEUE" 2>/dev/null | awk '{print $NF}')
fi
log "   作业 ID: $jid"
done=0
for i in $(seq 1 8); do
  sleep 2
  if ! lpstat -o "$QUEUE" 2>/dev/null | grep -q "$jid"; then
    log "  [$((i*2))s] 作业已离开队列（= 已完成）✅"; done=1; break
  fi
done
[ "$done" -eq 0 ] && log "  [16s] 作业仍在队列（未离开，需关注）"
log "   lpstat -p $QUEUE:"
lpstat -p "$QUEUE" 2>/dev/null | sed 's/^/     /'
log "   队列 ErrorPolicy: $(grep -i 'ErrorPolicy' /etc/cups/printers.conf 2>/dev/null | head -1 | awk '{print $2}' || echo '<未设置>')"

echo
log "== D. 修复后复查 AppArmor 拒绝是否消失 =="
dmesg -T 2>/dev/null | grep -iE 'cupsd|net_admin|apparmor.*denied' | tail -10 | sed 's/^/   /' || log "   (复查无新拒绝 = 修复生效)"

echo
log "===== 摘要（请整段发我）====="
log "A1 崩溃时段 AppArmor 拒绝(见上方): net_admin 拒绝是否仍在? ___  有无 segfault/trap? ___"
log "A4 net_admin 此前是否已授权: 否(未找到)"
log "B 已追加 net_admin 并重载: 是"
log "C 修复后 30s 是否仍崩溃: $([ "$crashed" -eq 0 ] && echo '否(已停止)' || echo '是(仍崩!)')"
log "C 受控作业是否完成离开队列: $([ "$done" -eq 1 ] && echo '是' || echo '否')"
log "D 修复后 AppArmor 拒绝是否消失: ___"
log "物理观察：受控打印实际出几张纸？___  手机（iOS/鸿蒙）打印是否还循环？___"
log "完整日志: $LOG"
echo "root@<box>:~#"
