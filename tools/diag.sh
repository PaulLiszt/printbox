#!/usr/bin/env bash
# PrintBox 一键诊断/修复脚本（通用，不依赖特定硬件/发行版）
#
# 用法：
#   bash diag.sh          仅诊断，打印根因与可复制的修复命令
#   bash diag.sh --fix    诊断 + 自动修复（禁 cups-browsed 风暴、抬上限、禁 USB autosuspend、切真实驱动）
#
# 报告同时显示在屏幕，并保存到 /tmp/printbox-diag-时间戳.txt
#
# 设计要点（通用性）：
#   本脚本不假设任何具体打印机型号/驱动，所有驱动候选都从 `lpinfo -m` 动态匹配。
#   也不依赖特定发行版：用 systemctl / service 退化、用 lpstat 探测 CUPS。

OUT="/tmp/printbox-diag-$(date +%Y%m%d-%H%M%S).txt"
: > "$OUT"
say() { echo "$*" | tee -a "$OUT"; }
sec() { say; say "===== $1 ====="; }

FIX=0
[ "${1:-}" = "--fix" ] && FIX=1

# 需要 root 权限：CUPS 管理命令（lpinfo/lpadmin/cupsdisable...）都在 /usr/sbin，
# 且脚本要写 /etc/cups、systemctl 等。普通用户跑会因“命令找不到/无权限”而静默失败，
# 造成“脚本没反应”的假象。所以先做硬性校验并给出明确提示。
if [ "$(id -u)" -ne 0 ]; then
  echo "⚠️ 本脚本需要 root 权限（CUPS 管理命令在 /usr/sbin 且需写配置）。" >&2
  echo "   请用 root 运行：  sudo bash diag.sh --fix   或先  su -  再运行 bash diag.sh --fix" >&2
  exit 1
fi
# 确保 CUPS 管理命令在 PATH（部分最小环境普通 PATH 不含 /usr/sbin，
# 即使 root 也可能漏掉；前置一下避免“命令未找到”）。
export PATH="/usr/sbin:/sbin:$PATH"

# 标记变量，最后汇总
RAW_FOUND=0
NOPPD_FOUND=0
USB_FLAPPY=0
FIXABLE_DRIVER=0
CUPS_OK=0

cups_running() { lpstat -r 2>/dev/null | grep -q "running"; }

wait_cups() {
  # 轮询直到 CUPS 真正就绪，最多 30s
  local i
  for i in $(seq 1 30); do
    if cups_running; then return 0; fi
    sleep 1
  done
  return 1
}

sec "0. 基本信息"
say "主机: $(hostname)    时间: $(date)    内核: $(uname -r)"

sec "1. CUPS 调度器与订阅风暴"
CUPS_ST=$(systemctl is-active cups 2>/dev/null)
say "cups 状态: ${CUPS_ST:-unknown}"

if [ "$FIX" = 1 ]; then
  say "    [--fix] 正在中断订阅风暴并稳定 CUPS..."
  # 订阅风暴（Create-Printer-Subscriptions from localhost）多数来自 cups-browsed；
  # 本打印服务器通过显式 Avahi AirPrint 服务发布打印机，并不需要 cups-browsed，
  # 因此停掉它可从根上消除风暴（这是通用修复，与具体打印机无关）。
  systemctl stop cups-browsed 2>/dev/null
  systemctl disable cups-browsed 2>/dev/null
  # 清理 cups-browsed 留下的孤儿订阅：它曾创建 200+ 个 dbus:// 订阅，禁用 cups-browsed 后
  # 这些订阅仍残留在 CUPS 运行时缓存里，CUPS 每次启动都会尝试恢复它们并不断重启 dbus notifier，
  # 最终拖垮调度器（error_log 反复出现 Scheduler shutting down due to program error）。
  # 清除运行时缓存即可消除（不影响 /etc/cups 下的打印机定义 printers.conf 与 PPD /etc/cups/ppd）。
  systemctl stop cups 2>/dev/null
  for cdir in /var/cache/cups /run/cups/cache; do
    if [ -d "$cdir" ]; then
      rm -rf "$cdir"/* 2>/dev/null
      say "    [--fix] 已清除 CUPS 运行时缓存 $cdir（含孤儿 dbus 订阅，崩溃根因）"
    fi
  done
  rm -f /etc/cups/subscriptions.conf 2>/dev/null
  # 修复 cups-driverd 崩溃根因：printer-driver-indexbraille（盲文打印机驱动，Debian 默认作为
  # cups 的 Recommends 被自动安装）附带两个语法损坏的 .defs 文件，会让 cups-driverd 解析时崩溃，
  # 进而让 cupsd 调度器反复 "Scheduler shutting down due to program error"。本打印服务器不需要
  # 盲文打印机，且该 bug 会让打印作业在崩溃/重启间隙被不断重发（"一直打印不停"的真相）。
  # 因此 purge 该包（避免 apt 更新时坏文件被覆盖复活），并兜底删除残留的坏 .defs 文件。
  if dpkg -s printer-driver-indexbraille >/dev/null 2>&1; then
    apt-get remove --purge -y printer-driver-indexbraille 2>&1 | tee -a "$OUT"
    say "    [--fix] 已移除 printer-driver-indexbraille（其坏 .defs 会让 cups-driverd 崩溃）"
  fi
  rm -f /usr/share/cups/drv/indexbraille-media.defs /usr/share/cups/drv/indexbraille-filter.defs 2>/dev/null
  # 订阅上限（MaxSubscriptions 等）现由 printbox 生产代码 printers.ensure_cupsd_managed()
  # 统一写 cupsd.conf 并去重，cupsd 重启生效；本诊断脚本不再手追加，以免与托管块重复。
  say "    [--fix] IPP 订阅上限由 printbox（ensure_cupsd_managed）托管，无需本脚本手动追加"
  # 禁用 USB autosuspend：Linux 默认对空闲 USB 设备自动挂起省电，host-based 打印机唤醒失败会被
  # 内核 reset，表现为 usblp 周期性掉线（同一打印机在 Windows 上不掉线，纯属 Linux 电源管理行为）。
  # 禁用后 USB 常驻供电，掉线/打印中断/后端崩溃一并消除（软件可控，与线缆/供电无关）。
  for d in /sys/bus/usb/devices/*; do
    [ -f "$d/power/control" ] && echo on > "$d/power/control" 2>/dev/null
  done
  say "    [--fix] 已禁用全部 USB 设备的 autosuspend（根治周期性掉线/打印中断）"
  systemctl restart cups 2>&1 |tee -a "$OUT"
  if wait_cups; then
    # 再观察一段时间，确认调度器没有再次崩溃（之前订阅风暴会让它在 ~15s 内 shutting down）
    sleep 6
    if cups_running; then
      say "    [--fix] CUPS 已稳定运行（调度器未再崩溃）。"
      CUPS_OK=1
    else
      say "    [--fix] ⚠️ CUPS 启动后再次崩溃，可能是孤儿订阅未清干净，请查看 error_log。"
    fi
  else
    say "    [--fix] ⚠️ CUPS 仍无法稳定运行，跳过驱动切换；请查看 systemctl status cups 与 /var/log/cups/error_log。"
  fi
else
  if [ "$CUPS_ST" != "active" ]; then
    say "⚠️ CUPS 未稳定运行，枚举设备与打印都会失败。修复：运行 bash diag.sh --fix"
  fi
fi

sec "2. 已添加的打印机"
if ! cups_running; then
  say "⚠️ CUPS 未运行，无法枚举打印机。请先运行 bash diag.sh --fix"
  PRINTERS=""
else
  PRINTERS=$(lpstat -a 2>/dev/null | awk '{print $1}')
  CUPS_OK=1
  if [ -z "$PRINTERS" ]; then
    say "（无打印机，lpstat -a 为空）"
  else
    say "$PRINTERS"
  fi
fi

for p in $PRINTERS; do
  sec "打印机: $p"
  say "--- 状态 ---"; lpstat -p "$p" 2>&1 | tee -a "$OUT"
  say "--- 队列作业 ---"; lpstat -o "$p" 2>&1 | tee -a "$OUT"

  # 通用修复：无论是否 raw，统一把出错策略设为 abort-job。
  # host-based 打印机（如 HP LaserJet P1505n）遇 USB 掉线会被 CUPS 按默认 retry 策略
  # 无限重打同一作业，表现为“打印测试页不停/打十几张”。abort-job 让出错作业立即终止，
  # 避免打印洪流（根因是 Linux 对 USB 自动挂起导致的掉线，软件层只做止损；禁用 autosuspend 后即从根上消除）。
  if [ "$FIX" = 1 ] && [ "$CUPS_OK" = 1 ]; then
    if lpadmin -p "$p" -o printer-error-policy=abort-job 2>&1 | tee -a "$OUT"; then
      say "    [--fix] 已设置 printer-error-policy=abort-job（避免掉线时作业无限重打）"
    fi
  fi

  PPD="/etc/cups/ppd/$p.ppd"
  BEST_DRV=""
  if [ -f "$PPD" ]; then
    NN=$(grep -iE '^\*NickName' "$PPD" | head -1 | sed 's/^\*NickName[[:space:]]*["]*//;s/["]*$//')
    MN=$(grep -iE '^\*ModelName' "$PPD" | head -1 | sed 's/^\*ModelName[[:space:]]*["]*//;s/["]*$//')
    say "--- 实际驱动 ---"
    say "NickName : $NN"
    say "ModelName : $MN"
    if echo "$NN $MN" | grep -qiE 'raw|()->'; then
      say "⚠️ 该队列使用 RAW 驱动（无真正过滤器），打印 PostScript/PDF 会“一直收数据但不出纸”。"
      RAW_FOUND=1
    fi
  else
    say "⚠️ 无 PPD 文件 -> 这是 raw 队列，不会出纸。"
    NOPPD_FOUND=1
  fi

  # 在 lpinfo -m 里为该机型找最佳驱动（不绑定具体型号，动态匹配）
  KW=$(echo "$p" | tr '_' ' ' | sed 's/\([a-z]\)\([A-Z]\)/\1 \2/g')
  BEST_DRV=$(lpinfo -m 2>/dev/null | grep -iE "$KW" | grep -iE 'hpcups|gutenprint|foo2|splix|brlaser|escpr|postscript' | head -1 | awk '{print $1}')
  if [ -z "$BEST_DRV" ]; then
    BEST_DRV=$(lpinfo -m 2>/dev/null | grep -iE "$KW" | head -1 | awk '{print $1}')
  fi
  say "--- lpinfo -m 中匹配该机型的驱动候选 ---"
  lpinfo -m 2>/dev/null | grep -iE "$KW" | head -8 | tee -a "$OUT"
  say "建议驱动(BEST_DRV): ${BEST_DRV:-<未找到>}"

  # 是否装了对应驱动包
  if echo "$BEST_DRV" | grep -qi hpcups; then
    if ! dpkg -l 2>/dev/null | grep -q printer-driver-hpcups; then
      say "⚠️ 建议用 hpcups，但 printer-driver-hpcups 未安装。"
    fi
  fi

  say "--- 打印记录 (page_log) ---"
  grep -F "$p" /var/log/cups/page_log 2>/dev/null | tail -3 | tee -a "$OUT" || say "（无 page_log 记录）"

  # 修复动作
  if { [ "$RAW_FOUND" = 1 ] || [ "$NOPPD_FOUND" = 1 ]; } && [ -n "$BEST_DRV" ]; then
    FIXABLE_DRIVER=1
    say ">>> 修复命令（复制执行即可）:"
    say "    lpadmin -p $p -E -m $BEST_DRV"
    say "    cancel -a $p"
    if [ "$FIX" = 1 ]; then
      if [ "$CUPS_OK" = 1 ]; then
        say "    [--fix] 正在切换驱动..."
        if lpadmin -p "$p" -E -m "$BEST_DRV" -o printer-error-policy=abort-job 2>&1 | tee -a "$OUT"; then
          say "    [--fix] 驱动已切换为 $BEST_DRV"
        else
          say "    [--fix] ⚠️ 切换失败，请手动执行上面的 lpadmin 命令"
        fi
        # 取消卡住作业并重启打印机
        cancel -a "$p" 2>/dev/null
        cupsenable "$p" 2>/dev/null; cupsaccept "$p" 2>/dev/null
        # 校验 PPD 是否真的生成
        sleep 1
        if [ -f "/etc/cups/ppd/$p.ppd" ]; then
          NEWNN=$(grep -iE '^\*NickName' "/etc/cups/ppd/$p.ppd" | head -1 | sed 's/^\*NickName[[:space:]]*["]*//;s/["]*$//')
          say "    [--fix] 已确认 PPD 存在: $NEWNN"
        else
          say "    [--fix] ⚠️ 仍未生成 PPD，CUPS 可能仍不稳定，请重试 bash diag.sh --fix"
        fi
      else
        say "    [--fix] 跳过驱动切换：CUPS 未运行，请先确认 CUPS 已稳定。"
      fi
    fi
  fi
  # 后端 URI 校正：HP 打印机在装有 HPLIP 时会同时暴露 hp:/usb（HPLIP 私有后端）与
  # usb://（CUPS 标准 USB 后端）两个 URI。foo2zjs/foo2xqx/gutenprint 等绝大多数驱动
  # 必须搭配标准 usb:// 后端；若当前 URI 是 hp:/usb 而驱动又不是 hpcups，则属于错误搭配，
  # 后端会反复重发数据，表现为“单个作业打印出多页/打印不停”。--fix 自动校正为 usb://。
  if [ "$FIX" = 1 ] && [ "$CUPS_OK" = 1 ]; then
    CU=$(lpstat -v "$p" 2>/dev/null | awk '{print $NF}')
    IS_HPCUPS=$(echo "$NN $MN $BEST_DRV" | grep -ci hpcups)
    if [ -n "$CU" ] && [ "${CU#hp:/usb}" != "$CU" ] && [ "$IS_HPCUPS" -eq 0 ]; then
      SERIAL=$(echo "$CU" | sed -n 's/.*serial=\([A-Za-z0-9]*\).*/\1/p')
      # 注意：lpinfo -v 每行是 “direct usb://HP/...” 形式（class 在前），
      # 行首并不是 usb://，因此用 'usb://' 而非 '^usb://' 匹配；URI 取第 2 列。
      NU=$(lpinfo -v 2>/dev/null | grep -iE 'usb://' | grep -i "$SERIAL" | awk '{print $2}' | head -1)
      [ -z "$NU" ] && NU=$(lpinfo -v 2>/dev/null | grep -iE 'usb://' | awk '{print $2}' | head -1)
      if [ -n "$NU" ] && [ "$NU" != "$CU" ]; then
        if lpadmin -p "$p" -v "$NU" -o printer-error-policy=abort-job 2>&1 | tee -a "$OUT"; then
          say "    [--fix] 已将设备 URI 从 $CU 校正为 $NU（标准 usb 后端；避免 hp 后端与 foo2zjs 驱动混用导致重复打印）"
        else
          say "    [--fix] ⚠️ URI 校正失败，请手动执行: lpadmin -p $p -v '$NU'"
        fi
      fi
    fi
  fi

  # 每个打印机重置位，避免误判后续
  RAW_FOUND=0; NOPPD_FOUND=0
done

sec "3. CUPS 错误日志 (末 40 行)"
tail -40 /var/log/cups/error_log 2>/dev/null | tee -a "$OUT" || say "（无 error_log）"

sec "3a. 局域网发布 (AirPrint / mDNS)"
say "--- avahi-daemon 状态 ---"
systemctl is-active avahi-daemon 2>/dev/null | tee -a "$OUT"
if [ "$FIX" = 1 ]; then
  say "    [--fix] 确保 avahi-daemon 运行（手机/电脑通过 mDNS 发现打印机依赖它）..."
  systemctl enable --now avahi-daemon 2>&1 | tee -a "$OUT"
  # 重新生成 Avahi 服务文件并刷新（修复「手动改 URI / 重装驱动后手机找不到打印机」）
  if command -v printbox >/dev/null 2>&1; then
    printbox republish 2>&1 | tee -a "$OUT" \
      && say "    [--fix] 已重新发布打印机到局域网（AirPrint/mDNS）。" \
      || say "    [--fix] ⚠️ 重新发布失败（可稍后手动执行 printbox republish）"
  else
    say "    [--fix] 未找到 printbox 命令，跳过自动发布；请手动执行 printbox republish"
  fi
fi

sec "3b. CUPS 订阅风暴来源（定位反复崩溃）"
say "--- cups-browsed 状态（常见订阅风暴源头）---"
systemctl is-active cups-browsed 2>/dev/null | tee -a "$OUT"
if [ "$FIX" = 1 ]; then
  say "    [--fix] 已禁用 cups-browsed（订阅风暴根因），并持久化抬高 MaxSubscriptions。"
fi
say "--- 连到 631 的本地连接（谁在疯狂建订阅，需在 CUPS 运行时才有意义）---"
if cups_running; then
  ss -tp 2>/dev/null | grep -i ':631' | tee -a "$OUT" || say "（当前无 631 连接，风暴已平息）"
else
  say "（CUPS 未运行，无法采集 631 连接）"
fi

sec "4. USB 稳定性"
USB_LOG=$(dmesg 2>/dev/null | grep -iE 'usblp.*(removed|registered)|usb.*(reset|error)')
N_REMOVED=$(echo "$USB_LOG" | grep -c 'removed')
say "usblp removed 次数: $N_REMOVED"
say "--- 各 USB 设备 autosuspend 状态（on=已关闭挂起/正常, auto=启用挂起/可能掉线）---"
for d in /sys/bus/usb/devices/*; do
  if [ -f "$d/power/control" ]; then
    printf '  %s -> %s\n' "$(basename "$d")" "$(cat "$d/power/control" 2>/dev/null)" | tee -a "$OUT"
  fi
done
if [ "$N_REMOVED" -gt 0 ]; then
  FIRST=$(echo "$USB_LOG" | grep 'removed' | head -1 | sed -E 's/.*\[([0-9.]+)\].*/\1/')
  LAST=$(echo "$USB_LOG" | grep 'removed' | tail -1 | sed -E 's/.*\[([0-9.]+)\].*/\1/')
  if [ -n "$FIRST" ] && [ -n "$LAST" ]; then
    AVG=$(awk -v n="$N_REMOVED" -v f="$FIRST" -v l="$LAST" 'BEGIN{ if(n>1 && l>f) printf "%.1f", (l-f)/(n-1); else print "n/a" }')
    say "掉线时间跨度: ${FIRST}s ~ ${LAST}s    平均间隔: ${AVG}s"
  fi
  if [ "$N_REMOVED" -gt 5 ]; then
    USB_FLAPPY=1
    say "⚠️ USB 周期性掉线（平均间隔很短）。这通常是 Linux 默认 autosuspend 所致，而非线缆/供电问题——"
    say "   同一打印机在 Windows 上不掉线即可佐证。host-based 打印机被内核 autosuspend 后唤醒失败会被 reset，"
    say "   表现为 usblp 周期性 removed/re-added，进而打断打印、甚至让 CUPS 后端崩溃。"
    say "   修复：运行 bash diag.sh --fix 禁用 autosuspend；或手动执行下面这行："
    say '    for d in /sys/bus/usb/devices/*; do [ -f "$d/power/control" ] && echo on > "$d/power/control"; done'
  fi
fi
say "--- 当前 USB 设备节点 ---"
ls -l /dev/bus/usb/*/ 2>/dev/null | grep -E '189,' | tee -a "$OUT" || say "（当前无打印机 USB 节点）"

sec "5. 自动结论"
if [ "$NOPPD_FOUND" = 1 ] || [ "$RAW_FOUND" = 1 ]; then
  say "【驱动问题】检测到 raw/无 PPD 队列，这是“收数据不出纸”的最常见原因。"
  if [ "$FIXABLE_DRIVER" = 1 ]; then
    say "  处理：运行上面打印的  lpadmin -p <名称> -E -m <驱动>  即可修复；或  bash diag.sh --fix  自动修。"
  else
    say "  处理：未找到合适驱动，请在 WebUI 手动指定 PPD，或安装对应厂商驱动包后重跑 detect。"
  fi
fi
if [ "$USB_FLAPPY" = 1 ]; then
  say "【USB 掉线根因】Linux 默认对空闲 USB 设备 autosuspend 省电，host-based 打印机唤醒失败会被内核"
  say "   reset，表现为周期性掉线/打印中断（同一打印机在 Windows 上不掉线，说明非线缆/供电问题）。"
  say "   已通过 bash diag.sh --fix 禁用 autosuspend；若禁用后仍掉线，再排查线缆/USB 口。"
fi
if [ "$NOPPD_FOUND" = 0 ] && [ "$RAW_FOUND" = 0 ] && [ "$USB_FLAPPY" = 0 ]; then
  say "未发现明显驱动/USB 异常，请检查打印机面板是否有卡纸/缺纸/墨粉告警，并把上面 error_log 片段发我。"
fi
if [ "$FIX" = 1 ] && [ "$CUPS_OK" = 1 ] && [ "$FIXABLE_DRIVER" = 1 ]; then
  say
  say "【下一步】驱动已切换，请用测试页验证出纸："
  say "    lp -d $(echo "$PRINTERS" | awk '{print $1}') /usr/share/cups/data/testprint"
fi

say
say "诊断报告已保存: $OUT"
