#!/usr/bin/env bash
# PrintBox 盒上端到端排错脚本（固定流程的一部分）
# 用法（盒上 root，或 <box> 登录后 su -）：
#   bash /home/<box>/diag_box.sh
# 结果写入 /root/printbox-diag.txt，把该文件内容贴回即可。
set -u
OUT=/root/printbox-diag.txt
: > "$OUT"
exec > "$OUT" 2>&1

# 问题计数（VERDICT 汇总用）。单独记录 PPD 过滤器与默认纸张两类“致命但历史上被漏检”的问题。
PPD_FILTER_BAD=0
PAPER_BAD=0
RAW_BAD=0
AIRPRINT_BAD=0

banner() { echo; echo "==================== $* ===================="; }

banner "DATE"; date

banner "CUPSD.CONF (Browsing / BrowseLocalProtocols / Port / Listen)"
grep -nE '^[[:space:]]*Browsing|^[[:space:]]*BrowseLocalProtocols|^[[:space:]]*Port |^[[:space:]]*Listen ' /etc/cups/cupsd.conf 2>/dev/null || echo "(无法读取 cupsd.conf)"

banner "CUPSD.CONF SYNTAX CHECK (cupsd -t)"
cupsd -t 2>&1 || echo "(cupsd -t 不可用或配置存在错误)"

banner "INVALID/UNKNOWN DIRECTIVES IN CUPSD.CONF"
grep -niE 'BrowseRemoteProtocols|Unknown directive' /etc/cups/cupsd.conf 2>/dev/null && echo "!! 发现非法/未知指令（会导致 cupsd 解析失败、回退默认配置）" || echo "(无非法指令)"

banner "DRIVERLESS / DNSSD BACKEND STATE (崩溃循环相关)"
for b in driverless dnssd; do
  orig="/usr/lib/cups/backend/$b"
  dis="${orig}.disabled"
  if [ -e "$dis" ] && [ -e "$orig" ] && grep -q "PrintBox" "$orig" 2>/dev/null; then
    # 0.2.35+ 做法：原路径写空操作 stub（exit 0，含 PrintBox 标记），备份为 .disabled
    echo "$b: 已禁用（备份=$dis；原路径为 exit 0 stub，cups-driverd 拿到空输出不再崩溃）"
    echo "    stub 首行: $(head -1 "$orig" 2>/dev/null)"
  elif [ -e "$dis" ] && [ -e "$orig" ]; then
    echo "$b: !! 原路径 $orig 存在但非 PrintBox stub（可能被恢复为真实后端），需重跑 printbox repair"
  elif [ -e "$dis" ]; then
    echo "$b: 已禁用（备份=$dis，原路径 stub 缺失，建议重跑 printbox repair 重建）"
  elif [ -e "$orig" ]; then
    echo "$b: 仍启用 ($orig) !! 这会让 cups-driverd 扫描到本机 AirPrint 并崩溃，必须禁用"
  else
    echo "$b: 不存在（cups-filters 未提供）"
  fi
done

banner "PRINTER(S) -p -l"
lpstat -p -l 2>/dev/null

banner "DEVICE URI (lpstat -v，看真实后端：应为 usb:// 而非 hp:/usb)"
lpstat -v 2>/dev/null || echo "(无 lpstat -v 输出)"

banner "QUEUED JOBS"
lpstat -o 2>/dev/null

banner "QUEUE COUNT / DUPLICATE CHECK (iOS 双显根因)"
n=$(lpstat -p 2>/dev/null | grep -c '^printer' || true)
echo "队列总数: $n"
lpstat -p 2>/dev/null | awk '{print $2}' | sort > /tmp/pq_names 2>/dev/null
if [ "$n" -gt 1 ]; then
  echo "!! 发现 $n 个队列，疑似重复（同物理打印机不应 >1）。列出："
  cat /tmp/pq_names 2>/dev/null
  echo "—— 按型号归并检查（同一设备 URI 却多个队列名）:"
  lpstat -v 2>/dev/null
else
  if [ "$n" -eq 1 ]; then
    echo "队列数 = 1（符合预期）。队列名: $(cat /tmp/pq_names 2>/dev/null | tr '\n' ' ')"
  else
    echo "!! 队列数 = $n（应 = 1；当前无可用队列，iOS/鸿蒙/Win 均无法打印；注意：cupsd 若处于崩溃循环，lpstat 连不上会显示 0，这是假零，需结合 STABILITY 段判断）"
  fi
fi
# 特别检查：是否存在 _printbox 双队列（hp_laserjet_p1505n_printbox 与 laserjet_p1505n_printbox 并存）
hp=$(lpstat -p 2>/dev/null | awk '{print $2}' | grep -c 'hp_laserjet_p1505n_printbox' || true)
short=$(lpstat -p 2>/dev/null | awk '{print $2}' | grep -c 'laserjet_p1505n_printbox' || true)
if [ "$hp" -ge 1 ] && [ "$short" -ge 1 ]; then
  echo "!! 双队列并存（hp_laserjet_p1505n_printbox + laserjet_p1505n_printbox）—— 0.2.37 的 prune_phantom_printers 应已收敛，若仍存在请重跑 printbox repair"
fi

banner "PRINTER ERROR POLICY (重打根因关键证据)"
echo "--- /etc/cups/printers.conf 中的 printer-error-policy / ErrorPolicy ---"
grep -iE 'printer-error-policy|errorpolicy' /etc/cups/printers.conf 2>/dev/null || echo "(printers.conf 中未显式设置 -> 用 CUPS 默认值)"
echo "--- CUPS 全局默认 ErrorPolicy (cupsd.conf) ---"
grep -iE '^[[:space:]]*ErrorPolicy' /etc/cups/cupsd.conf 2>/dev/null || echo "(cupsd.conf 未设置 -> CUPS 编译期默认，通常为 retry-job)"
echo ">>> 若此处显示 retry-job / retry-current-job（或为空=默认 retry），则『同一作业被打 N 遍』即 cupsd 在重试同一 job-ID；正确应为 abort-job"

banner "JOB RETRY / BACKEND FAILURE 证据 (error_log)"
echo "--- 聚焦 grep：retry / backend returned / Filter failed / held / aborted / RECOVERABLE ---"
grep -iE 'retry|backend returned|Filter failed|job.*held|job.*abort|RECOVERABLE|printer-state-changed|stopped| destinations|now|sleeping' /var/log/cups/error_log 2>/dev/null | tail -40 || echo "(无匹配行)"
echo "--- 最近一次测试打印前后，作业状态如何变化 ---"
lpstat -l -o 2>/dev/null || echo "(无在途作业)"

banner "DEVICE URI (lpoptions)"
lpoptions -p hp_laserjet_p1505n_printbox -l 2>/dev/null | head -20 || echo "(无 lpoptions 输出)"

banner "AVAILABLE DRIVERS MATCHING 1505/foo2"
lpinfo -m 2>/dev/null | grep -iE '1505|p1505|foo2' || echo "(无匹配驱动)"

banner "FILTERS / TOOLS"
ls -l /usr/lib/cups/filter/ 2>/dev/null | grep -E 'foo2|zjs|xqx|pdftopdf|foomatic|imagetopdf' || echo "(filter 目录无匹配)"
for b in gs foomatic-rip; do command -v "$b" >/dev/null 2>&1 && echo "$b: OK ($(command -v "$b"))" || echo "MISSING: $b"; done
[ -x /usr/lib/cups/filter/pdftopdf ] && echo "pdftopdf: OK (/usr/lib/cups/filter/pdftopdf)" || echo "MISSING: pdftopdf"

banner "PACKAGES"
dpkg -l 2>/dev/null | grep -iE 'foo2|ghostscript|cups-filters|printer-driver|gs-gpl' || echo "(无匹配包)"

banner "OUR AIRPRINT SERVICE FILES"
ls -la /etc/avahi/services/ 2>/dev/null || echo "(无 avahi services 目录)"

banner "DYNAMIC AIRPRINT SERVICE STATE"
systemctl is-active printbox-airprint-publish.service 2>/dev/null
systemctl is-enabled printbox-airprint-publish.service 2>/dev/null

banner "AVAHI BROWSE _ipp._tcp (p1505)"
avahi-browse -rt _ipp._tcp 2>/dev/null | grep -iA8 p1505 || echo "(avahi-browse 无输出或缺失)"

banner "AVAHI BROWSE _universal._sub._ipp._tcp (p1505)"
avahi-browse -rt _universal._sub._ipp._tcp 2>/dev/null | grep -iA8 p1505 || echo "(avahi-browse 无输出或缺失)"

banner "AIRPRINT SERVICE FILE CHECK (iOS/鸿蒙/Win 发现根因)"
# 每个「有效（非 raw、有 PPD）」队列都必须有对应的 avahi 服务文件 airprint-<name>.service。
# 历史上 0.2.40 升级后队列落成坏队列 → 发布阶段静默跳过 → 服务文件目录为空 →
# iOS「看不到 A4 / 打不了」、鸿蒙「搜不到打印机」。这里一旦发现有效队列却无对应服务文件，直接判 FAIL。
any_valid=0
for q in $(lpstat -p 2>/dev/null | awk '{print $2}'); do
  ppd="/etc/cups/ppd/${q}.ppd"
  svc="/etc/avahi/services/airprint-${q}.service"
  if [ -f "$ppd" ] && ! grep -iqw 'raw' "$ppd"; then
    any_valid=1
    if [ -f "$svc" ]; then
      echo "OK: 队列 $q 有对应 AirPrint 服务文件 $svc"
    else
      echo "!! 队列 $q 为有效驱动却缺少 AirPrint 服务文件 $svc（iOS/鸿蒙/Win 将搜不到打印机）"
      AIRPRINT_BAD=1
    fi
  fi
done
[ "$any_valid" -eq 0 ] && echo "(无有效队列，跳过 AirPrint 服务文件检查)"
[ -d /etc/avahi/services ] && ls -la /etc/avahi/services/ 2>/dev/null

banner "WRAPPER SCRIPT (foo2xqx PDF 归一化)"
echo "--- 旧路径 /usr/local/bin/printbox-foo2xqx-pdf（0.2.34 及之前误装，AppArmor 拒执行，必须不存在）---"
if [ -e /usr/local/bin/printbox-foo2xqx-pdf ]; then
  echo "!! 仍存在旧 wrapper（/usr/local/bin/printbox-foo2xqx-pdf）—— AppArmor 会拒执行导致 0 字节不出纸，需删除"
  ls -l /usr/local/bin/printbox-foo2xqx-pdf 2>/dev/null
else
  echo "旧 wrapper 不存在 (正确)"
fi
echo "--- 新路径 /usr/lib/cups/filter/printbox-foo2xqx-pdf（AppArmor 放行目录，0.2.35 起必须存在且可执行）---"
if [ -x /usr/lib/cups/filter/printbox-foo2xqx-pdf ]; then
  echo "新 wrapper 存在且可执行 (正确)"
  head -3 /usr/lib/cups/filter/printbox-foo2xqx-pdf 2>/dev/null
else
  echo "!! 新 wrapper 缺失或不可执行（/usr/lib/cups/filter/printbox-foo2xqx-pdf）—— 打印会 0 字节"
  ls -l /usr/lib/cups/filter/printbox-foo2xqx-pdf 2>/dev/null
fi

banner "LIVE PPD FILTER PATH (致命：作业 0 字节 / cupsd 崩溃根因)"
# 仅检查真实存在的队列 PPD；不只看 wrapper 文件是否存在，还要核验 PPD 真的指向它。
PPD_FILE=""
for q in $(lpstat -p 2>/dev/null | awk '{print $2}'); do
  ppd="/etc/cups/ppd/${q}.ppd"
  if [ -f "$ppd" ]; then
    PPD_FILE="$ppd"
    echo "--- 队列 $q 的 PPD: $ppd ---"
    grep -nE '^\*cupsFilter|^[[:space:]]*\*cupsFilter|^\*cupsFilter2' "$ppd" 2>/dev/null || echo "(无 cupsFilter 行)"
    # 致命：PPD 仍指向 0.2.34 删掉的旧 wrapper（AppArmor 拒执行 → 作业 0 字节、cupsd 崩溃）
    if grep -q '/usr/local/bin/printbox-foo2xqx-pdf' "$ppd"; then
      echo "!! 致命：PPD 仍指向已删除的旧 wrapper /usr/local/bin/printbox-foo2xqx-pdf"
      echo "   → 必须重跑 printbox repair（0.2.37 起 repair 会强制把 PPD 过滤器重定向到 /usr/lib/cups/filter/）"
      PPD_FILTER_BAD=1
    fi
    # PPD 引用的过滤器是否真实存在且可执行（cupsFilter 行里形如 "/usr/lib/cups/filter/xxx"）
    for f in $(grep -oE '"/usr/lib/cups/filter/[^"]+"' "$ppd" 2>/dev/null | tr -d '"' | sort -u); do
      if [ -x "$f" ]; then
        echo "OK: PPD 过滤器 $f 存在且可执行"
      else
        echo "!! PPD 引用的过滤器 $f 不存在/不可执行（作业将失败）"
        PPD_FILTER_BAD=1
      fi
    done
  else
    echo "!! 队列 $q 的 PPD 不存在: $ppd"
    PPD_FILTER_BAD=1
  fi
done
[ -z "$PPD_FILE" ] && echo "(无队列 PPD 可检查)"

banner "DRIVER / RAW CHECK (队列是否为真驱动，而非 raw)"
# raw 队列 = 没有真实 PPD：无 A4 选项、PDF 直传打印机会报错灯。这是 0.2.36~0.2.38 反复回潮的根因之一。
for q in $(lpstat -p 2>/dev/null | awk '{print $2}'); do
  ppd="/etc/cups/ppd/${q}.ppd"
  if [ -f "$ppd" ]; then
    nick=$(grep -m1 '^\*NickName:' "$ppd" | sed 's/^\*NickName:[[:space:]]*//; s/"//g')
    model=$(grep -m1 '^\*ModelName:' "$ppd" | sed 's/^\*ModelName:[[:space:]]*//; s/"//g')
    echo "队列 $q -> NickName: ${nick:-?} | ModelName: ${model:-?}"
    if echo "$nick $model" | grep -iqw 'raw'; then
      echo "!! 队列 $q 是 RAW 队列（无真实驱动）：无 A4 选项、PDF 直传打印机会报错灯，必须重建为 foo2zjs"
      RAW_BAD=1
    else
      echo "OK: 队列 $q 为真实驱动 ($nick)"
    fi
  else
    echo "!! 队列 $q 无 PPD 文件 ($ppd)"
    RAW_BAD=1
  fi
done
[ -z "$(lpstat -p 2>/dev/null | awk '{print $2}')" ] && echo "(无队列可检查)"

banner "DEFAULT PAPER SIZE (A4 校验)"
# iOS/鸿蒙/Win 默认纸张应为 A4；若 PPD 默认 Letter 或队列级默认非 A4，会导致客户端看不到/默认错纸张。
for q in $(lpstat -p 2>/dev/null | awk '{print $2}'); do
  def=$(lpoptions -p "$q" -l 2>/dev/null | grep -E '^PageSize|^"PageSize' | grep -oE '\*[A-Za-z0-9.]+' | head -1)
  echo "队列 $q 当前默认纸张: ${def:-?}"
  if [ "${def#\*}" = "A4" ]; then
    echo "OK: 默认 A4"
  else
    echo "!! 默认纸张非 A4（当前 ${def}）—— iOS/鸿蒙/Win 可能默认 Letter 或看不到 A4"
    PAPER_BAD=1
  fi
done

banner "SUBMIT TEST PRINT + CAPTURE FILTER ERROR"
lp -d hp_laserjet_p1505n_printbox /usr/share/cups/data/testprint 2>&1
sleep 6
echo "----- error_log tail (after test print) -----"
tail -n 60 /var/log/cups/error_log 2>/dev/null
echo "----- job state after test print -----"
lpstat -o 2>/dev/null

banner "JOURNALCTL CUPS (crash reason)"
journalctl -u cups --no-pager -n 40 2>/dev/null | tail -40 || echo "(journalctl 无 cups 单元日志)"

banner "REAL CRASH REASON (cupsd 前台进程捕获真实致命错误)"
echo "--- 停止 cups 后以单前台进程运行 cupsd，捕捉『Scheduler shutting down』之前真正的致命原因（约 8 秒后自动结束并重启 cups）---"
systemctl stop cups 2>/dev/null
pkill -x cupsd 2>/dev/null
sleep 2
timeout 8 cupsd -f -l > /tmp/cupsd-foreground.log 2>&1 &
CPID=$!
sleep 9
echo "----- cupsd 前台 8 秒日志（真实致命原因通常就在这里）-----"
cat /tmp/cupsd-foreground.log 2>/dev/null
pkill -x cupsd 2>/dev/null
sleep 1
systemctl start cups 2>/dev/null
sleep 2

banner "PRINTERS.CONF STATE (崩溃循环常因其中残留坏队列/raw 队列)"
ls -l /etc/cups/printers.conf* 2>/dev/null || echo "(无 printers.conf 相关文件)"
echo "--- /etc/cups/printers.conf 内容 ---"
cat /etc/cups/printers.conf 2>/dev/null || echo "(printers.conf 不存在 -> cupsd 应从空配置干净启动)"

banner "DRIVER ID FORMAT (lpinfo -m 实际返回的 model id 是否带代价前缀 :0/)"
echo "--- 本机 lpinfo -m 中 p1505/foo2 相关条目（注意是 foo2zjs:0/ppd/... 还是 foo2zjs:/ppd/...）---"
lpinfo -m 2>/dev/null | grep -iE 'p1505|foo2zjs' | head -10 || echo "(lpinfo -m 无输出)"

banner "CUPS-DRIVERD 崩溃源 (.defs 文件)"
echo "--- indexbraille 包是否仍在（其坏 .defs 会令 cups-driverd 崩溃 -> Scheduler shutting down）---"
dpkg -l 2>/dev/null | grep -i indexbraille || echo "(indexbraille 未安装)"
echo "--- /usr/share/cups/drv/*.defs 列表（注意 0 字节或异常小的文件，可能损坏）---"
ls -l /usr/share/cups/drv/ 2>/dev/null || echo "(无 drv 目录)"
echo "--- 其他已知坏 .defs：indexbraille-media / indexbraille-filter ---"
ls -l /usr/share/cups/drv/indexbraille-media.defs /usr/share/cups/drv/indexbraille-filter.defs 2>/dev/null || echo "(indexbraille .defs 不存在)"

banner "APPARMOR / AUDIT (cupsd 启动所需资源被拒会导致崩溃)"
echo "--- dmesg 中 apparmor 拒绝 ---"
dmesg 2>/dev/null | grep -i apparmor | tail -20 || echo "(dmesg 无 apparmor 记录或不可读)"
echo "--- audit 日志中 denied（cupsd 相关）---"
grep -i denied /var/log/audit/audit.log 2>/dev/null | grep -i cups | tail -20 || echo "(audit 日志无 cups denied 记录)"

banner "FULL ERROR_LOG TAIL (不过滤，看『Scheduler shutting down』之前的真实报错)"
tail -n 80 /var/log/cups/error_log 2>/dev/null || echo "(无 error_log)"

banner "FULL JOURNALCTL CUPS (不过滤，捕捉 stderr/panic)"
journalctl -u cups --no-pager -n 200 2>/dev/null | tail -120 || echo "(journalctl 无 cups 单元日志)"

banner "CUPS STABILITY (crash-loop check)"
echo "cupsd active: $(systemctl is-active cups 2>/dev/null || echo '?')"
echo "cupsd syntax: $(cupsd -t 2>&1 | head -1)"
echo "Browsing line: $(grep -iE '^[[:space:]]*Browsing' /etc/cups/cupsd.conf 2>/dev/null || echo '(无)')"
echo "BrowseRemoteProtocols present? $(grep -qiE '^[[:space:]]*BrowseRemoteProtocols' /etc/cups/cupsd.conf 2>/dev/null && echo 'YES !! 非法指令，必须删除' || echo 'no (正确)')"
echo "driverless backend: $([ -e /usr/lib/cups/backend/driverless.disabled ] && echo 'disabled (正确)' || ([ -e /usr/lib/cups/backend/driverless ] && echo 'ENABLED !! 会崩溃' || echo 'absent'))"
# 注意：journalctl --since 不接受 '-2m' 这种写法（静默返回空 → 计数恒为 0，漏报崩溃）。
# 改用绝对时间（date -d '2 minutes ago'）；macOS/BSD 用 date -v 兜底。
TWO_MIN_AGO=$(date -d '2 minutes ago' '+%Y-%m-%d %H:%M:%S' 2>/dev/null || date -v -2M '+%Y-%m-%d %H:%M:%S' 2>/dev/null)
FIVE_MIN_AGO=$(date -d '5 minutes ago' '+%Y-%m-%d %H:%M:%S' 2>/dev/null || date -v -5M '+%Y-%m-%d %H:%M:%S' 2>/dev/null)
# cupsd 的 “Scheduler shutting down due to program error” 只写进 error_log、不进 journald；
# journald 里对应的真实崩溃信号是 cups.service 的 “status=1/FAILURE”。下面两者取较大值，
# 确保“cupsd 崩溃循环”不再被漏报为 0（0.2.36/0.2.37 的 diag 曾因此误报健康）。
_CRASH_J=$(journalctl -u cups --since "$TWO_MIN_AGO" 2>/dev/null | grep -c 'status=1/FAILURE')
_CRASH_L=$(tail -n 300 /var/log/cups/error_log 2>/dev/null | grep -c 'Scheduler shutting down')
if [ "$_CRASH_J" -gt "$_CRASH_L" ]; then CUPS_CRASHES=$_CRASH_J; else CUPS_CRASHES=$_CRASH_L; fi
echo "crashes in last 2 min: $CUPS_CRASHES (journald status=1/FAILURE=$_CRASH_J, error_log Scheduler shutting down=$_CRASH_L)"
echo "Bad line from driverless in last 5 min: $(journalctl -u cups --since "$FIVE_MIN_AGO" 2>/dev/null | grep -c 'Bad line from \"driverless\"')"

banner "VERDICT (PrintBox 健康核验)"
VER=$(printbox --version 2>/dev/null || printbox -V 2>/dev/null || pip show printbox 2>/dev/null | awk -F': ' '/^Version/ {print $2}' || echo '未知')
echo "PrintBox 版本: $VER"
issues=0
# 0) 致命：Live PPD 仍指向已删除的旧 wrapper / 引用的过滤器不存在
if [ "$PPD_FILTER_BAD" -eq 1 ]; then
  echo "[FAIL] Live PPD 过滤器路径异常（仍指向 /usr/local/bin 旧 wrapper 或引用不存在的过滤器）—— 作业 0 字节、cupsd 崩溃根因"; issues=$((issues+1))
else
  echo "[OK]   Live PPD 过滤器路径正确（指向 /usr/lib/cups/filter/ 且可执行）"
fi
# 0b) 默认纸张应为 A4
if [ "$PAPER_BAD" -eq 1 ]; then
  echo "[FAIL] 默认纸张非 A4（iOS/鸿蒙/Win 可能默认 Letter 或看不到 A4）"; issues=$((issues+1))
else
  echo "[OK]   默认纸张 A4"
fi
# 0c) 队列必须为真实驱动（非 raw）
if [ "$RAW_BAD" -eq 1 ]; then
  echo "[FAIL] 存在 RAW 队列（无真实驱动：无 A4 选项、PDF 直传打印机会报错灯，必须重建为 foo2zjs）"; issues=$((issues+1))
else
  echo "[OK]   队列均为真实驱动（非 raw）"
fi
# 1) cupsd 不崩：无 BrowseRemoteProtocols、cupsd -t 通过、近 2 分钟无 Scheduler shutting down
if grep -qiE '^[[:space:]]*BrowseRemoteProtocols' /etc/cups/cupsd.conf 2>/dev/null; then
  echo "[FAIL] cupsd.conf 含非法指令 BrowseRemoteProtocols（会解析失败、回退 Browsing On 崩溃）"; issues=$((issues+1))
else echo "[OK]   cupsd.conf 无 BrowseRemoteProtocols 非法指令"; fi
if cupsd -t >/dev/null 2>&1; then echo "[OK]   cupsd -t 校验通过"; else echo "[FAIL] cupsd -t 校验失败"; issues=$((issues+1)); fi
crashes=$CUPS_CRASHES
bad=$(journalctl -u cups --since "$FIVE_MIN_AGO" 2>/dev/null | grep -c 'Bad line from "driverless"')
echo "近 2 分钟 cupsd 崩溃次数: $crashes / 近 5 分钟 driverless 坏行次数: $bad"
[ "$crashes" -gt 0 ] && { echo "[FAIL] cupsd 仍在崩溃循环"; issues=$((issues+1)); } || echo "[OK]   cupsd 无崩溃"
[ "$bad" -gt 0 ] && { echo "[FAIL] driverless 仍在产出坏行"; issues=$((issues+1)); } || echo "[OK]   driverless 无坏行"
# 2) 单队列：iOS 不双显
qn=$(lpstat -p 2>/dev/null | grep -c '^printer' || true)
if [ "$qn" -eq 1 ]; then echo "[OK]   队列数 = 1（iOS 只看到一台）"; else echo "[FAIL] 队列数 = $qn（应 =1，否则 iOS 双显）"; issues=$((issues+1)); fi
# 3) wrapper 在放行目录且旧路径已清
[ -x /usr/lib/cups/filter/printbox-foo2xqx-pdf ] && echo "[OK]   foo2xqx wrapper 在 /usr/lib/cups/filter/ 且可执行" || { echo "[FAIL] 新 wrapper 缺失/不可执行（作业 0 字节）"; issues=$((issues+1)); }
[ -e /usr/local/bin/printbox-foo2xqx-pdf ] && { echo "[FAIL] 旧 /usr/local/bin wrapper 仍存在（AppArmor 拒执行）"; issues=$((issues+1)); } || echo "[OK]   旧 /usr/local/bin wrapper 已清理"
# 3b) 每个有效队列都应有对应 AirPrint 服务文件（iOS/鸿蒙/Win 发现的根因之一）
if [ "$AIRPRINT_BAD" -eq 1 ]; then
  echo "[FAIL] 存在有效队列但缺少对应 AirPrint 服务文件（iOS/鸿蒙/Win 将搜不到打印机）"; issues=$((issues+1))
else
  echo "[OK]   有效队列均已发布 AirPrint 服务文件"
fi
echo "============================================"
if [ "$issues" -eq 0 ]; then echo "VERDICT: 健康 ✅ 可正常打印（WebUI 测试页出纸、iOS/鸿蒙/Win 单台、默认 A4）"; else echo "VERDICT: 发现 $issues 项问题 ❌ 请重跑 printbox repair 后再次运行本脚本"; fi

banner "DONE"; echo "结果已写入 $OUT"
