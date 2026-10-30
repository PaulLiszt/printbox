#!/usr/bin/env bash
# PrintBox 崩溃循环根治脚本（盒上 root 执行）
# 设计：先取证（不改任何东西）→ 应用全部已知修复 → 干净重启验证。
# 一次跑完，结尾「===== 摘要 =====」整段发回即可。
set -u
LOG=/tmp/printbox-crashfix-$(date +%Y%m%d-%H%M%S).log
exec > >(tee -a "$LOG") 2>&1

ts(){ date '+%H:%M:%S'; }
log(){ echo "[$(ts)] $*"; }
BE=/usr/lib/cups/backend
QUEUE=hp_laserjet_p1505n_printbox

echo "============================================================"
log "PrintBox 崩溃循环根治脚本 ($(date))"
log "日志同时写入: $LOG"
echo "============================================================"

echo
log "== A. 当前状态快照（取证，暂不改动） =="
log "A1) cupsd.conf 中 Browsing / BrowseLocalProtocols："
grep -niE '^[[:space:]]*Browsing|^[[:space:]]*BrowseLocalProtocols' /etc/cups/cupsd.conf 2>/dev/null || log "    (两项都未显式设置，cupsd 用默认 Browsing On)"
log "A2) driverless / dnssd 后端类型（real 二进制 vs PrintBox 空操作 stub）："
for n in driverless driverless-fax dnssd; do
  p="$BE/$n"
  if [ -e "$p" ]; then
    if head -1 "$p" 2>/dev/null | grep -q 'PrintBox'; then
      log "    $n: 已是 PrintBox 空操作 stub（禁用生效）"
    else
      log "    $n: 仍是【真实后端】($(head -c 4 "$p" 2>/dev/null | xxd -p 2>/dev/null || echo '?')) —— 未禁用!"
    fi
  else
    log "    $n: 原路径不存在"
  fi
  [ -e "$p.disabled" ] && log "      ↳ 存在备份 $n.disabled"
done
log "A3) cups-browsed 状态: active=$(systemctl is-active cups-browsed 2>/dev/null) enabled=$(systemctl is-enabled cups-browsed 2>/dev/null)"
log "A4) error_log 累计 'Scheduler shutting down' 次数: $(grep -c 'Scheduler shutting down' /var/log/cups/error_log 2>/dev/null || echo 0)"
log "A5) error_log 'Bad line from' 次数: $(grep -c 'Bad line from' /var/log/cups/error_log 2>/dev/null || echo 0)"
log "A6) error_log 'Filter failed' 次数: $(grep -c 'Filter failed' /var/log/cups/error_log 2>/dev/null || echo 0)"

echo
log "== B. 抓取『干净重启后首次崩溃』的真实错误（关键取证） =="
log "B1) 停 cupsd ..."
systemctl stop cups 2>/dev/null; pkill -x cupsd 2>/dev/null; sleep 2
log "B2) 启动 cupsd 并监视 20s，捕获首次崩溃前上下文 ..."
systemctl start cups 2>/dev/null || cupsd 2>/dev/null
sleep 3
startmark=$(wc -l < /var/log/cups/error_log 2>/dev/null || echo 0)
log "    (只看重启后新增日志，基准行号=$startmark)"
crashed=0
for i in $(seq 1 10); do
  sleep 2
  if tail -n +"$((startmark+1))" /var/log/cups/error_log 2>/dev/null | grep -q 'Scheduler shutting down'; then
    crashed=1
    log "    [$(($i*2))s] 检测到崩溃! 首次崩溃前 40 行真实日志："
    tail -n +"$((startmark+1))" /var/log/cups/error_log 2>/dev/null | head -n 40 | sed 's/^/      /'
    break
  fi
  if [ "$i" -eq 10 ]; then
    log "    [20s] 未检测到崩溃 —— 重启后已稳定（若此前在崩，说明崩在周期性扫描而非启动）"
  fi
done

echo
log "== C. 应用全部已知修复（幂等，可重复执行） =="
# C1) cupsd.conf: Browsing Off + BrowseLocalProtocols none + 删除非法 BrowseRemoteProtocols
CF=/etc/cups/cupsd.conf
if [ -f "$CF" ]; then
  cp -a "$CF" "$CF.printbox-bak-$(date +%s)"
  if grep -qiE '^[[:space:]]*Browsing[[:space:]]' "$CF"; then
    sed -i -E 's/^[[:space:]]*Browsing[[:space:]].*/Browsing Off/I' "$CF"
  else echo 'Browsing Off' >> "$CF"; fi
  if grep -qiE '^[[:space:]]*BrowseLocalProtocols[[:space:]]' "$CF"; then
    sed -i -E 's/^[[:space:]]*BrowseLocalProtocols[[:space:]].*/BrowseLocalProtocols none/I' "$CF"
  else echo 'BrowseLocalProtocols none' >> "$CF"; fi
  sed -i -E '/^[[:space:]]*BrowseRemoteProtocols[[:space:]]/d' "$CF"
  if cupsd -t -c "$CF" >/dev/null 2>&1; then
    log "C1) [OK] cupsd.conf 已设 Browsing Off + BrowseLocalProtocols none，语法校验通过"
  else
    log "C1) [WARN] cupsd.conf 语法校验失败，请人工检查（已备份）"; cupsd -t -c "$CF" 2>&1 | tail -3 | sed 's/^/      /'
  fi
else
  log "C1) [SKIP] $CF 不存在"
fi

# C2) 禁用 driverless / driverless-fax / dnssd 后端（写 exit 0 stub + 备份真实后端）
log "C2) 禁用 driverless/dnssd 后端："
for n in driverless driverless-fax dnssd; do
  p="$BE/$n"; bak="$p.disabled"
  if [ -e "$p" ] && ! head -1 "$p" 2>/dev/null | grep -q 'PrintBox'; then
    if [ -e "$bak" ]; then rm -f "$p"; else mv "$p" "$bak"; fi
    chmod 644 "$bak" 2>/dev/null
    log "    已备份真实 $n → $bak"
  fi
  cat > "$p" <<'EOF'
#!/bin/bash
# PrintBox: 禁用 driverless/dnssd 后端（空操作），避免 cups-driverd 产出畸形行导致 cupsd 崩溃
exit 0
EOF
  chmod 755 "$p"
  log "    已写入 $n 空操作 stub"
done

# C3) 停用 cups-browsed（与本机固定 USB 打印机 + PrintBox 显式 AirPrint 发布冲突，常引发自引用/重复队列）
if systemctl is-active --quiet cups-browsed 2>/dev/null; then
  systemctl disable --now cups-browsed 2>/dev/null
  log "C3) [OK] 已停用 cups-browsed（本机无需其自动发现；如需恢复: systemctl enable --now cups-browsed）"
else
  log "C3) cups-browsed 未运行（无需处理）"
fi

echo
log "== D. 验证：干净重启 + 监视 30s + 受控打 1 页 =="
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
    log "  [$(($i*2))s] 仍检测到崩溃! 崩溃前 30 行："
    tail -n +"$((startmark+1))" /var/log/cups/error_log 2>/dev/null | head -n 30 | sed 's/^/      /'
    break
  fi
done
[ "$crashed" -eq 0 ] && log "  [30s] 未检测到崩溃 —— 崩溃循环已停止 ✅"

# 受控打 1 页，确认作业完成并离开队列（不残留 = 不再被重跑）
log "D2) 受控打印 1 页，监视作业是否『完成并离开队列』："
TP=/usr/share/cups/data/testprint
if [ -f "$TP" ]; then
  jid=$(lp -d "$QUEUE" "$TP" 2>/dev/null | awk '{print $NF}')
else
  jid=$(echo "PrintBox crash-loop verify $(date)" | lp -d "$QUEUE" 2>/dev/null | awk '{print $NF}')
fi
log "  作业 ID: $jid"
done=0
for i in $(seq 1 8); do
  sleep 2
  if ! lpstat -o "$QUEUE" 2>/dev/null | grep -q "$jid"; then
    log "  [$((i*2))s] 作业已离开队列（= 已完成，不会被重跑）✅"; done=1; break
  fi
done
[ "$done" -eq 0 ] && log "  [16s] 作业仍在队列（未离开，需关注）"
log "  lpstat -p $QUEUE:"
lpstat -p "$QUEUE" 2>/dev/null | sed 's/^/    /'
log "  队列 ErrorPolicy: $(grep -i 'ErrorPolicy' /etc/cups/printers.conf 2>/dev/null | head -1 | awk '{print $2}' || echo '<未设置>')"

echo
log "===== 摘要（请整段发我）====="
log "A1 Browsing/BrowseLocalProtocols: $(grep -niE '^[[:space:]]*Browsing|^[[:space:]]*BrowseLocalProtocols' /etc/cups/cupsd.conf 2>/dev/null | tr '\n' ' ')"
log "A2 driverless 后端: $([ -e "$BE/driverless" ] && (head -1 "$BE/driverless" | grep -q PrintBox && echo 'stub(禁用)' || echo 'REAL(未禁!)') || echo '不存在')"
log "A3 cups-browsed: $(systemctl is-active cups-browsed 2>/dev/null)"
log "B 干净重启首次崩溃前报错(见上方 B2 段): _____"
log "D 修复后 30s 是否仍崩溃: $([ "$crashed" -eq 0 ] && echo '否(已停止)' || echo '是(仍崩!)')"
log "D 受控作业是否完成离开队列: $([ "$done" -eq 1 ] && echo '是' || echo '否')"
log "物理观察：受控打印实际出几张纸？___  手机（iOS/鸿蒙）打印是否还循环？___"
log "完整日志: $LOG"
echo "root@<box>:~#"
