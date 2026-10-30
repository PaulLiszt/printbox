#!/usr/bin/env bash
# tools/printbox-quickfix.sh —— 盒上「立刻验证 + 建队」一键脚本（需 root）
#
# 等价于把此前要你手敲的一堆命令固化为一个脚本，自动完成：
#   1) 稳定 cupsd：停服 → 修 /var/spool/cups/tmp 权限 → 禁用 hpfax/hp 私有后端 → 重启 → 等就绪
#   2) 自动定位 HP LaserJet P1505n 的真实 PPD 文件（递归查找，兼容 .ppd.gz）
#   3) 用 lpadmin -P <真实PPD> -o printer-error-policy=abort-job 建队（绝不落半残 raw 队列）
#   4) 自检：cupstestppd 校验 PPD、lpstat 看队列、确认 error-policy=abort-job、打印测试页
#   5) 把关键结果打印出来，整段回贴给我即可
#
# 用法（盒上 root）：
#   bash /home/<box>/printbox-quickfix.sh
#   跑完把末尾“===== 回贴摘要 =====”整段发我。
#
# 说明：本脚本不依赖新版本包，在当前已安装的 printbox 上即可立即救活打印；
#       随后若要“重启也自愈”，再单独跑 upgrade_box.sh 升级到 0.2.47。

set -u
# 不用 set -e：任一步失败都要继续跑完并报告，而不是半路退出。

QUEUE="${PRINTBOX_QUEUE:-hp_laserjet_p1505n_printbox}"
DEFAULT_URI="usb://HP/LaserJet%20P1505n?serial=KQ12ZY6"
DRIVER_HINT="foo2zjs:0/ppd/foo2zjs/HP-LaserJet_P1505n.ppd"
LOG="/tmp/printbox-quickfix-$(date +%Y%m%d-%H%M%S).log"

# 彩色/分隔辅助
hr(){ printf '%s\n' "------------------------------------------------------------"; }
log(){ printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
step(){ hr; log "== $* =="; }

# 运行命令并把 stdout/stderr 一起带回（用于诊断）。返回 rc。
run(){ "$@" 2>&1; return $?; }

# ---- 1. 稳定 cupsd ----
stabilize_cups(){
  step "1/5 稳定 cupsd（停服→修 spool 权限→禁用 hpfax/hp→重启）"
  run systemctl stop cups >/dev/null 2>&1
  pkill -x cupsd >/dev/null 2>&1
  sleep 1

  # 修 spool 目录权限（0.2.46 固化项：/var/spool/cups/tmp 缺失/权限错会导致 hpfax 启动时拖垮 cupsd）
  mkdir -p /var/spool/cups/tmp
  chmod 1770 /var/spool/cups/tmp
  chown root:lp /var/spool/cups/tmp
  chmod 1750 /var/spool/cups 2>/dev/null
  chown root:lp /var/spool/cups 2>/dev/null

  # 禁用 HP 私有后端（本机 foo2xqx USB 激光机完全用不到；hpfax 启动报 “Failed to create /var/spool/cups/tmp/.hplip” 会让 cupsd 崩溃循环）
  for b in hpfax hp; do
    f="/usr/lib/cups/backend/$b"
    if [ -e "$f" ] && [ ! -e "$f.disabled" ]; then
      mv "$f" "$f.disabled" && log "  禁用私有后端: $f"
    fi
  done

  run systemctl start cups
  # 等就绪（精确匹配 “scheduler is running”，避开 “not running” 的陷阱）
  local i ok=0
  for i in $(seq 1 20); do
    if lpstat -r 2>/dev/null | grep -qx "scheduler is running"; then ok=1; break; fi
    sleep 1
  done
  if [ "$ok" = "1" ]; then log "  cupsd 已就绪"; else log "  [WARN] cupsd 未在 20s 内就绪，继续尝试"; fi
}

# ---- 2. 定位真实 PPD 文件（镜像 _resolve_ppd_file_from_driver_id）----
find_ppd(){
  local roots="/usr/share/ppd /usr/share/cups/model /usr/lib/cups/drivers /usr/share/ppd/cups /usr/share/ppd/cupsfilters /usr/share/cups/drv /opt"
  local found=""
  for r in $roots; do
    [ -d "$r" ] || continue
    while IFS= read -r f; do
      case "$f" in
        *.ppd)
          found="$f"; break;;
        *.ppd.gz)
          local tmp="/tmp/printbox-$(basename "$f" .gz)"
          if gunzip -c "$f" > "$tmp" 2>/dev/null && [ -s "$tmp" ]; then found="$tmp"; fi
          break;;
      esac
    done < <(find "$r" -type f \( -iname '*p1505n*.ppd' -o -iname '*p1505n*.ppd.gz' \) 2>/dev/null)
    [ -n "$found" ] && break
  done
  # 兜底：上面根目录都不存在时，跨 /usr/share 与 /usr/lib 递归
  if [ -z "$found" ]; then
    while IFS= read -r f; do
      case "$f" in
        *.ppd) found="$f"; break;;
        *.ppd.gz)
          local tmp="/tmp/printbox-$(basename "$f" .gz)"
          if gunzip -c "$f" > "$tmp" 2>/dev/null && [ -s "$tmp" ]; then found="$tmp"; fi
          break;;
      esac
    done < <(find /usr/share /usr/lib -type f \( -iname '*p1505n*.ppd' -o -iname '*p1505n*.ppd.gz' \) 2>/dev/null)
  fi
  echo "$found"
}

# ---- 3. 自动探测设备 URI（优先标准 usb://，其次 hp:/usb）----
detect_uri(){
  local u
  u=$(lpinfo -v 2>/dev/null | grep -i 'usb://HP' | awk '{print $2}' | head -n1)
  if [ -z "$u" ]; then
    u=$(lpinfo -v 2>/dev/null | grep -i 'hp:/usb' | awk '{print $2}' | head -n1)
  fi
  echo "$u"
}

# ---- 4. 校验队列是否“真建好”（非 raw、PPD 文件存在）----
ppd_path(){ ls -1 /etc/cups/ppd/"$QUEUE".ppd 2>/dev/null; }

# 读队列真实 ErrorPolicy（来自 printers.conf，唯一真相源；不是 PPD 选项）
read_error_policy(){
  awk -v q="$QUEUE" '
    $1=="<Printer" && $2==q">"       {f=1; next}
    $1=="<DefaultPrinter" && $2==q">" {f=1; next}
    /^<\/(Printer|DefaultPrinter)>/    {f=0}
    f && $1=="ErrorPolicy"            {print $2}
  ' /etc/cups/printers.conf 2>/dev/null
}
is_raw(){
  local p; p=$(ppd_path)
  [ -z "$p" ] && return 0   # PPD 文件缺失 = 半残队列 → 视为“raw/坏”
  # 真实 PPD 已生成且能被 lpstat 识别为打印机，则非 raw
  lpstat -p "$QUEUE" 2>/dev/null | grep -qi 'printer' && return 1
  return 0
}

# ---- 5. 建队 ----
build_queue(){
  step "3/5 建队 $QUEUE"
  local uri="$1" ppd="$2"

  # 幂等：先摘掉旧队列
  lpadmin -x "$QUEUE" >/dev/null 2>&1 || true
  sleep 1

  local rc added=0
  # 优先：真实 PPD 文件直装（绕开驱动库路径解析，0.2.47 的兜底成功路径）
  if [ -n "$ppd" ]; then
    log "  用真实 PPD 安装: $ppd"
    lpadmin -p "$QUEUE" -E -v "$uri" -P "$ppd" -o printer-error-policy=abort-job
    rc=$?
    if [ $rc -eq 0 ] && [ -n "$(ppd_path)" ] && ! is_raw; then
      added=1; log "  [OK] lpadmin -P 安装成功且 PPD 真实"
    else
      log "  [WARN] lpadmin -P 未成/半残，准备走 -m 候选"
    fi
  fi

  # 回退：用 lpinfo -m 的驱动 id（优先精确 P1505n，其次任意 p1505；去代价前缀优先、原 id 其次）
  if [ "$added" != "1" ]; then
    local drv_list
    drv_list=$(lpinfo -m 2>/dev/null | grep -i 'p1505n' | awk '{print $1}')
    [ -z "$drv_list" ] && drv_list=$(lpinfo -m 2>/dev/null | grep -i 'p1505' | awk '{print $1}')
    if [ -z "$drv_list" ]; then drv_list="$DRIVER_HINT"; fi
    for cand in $(echo "$drv_list" | sed 's/:0\//:\//' ) $(echo "$drv_list"); do
      [ -z "$cand" ] && continue
      log "  尝试 lpadmin -m $cand"
      lpadmin -p "$QUEUE" -E -v "$uri" -m "$cand" -o printer-error-policy=abort-job 2>&1 | sed 's/^/    /'
      if [ -n "$(ppd_path)" ] && ! is_raw; then added=1; log "  [OK] 驱动 $cand 安装成功且 PPD 真实"; break; fi
      log "  [WARN] $cand 半残/无 PPD，删除重试"
      lpadmin -x "$QUEUE" >/dev/null 2>&1 || true
      sleep 1
    done
  fi

  if [ "$added" != "1" ]; then
    log "  [FAIL] 所有候选均无法建成真实队列。请回贴本日志，我定位 PPD 真实路径。"
    return 1
  fi

  # 关键：独立、显式强制 abort-job（不依赖上面 -m/-P 的 -o 是否生效）
  lpadmin -p "$QUEUE" -o printer-error-policy=abort-job 2>/dev/null
  # 放行作业 + 设为默认 + 共享（iOS/Win 发现用）
  lpadmin -p "$QUEUE" -o printer-is-shared=true 2>/dev/null
  lpoptions -d "$QUEUE" 2>/dev/null
  return 0
}

# ---- 6. 自检 ----
selfcheck(){
  step "4/5 自检"
  local p; p=$(ppd_path)
  if [ -z "$p" ]; then log "  [FAIL] 队列 PPD 不存在: /etc/cups/ppd/$QUEUE.ppd"; return 1; fi
  log "  PPD 路径: $p"
  if command -v cupstestppd >/dev/null 2>&1; then
    local out; out=$(cupstestppd "$p" 2>&1)
    if echo "$out" | grep -qi 'PASS'; then log "  [OK] cupstestppd: PASS"; else log "  [WARN] cupstestppd 输出:"; echo "$out" | sed 's/^/    /'; fi
  fi
  log "  lpstat -p $QUEUE:"
  lpstat -p "$QUEUE" 2>&1 | sed 's/^/    /'
  log "  ErrorPolicy(来自 printers.conf): '$(read_error_policy)'"
  # 设为接受+启用
  lpadmin -p "$QUEUE" -E 2>/dev/null
  accept "$QUEUE" 2>/dev/null
  return 0
}

# ---- 7. 测试页 ----
testpage(){
  step "5/5 打印测试页"
  local tf="/usr/share/cups/data/testprint"
  if [ -f "$tf" ]; then
    lp -d "$QUEUE" "$tf" 2>&1 | sed 's/^/    /'
  else
    printf 'PrintBox quickfix test page\n%s\n' "$(date)" | lp -d "$QUEUE" 2>&1 | sed 's/^/    /'
  fi
  sleep 2
  log "  当前作业："
  lpstat -o "$QUEUE" 2>&1 | sed 's/^/    /' || true
}

# ================= main =================
{
  hr
  log "PrintBox 一键验证+建队 ($(date))"
  log "队列名: $QUEUE | 默认 URI: $DEFAULT_URI | 驱动提示: $DRIVER_HINT"
  log "日志同时写入: $LOG"

  stabilize_cups

  step "2/5 探测设备 + 定位 PPD"
  URI="$(detect_uri)"
  if [ -z "$URI" ]; then URI="$DEFAULT_URI"; log "  lpinfo -v 未列出 USB 打印机，改用默认 URI: $URI"; else log "  探测到 URI: $URI"; fi

  PPD="$(find_ppd)"
  if [ -n "$PPD" ]; then log "  定位到真实 PPD: $PPD"; else log "  [WARN] 未找到 P1505n 的 .ppd/.ppd.gz，将依赖 lpadmin -m 候选"; fi

  log "  lpinfo -m 中 P1505 相关驱动 id："
  lpinfo -m 2>/dev/null | grep -i 'p1505' | sed 's/^/    /' || log "    (无)"

  if ! build_queue "$URI" "$PPD"; then
    hr; log "[结果] 建队失败，未对系统造成半成品残留（已清理）。请回贴本段日志。"
    hr
    exit 2
  fi

  selfcheck
  testpage

  hr
  log "===== 回贴摘要 ====="
  log "1) cupsd 状态: $(systemctl is-active cups 2>/dev/null)"
  log "2) 队列: $QUEUE"
  log "3) URI: $URI"
  log "4) PPD: $(ppd_path)"
  log "5) 是否 raw: $(if is_raw; then echo YES-异常; else echo 否-正常; fi)"
  log "6) lpstat -p:"
  lpstat -p "$QUEUE" 2>&1 | sed 's/^/    /'
  log "把以上整段发我即可。若测试页已出纸，打印服务即已恢复。"
  hr
} 2>&1 | tee "$LOG"
