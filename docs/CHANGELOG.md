# PrintBox 更新日志

所有版本均通过 `upgrade_box.sh` + `printbox repair` 升级链路交付；本机 CUPS 2.4 / Debian trixie / AppArmor 启用。

## 0.2.51（2026-10-30）— 根治「多份翻平方」：按 CUPS 实际拓扑自动选 wrapper 模式

0.2.50 升级后复测仍是 N²（iOS/Win/鸿蒙：2 份→4、3 份→9）。决定性再分析坐实根因比 0.2.49/0.2.50 设想的更深一层：

**Debian trixie 的 CUPS 默认在 PDF 链路自动插入 `pdftopdf`**（mime 转换 `application/pdf → application/vnd.cups-pdf`）。
pdftopdf 本职就是按 `COPIES` 把页数翻倍、处理页码范围、按 PPD 默认 A4 归一化页面尺寸。PrintBox 的 foo2xqx
包装脚本若**再**调一次 pdftopdf 处理 COPIES，即两次乘法 → 实际份数 = 请求份数²。

此前 0.2.49/0.2.50 都建立在「wrapper 内调一次 pdftopdf、foomatic 收 COPIES=1」上，这只对「CUPS 未自动插入
pdftopdf」的拓扑成立；本盒是 Debian trixie，CUPS 已自动插入，固定模式 B 会与上游 pdftopdf 叠加翻倍，且 0.2.50 的
新 wrapper 又因部署链路问题没落到盒上（盒子残留更旧 wrapper：foomatic 收 COPIES=$4 → 彻底 N²）。

修复（生产代码 + 一键脚本双轨）：
- `printers._cups_inserts_pdftopdf()`：直接读 CUPS mime 规则文件判定是否自动插入 pdftopdf（不依赖 cupsd 运行 / PPD）。
- `_install_foo2xqx_wrapper()` 据此自动选模式：
  - 模式 A（自动插入，绝大多数发行版）：wrapper **不再调 pdftopdf**，直接透传 + foomatic 收原始 COPIES（只乘一次）；
  - 模式 B（未插入，罕见）：wrapper 自调 pdftopdf(COPIES=N) 归一化 + foomatic 收 COPIES=1。
  两种模式都保证 copies 仅被处理一次。
- `tools/printbox-fix-copies.sh` 重写：自带诊断（mime 判定 + 当前 wrapper/PPD 抓取）→ 直接落盘正确 wrapper（绕开
  升级流程的所有不确定性）→ 修正 PPD 指向 → 重启 cupsd → 提交 `lp -n 2` 受控作业核对实际页数（预期 2）。
- 验证：`_install_foo2xqx_wrapper` 两种模式生成均经单测确认（模式 A 不再调 pdftopdf、模式 B foomatic 收 COPIES=1）；
  py_compile 通过。

## 0.2.50（2026-10-29）— 修 0.2.49 未生效：foo2xqx 包装脚本部署被安装时探测卡死

0.2.49 已写出正确的「foomatic 以 COPIES=1 调用」逻辑，但**升级后复测仍是 N²**，根因不在修复逻辑，
而在**部署链路**：`_install_foo2xqx_wrapper()` 在写盘前判断 `if not pdftopdf or not foomatic: return False`——
一旦部署那一刻 `_resolve_filter("pdftopdf")` 没找到（pdftopdf 装在 `/usr/lib/cups/filter/` 不在 `$PATH`，
探测偶发失败），包装脚本就**根本不写盘**，盒上永远残留旧版（把 COPIES 也传给 foomatic → N²）。
这正是「升级了却没修好」的真实原因。

修复：
- 去掉安装时的提前 `return False`，**无论探测成败都强制写盘**；探测结果仅作内联优选路径，脚本运行时
  再校验其可执行，不可用时回退 `command -v` / 过滤器目录。磁盘上的 wrapper 永远是修复后的最新版。
- 同步提供 `tools/printbox-fix-copies.sh`：盒上 root 一键把正确的 wrapper 直接写死到磁盘（绕开 Python 探测）、
  重启 cupsd、并用 `lp -n 2` 受控作业核对实际页数（预期 2），自带诊断与验证，无需等完整升级。
- WebUI（`webapp.self_url`）与 CLI 现已统一展示三个访问地址：本机 IP / `<主机名>.local` / `printbox.local`。

## 0.2.49（2026-10-29）— 修复「请求 N 份实际打出 N² 份」：foo2xqx 包装脚本重复计份

0.2.48 根治 cupsd 崩溃循环后，单份打印全部正常（iOS/鸿蒙/Win10 各 1 份），但 **「请求多份」出现份数翻平方**：
Win10 打印某页 2 份 → 3~4 份；iOS 打印 PDF 第 1 页 2 份 → 4 份；鸿蒙（华为打印）打印某 Word 2 页 × 2 份 → 8 页（4 份）。
1 份请求始终正确（1²=1），定位为「份数」专属缺陷而非崩溃类。

**真因**：`printers._install_foo2xqx_wrapper()` 生成的 `/usr/lib/cups/filter/printbox-foo2xqx-pdf` 包装脚本，
把 `COPIES` 同时传给了两步：
1. `pdftopdf`（cups-filters 的 PDF→PDF 归一化，本职就是按份数把页数翻倍、处理页码范围）= 得到 N 页；
2. `foomatic-rip`（下游 foo2xqx 驱动按 `COPIES` 再把页数乘一次）= N×N。

即 `实际份数 = 请求份数²`。pdftopdf 已在第 1 步把份数翻进页数，第 2 步必须令 `COPIES=1` 只打一遍。

修复：成功路径（pdftopdf 已产出合法 PDF）下，调用 `foomatic` 时硬置 `COPIES=1`；仅当 pdftopdf 失败回退到原始
`foomatic` 行为时，才保留原 `COPIES`（此时 pdftopdf 未生效，需由 foomatic 计份）。新增回归测试
`tests/test_foo2xqx_copies.py` 用假 `pdftopdf/foomatic` 验证「成功路径 foomatic 收到 1、回退路径保留原份数」。

附：补全版本号显示——`__init__.__version__` 此前停在 0.2.47（与 `pyproject.toml` 不一致，导致 `printbox --version`
与 WebUI 显示旧号），本版统一为 0.2.49。

## 0.2.48（2026-10-29）— 根治「双打 / iOS·鸿蒙无限循环」：禁用 cupsctl 对 PreserveJobFiles 的 live 重载

<box> 盒 2026-10-29 决定性取证（`printbox-diag-reload.sh`）坐实根因链：

1. 停掉所有 printbox 单元 → cupsd 30s 稳定不崩、期间无外部 PUT → 证明崩溃由 PrintBox 自身触发，非系统/驱动。
2. 用同款命令 `cupsctl PreserveJobFiles=yes PreserveJobHistory=yes` 复现 → cupsd 立即
   `Scheduler shutting down due to program error` 崩溃重启。
3. 禁用 cups.socket/cups.path 后同款命令仍崩 → 排除 Port 631 / 套接字激活冲突（此前猜测方向证伪）。
4. 反差线索：`cupsctl LogLevel=debug` 不崩，唯独 `PreserveJobFiles/PreserveJobHistory` 的 live 重载必崩。

**真因**：`enable_job_archive()`（及 archive 守护进程每轮调用的 `ensure_preserve`）通过 `cupsctl` 对
`PreserveJobFiles/PreserveJobHistory` 做 live 重载，而本盒 CUPS 2.4 在对该指令 live 重载时会让 cupsd 直接崩溃
重启；崩溃把活动/已完成作业重跑 → **双打 / iOS 首打 2 份 / 鸿蒙·iOS 无限循环**，printbox-guard 每 ~15s 一轮又把
雪崩放大。同时 `cupsctl` 重写 `cupsd.conf` 会剥掉注释，导致 `_ensure_cupsd_limits` 依赖的注释 mark 失效、每次
`init` 又追加 `MaxSubscriptions` 块，把 `cupd.conf` 撑到 40+ 遍重复（实证抓取确认），进一步放大崩溃。

修复：
- **彻底弃用 `cupsctl` 做这两条指令的 live 重载**：改为由 `printers.ensure_cupsd_managed()` 直接写
  `cupsd.conf` 文件（指令型配置，本只需在 cupsd 下次正常启动时落地，无需 live 重载）。
- 写盘前先判定「是否已满足」，满足则纯 no-op —— archive 守护进程每轮的 `ensure_preserve` 因此彻底变成空操作，
  崩溃循环从源头断开。
- 写盘用临时文件 + `os.replace` 原子替换，写前用 `cupsd -t` 预校验，失败保留原文件不破坏。
- `_ensure_cupsd_limits` 改用「按指令去重」代替「注释 mark」，并顺手把历史 `cupsctl` 撑出的 40+ 遍
  `MaxSubscriptions` 重复块收敛为唯一一份（`PreserveJobFiles/History` 同理），根治配置膨胀。
- 0.2.47 已固化的 `_ensure_cups_apparmor()`（AppArmor 授权 `net_admin`）**保留**：取证显示 net_admin 拒绝均为
  10-27 旧日志，10-29 已无，故非本次根因，但作为防御项无害保留。

交付同时提供 `tools/printbox-fix-reload.sh` 一键脚本：盒上 root 跑即完成「停 printbox 单元（断 cupsctl 循环）→
原子去重清理 cupsd.conf（40+ 遍重复块收敛为 1）→ `cupsd -t` 校验 → 干净重启 cupsd → 拉起 WebUI（printbox-web）
立即恢复访问」。随后走标准 `upgrade_box.sh` + `printbox repair` 升级到 0.2.48，自动重新启用 printbox-archive
（已修复、不再 cupsctl）获得完整归档能力。

## 0.2.47（2026-10-28）— 根治「`lpinfo -m` 偶发空 → `pick_driver` 误判 raw → 队列永远建不出」

0.2.46 升级后盒上实测：`printbox repair` 仍报 `驱动 raw 添加返回成功但队列无真实 PPD（半残）` 并最终 `lpstat -p` 空队列。
逐行比对日志与源码后定位到两层根因：

### A. `pick_driver` 在 `lpinfo -m` 偶发返回空时静默兜底 raw（致命）
`pick_driver()` 只调一次 `lpinfo -m`；本机 CUPS 2.4 下，`lpinfo -m` 在 cupsd 刚就绪、驱动后端（cups-driverd）
尚未枚举完成时**会偶发返回空**。此时 `pick_driver` 既拿不到型号匹配、也走不到 IPP 分支，直接返回 `None` →
`add_printer` 把真实 USB 打印机兜底成 `raw` → 被 CUPS 2.4 的 `Raw queues are deprecated` 拒绝 → 队列永远建不出。

修复：把 `lpinfo -m` 改为**先 `_ensure_cups()` 再重试最多 5 次、每次间隔 1s**，给驱动后端枚举时间；
仍为空才返回 `None`，交由 `add_printer` 对真实打印机显式报错（不再静默 raw）。

### B. `add_printer` 的 `lpadmin -P` 兜底路径是死路（次要但必须修）
0.2.46 新增的 `_resolve_ppd_file_from_driver_id` 把驱动 id `foo2zjs:0/ppd/foo2zjs/HP-LaserJet_P1505n.ppd`
拼成 `/usr/share/ppd/foo2zjs/HP-LaserJet_P1505n.ppd`，但盒上实测该路径**不存在**（foo2zjs PPD 实际安装路径随发行版变化，且多为 `.gz`）。
用户手工 `lpadmin -P /usr/share/ppd/foo2zjs/...ppd` 直接报 `Unable to open PPD file on line 0`，证实了这点。

修复：重写为「按文件名（如 `HP-LaserJet_P1505n.ppd`）在常见 PPD 根目录递归查找，同时匹配 `.ppd` 与 `.ppd.gz`；
命中 `.gz` 时就地解压到临时文件并返回，供 `lpadmin -P` 使用」。这样 `lpadmin -m` 两种候选都半残时，能真正定位到真实 PPD 文件绕开驱动库路径解析。

两处均附单测（`test_pick_driver_retry` / `test_resolve_ppd_gz`），`py_compile` + 全量单测通过。

## 0.2.46（2026-10-28）— 修复 `printbox repair` 漏建队列（cupsd 已稳却仍空队列）+ 驱动 id 解析兜底 + 修正 cupsd 就绪误判

0.2.45 用户升级后盒上实测：cupsd 崩溃循环已靠「修 /var/spool/cups/tmp 权限 + 禁用 hpfax/hp 私有后端」止住（该根因此前被 `Scheduler shutting down` 的通用报错掩盖），但 `printbox repair` 跑完 `lpstat -p` 仍**空队列**。
根因是 repair 的编排缺陷 + 一个潜在误判，分别如下：

### A. `repair` 在 cupsd 已稳时仍拆+重启它 → 制造 USB 枚举空窗 → `main()` 探测拿空漏建队列
`repair()` 里的 `_stabilize_cups_for_rebuild` 在停服前判定 `was_crashing = _cupsd_recently_crashed(120)`；
若最近 120s 内有过任意一次 cupsd 失败（用户手工稳住 cupsd 后那段「近 10s 1 次」的残留计数就满足），便走「核弹级重置」——
整体移走 printers.conf、删光 PPD、重启 cupsd。紧接着 `_rebuild_printers_with_guard` 立刻调 `main()` 建队列，
**而刚重启的 cupsd 需要数秒让 usb 后端枚举出设备**，`main()` 的探测恰逢其会拿到空 → 报「未发现打印机」→ 队列建不出来。
用户手工 `mv printers.conf; systemctl start cups` 后一次建队成功，正说明「cupsd 已稳就别再拆它」才是正确路径。

修复（已稳则不拆）：
1. **`_stabilize_cups_for_rebuild` 在停服前先判定 `cupsd 当前是否在跑 且 近 15s 无崩溃`**：若是 → **直接返回**，由后续 `main()` 在稳定 cupsd 上重建队列；
   仅在确实不稳定（崩溃循环）时才走离线清场/核弹级重置。等价于「cupsd 已稳就别动它」，彻底消除重建竞赛中的枚举空窗。
2. 已稳路径仍做轻量**在线 PPD 净化**（`_sanitize_ppd_filter_paths`，不改 printers.conf、不重启，安全）。
3. 单测 `test_stabilize_cups.py` 新增 `test_stabilize_skip_when_already_stable`：mock 证明「cupsd 在跑且无近期崩溃 → 跳过 mask/stop/start、不触发任何清场、仅做一次在线净化」。

### B. `add_printer` 在 `lpadmin -m <驱动id>` 两种候选都半残时仍建不出队列 → 新增 `-P` 真实 PPD 兜底
本机 `lpinfo -m` 给的驱动 id 带 `:N/` 代价前缀（如 `foo2zjs:0/ppd/...`），`lpadmin -m` 在本机 CUPS 2.4 可能「rc=0 却生成半残队列（PPD 实际缺失）」；
代码此前靠去前缀 `foo2zjs:/ppd/...` 兜底，但若两种 `-m` 形式都失败/半残，便直接报错无队列。
新增**从驱动 id 解析真实 PPD 文件路径、改用 `lpadmin -P <文件>` 直接安装**的兜底，彻底绕开驱动数据库路径解析问题：

1. **新增 `_resolve_ppd_file_from_driver_id(drv)`**：从 `foo2zjs:0/ppd/foo2zjs/HP-LaserJet_P1505n.ppd` 解析出 `/usr/share/ppd/foo2zjs/HP-LaserJet_P1505n.ppd`（标准 CUPS PPD 安装路径），文件存在才返回。
2. **`add_printer` 尝试循环**：在 `-m` 两种候选（去前缀优先、原 id 次之）均失败/半残后，若解析到真实 PPD 文件则回退 `lpadmin -P <文件> -o printer-error-policy=abort-job`；成功且生成真实 PPD 即接受。
3. 单测 `test_add_printer_ppd.py` 新增 `test_add_printer_ppd_fallback_when_m_both_half_broken`：模拟两种 `-m` 都半残、`-P` 成功，断言 `add_printer` 经 `-P` 兜底成功建队。

### C. 修正 cupsd「就绪」误判（潜在 bug，本次一并根治）
`lpstat -r` 在 cupsd **未运行**时输出 `scheduler is not running`，其中也含 `running` 子串；
原 `_cups_running()` / `_wait_cups_ready()` / 新增稳定判定都用 `if "running" in out` 粗匹配 → **会把「未运行」误判为「已就绪」**。
后果：`_ensure_cups()` 以为调度器已起而不去启动它；repair 把「not running」误当就绪跳过清场。

修复：三处一律改为精确匹配 `"scheduler is running"`，杜绝 `not running` 的假阳性。

### D. 固化 cupsd 崩溃循环的「真正」本地根因（spool 权限 + HP 私有后端）进代码，防重启复发
0.2.45 升级后盒上崩溃循环的最终真凶并非打印机配置，而是：
1) `/var/spool/cups/tmp` 等 CUPS 运行目录缺失/权限错，HP 私有后端 `hpfax` 启动即报 `Failed to create /var/spool/cups/tmp/.hplip` 拖垮 cupsd；
2) HP 私有后端 `hpfax`/`hp` 本机 foo2xqx USB 激光机完全用不到、且启动即报错。
用户手工修这两处后 cupsd 立刻稳、`lpinfo -m` 正常。为防盒重启复发，新增 `_fix_cups_spool_and_backends()` 并在 `provision()`/`repair()` 的 `_fix_cups_driverd()` 之后调用：
- 幂等修复 `/var/spool/cups/tmp`(1770)、`/var/spool/cups`、`/var/cache/cups`、`/run/cups`(0755)、`/var/log/cups`(0700) 的目录与 `root:lp` 归属；
- 将 `/usr/lib/cups/backend/hpfax`、`/usr/lib/cups/backend/hp` 改名 `.disabled` 备份（幂等，包升级可还原）。
- 单测 `test_stabilize_cups.py` 新增 `test_fix_cups_spool_and_backends` 验证目录创建与后端停用。

## 0.2.45（2026-10-28）— 核弹级重置兜底 + 强制 ErrorPolicy=abort-job 根治「同一作业被打 N 遍」；彻底关掉「升级后 cupsd 仍崩」的回归

0.2.44 升级后盒上实测：版本号已变 0.2.44，但 `printbox repair` 跑完 `cupsd` **仍崩（2 分钟 300 次 `Scheduler shutting down`）**、`lpstat` 全空、`队列数=0`；
三端里 Win10/WebUI 能偶尔出纸（用户手工 `mv printers.conf printers.conf.old; rm -f /etc/cups/ppd/*.ppd; systemctl start cups` 后一次成功），
但 iOS 仍失败、鸿蒙「一直打印不停、已出 5 页」、Win10「1 页×5 份 = 15 张分 3 批」。本版分别根治：

### A. 自动 repair 仍崩 → 正则编辑打不赢崩溃循环里的文件写崩（核弹级兜底）
`_stabilize_cups_for_rebuild` → `_wipe_managed_queues_offline` 是**用正则编辑** `printers.conf` 删坏块。
但崩溃循环里 `cupsd` 会把 `printers.conf` **截断/写崩**，正则匹配不到任何 `<Printer>` 块 → 编辑空手而归 → 损坏文件原样留下 →
`cupsd` 启动即崩、`main()` 永远没机会重建。用户手工**整体移走文件**（`mv printers.conf printers.conf.old`）才是稳住的真正原因。

修复（固化「整体移走文件」为自动兜底层）：
1. **新增 `_nuclear_reset_printers()`**：`mask`+`stop`+确保退出 → 把 `printers.conf` **整体 `os.rename` 移走**（带 `.bak.<时间戳>` 备份，非正则编辑）→ 删全部 PPD → `unmask`+`enable`+`start` → 等就绪。等价于用户手工恢复 recipe，作为打断崩溃循环的最后手段。
2. **`_emergency_full_wipe_and_restart` 升级为两层兜底**：第一层结构化全量清场（摘除全部打印机块+PPD）；若启动后 `journalctl` 仍见 `status=1/FAILURE`（说明文件损坏致正则失效）→ 第二层调 `_nuclear_reset_printers()`（整体移走文件）。
3. **`_stabilize_cups_for_rebuild` 兜底链**：启动+等就绪后，若 `not _wait_cups_ready() or _cupsd_recently_crashed()` → `_emergency_full_wipe_and_restart()`。
4. **新增 `_rebuild_printers_with_guard()`**：`provision`/`repair` 重建队列后**复核 cupsd 稳定性**，仍崩则核弹级重置并二次重建一次，确保「修完盒子可用」而非「修完仍崩」。

### A'（0.2.45 续修）核弹级重置仍被 `_wait_cups_ready` 竞态跳过 → repair 跑完 cupsd 依旧崩
**用户 0.2.45 升级后盒上实测仍崩（2 分钟 157 次 `Scheduler shutting down`、队列数=0）**，证明上面的核弹级兜底**没被触发**。根因是 `_stabilize_cups_for_rebuild` 的兜底判定写在 `systemctl start cups` **之后**：
`if not _wait_cups_ready(timeout=60) or _cupsd_recently_crashed():`。**崩溃循环里 cupsd 每次重启后会有瞬间 "running"**，`_wait_cups_ready` 极易在崩溃前误返回 True；
而 `_cupsd_recently_crashed()` 用 30s 窗口查 journald，也可能漏掉刚发生的崩溃 → 整个 `if` 不触发 → **正则软清场后的损坏 `printers.conf` 原样留下 → cupsd 启动即崩**。
用户手工 `mv printers.conf` 配方（等价核弹级整体移走）屡屡奏效，恰恰反证根因就是「坏 `printers.conf` 没被自动清掉」。

修复（把判定提前到停服前，崩溃循环直接核弹级）：
1. **`_stabilize_cups_for_rebuild` 在 `systemctl mask/stop` 之前先判定 `was_crashing = _cupsd_recently_crashed(120)`**（journald 持久，停服不影响判断）。
2. 若 `was_crashing` 为真 → **直接调 `_nuclear_reset_printers()` 并 early-return**，跳过不可靠的正则软清场（`_wipe_managed_queues_offline` 在损坏文件上匹配不到块会落空）。
3. 单测 `test_stabilize_cups.py` 新增 `test_stabilize_nuclear_when_crashing_before_stop`：用 mock 证明「停服前检测到崩溃循环 → 核弹级重置被触发（`_nuclear_reset_printers` 调用 1 次）、软清场不再被误用」，以及「停服前未崩、启动后才崩 → 仍走软清场+紧急全量清场」双路径正确。

### B. 同一作业被打 N 遍（鸿蒙一直打印不停 / Win10「1页×5份=15张分3批」）→ 服务端 job 重试（非客户端重发）
**根因纠正（重要）**：初判为「客户端重复提交」，但用户实测只点了一次、且客户端早已不再显示打印任务 → 客户端并未在后台重发。
重复出纸只能来自**服务端**：`add_printer` 的「队列已存在且健康」快速通道此前**只做 URI 自愈 + PPD 补丁就 return，从未设置 `printer-error-policy=abort-job`**。
用户是手工 `lpadmin` 建好队列后跑 `printbox republish` → 走健康分支 → 队列沿用 **CUPS 默认 `retry-job`** 策略。foo2xqx 为 host-based，每作业先灌固件、出纸慢，
后端每作业出纸后一旦报错（USB/固件握手异常），`cupsd` 便以**同一 job-ID** 重试整个作业 → 表现为「5 页、停顿、再 5 页、再 5 页」、鸿蒙「一直打印不停」。
**重试复用同一 job-ID**，故此前设想的 `guard_runaway_jobs`（按 job-ID 去重）**拦不住**——它只适用于「客户端真的提交了多个不同 job-ID」的二级场景。

修复（从 ErrorPolicy 根因消除重打）：
1. **新增 `_ensure_error_policy_abort(name)`**：强制 `lpadmin -p <name> -o printer-error-policy=abort-job`；任何后端/传输错误都让作业**立即中止、绝不重打**，从根因关掉「同一作业被反复执行」。
2. **`add_printer` 全路径兜底设 `abort-job`**：新建分支本就带 `-o printer-error-policy=abort-job`；本次补漏——「健康复用」快速通道（republish/repair 命中它）与函数末尾各补一次 `_ensure_error_policy_abort(name)`，
   确保无论新建/重建/复用，托管队列始终是 `abort-job`，不再漏成 CUPS 默认 `retry-job`。
3. **`guard_runaway_jobs` 降级为二级安全网（文档已更正）**：仅拦「客户端确实提交了多个不同 job-ID 的同文档重复作业」；明确标注它**不解决**同 job-ID 的 cupsd 重试，主修复是上面的 ErrorPolicy。
4. **修复定时器生效缺口**：`provision`/`repair` 原先只 `systemctl enable` `EXTRA_UNITS` 不 `start` → `printbox-guard.timer` 要等下次重启才生效。
   改为 `enable` 后对 `.timer` 单元额外 `systemctl start`，升级后定时器立即激活（同步启动 mdns-renew.timer）。

### C. 设备枚举滞后 → 重建队列加重试
`printers.main()` 探测设备前新增重试（最多 5 次、每次 `time.sleep(2)`）：`cupsd` 刚重启后 USB 后端枚举滞后数秒，
单次 `lpinfo -v` 易返回空 → 误判「未发现打印机」→ 不建队列 → 盒子仍空。重试给后端枚举时间。

单测：新增 `tests/test_guard_runaway.py`（运行级 mock 实跑，覆盖重复作业取消 / 不同用户·空名不误杀 / lpstat 失败安全跳过 / 核弹级重置序列 / 无 conf 时幂等）；
全部 6 个测试文件 + `py_compile` + `bash -n` 四关验证通过。

## 0.2.44（2026-10-27）— 根治「离线清场漏删非 _printbox 坏队列 → cupsd 加载即崩、main() 永远建不出队列」

0.2.43 升级盒上实测：版本号已变 0.2.44 之前的现象——`printbox repair` 跑完、`AirPrint` 文件空、`lpstat` 全空、`队列数=0`，但 error_log 每 ~15 秒一次 `Scheduler shutting down due to program error`（2 分钟崩 155 次）。三端（iOS 搜不到 / Win10 测试页报错 / WebUI 无打印机）全部失效，根因同源：**cupsd 在崩溃循环里，`lpstat` 连不上 → 队列数=0 是假零**。

真因在 `repair()` → `_stabilize_cups_for_rebuild()` → `_wipe_managed_queues_offline()`：它**只摘名字带 `_printbox` 后缀的队列**，漏掉了不带 `_printbox` 后缀的坏队列（历史/手建，如 `hp_laserjet_p1505n`）。其 PPD 已被历史崩溃写崩/删除，cupsd 一加载就 `Raw queues are deprecated` + `Scheduler shutting down` 崩溃循环；因名字不带 `_printbox`、旧清场逻辑漏掉它 → 清场不净 → cupsd 持续崩溃 → `printers.main()` 里 `lpinfo` 连不上、永远建不出新队列。

修复：
1. **`_wipe_managed_queues_offline` 扩大清场范围**：除 `_printbox` 托管队列外，凡是 **PPD 缺失/损坏/raw 的坏队列，无论名字是否带 `_printbox`** 一律离线摘除（修掉漏删非 `_printbox` 坏队列）；健康（有真实 PPD、非 raw）的非 `_printbox` 队列仍保留，不误伤用户手动打印机。
2. **新增 `_wipe_all_printers_offline`（紧急全量清场）** + `_stabilize_cups_for_rebuild` 兜底：若正常清场后 cupsd **仍不稳定（最近 30s 内 journald 仍有 `status=1/FAILURE`）**，则 `mask`+停+全量摘除全部打印机块与 PPD+干净重启，确保 `main()` 有干净起点可重建。这是打断崩溃循环的最后手段。
3. **`_ensure_cups_stopped` 收口更稳**：无论是否超时最后都 `pkill -x cupsd` 一次；`_stabilize_cups_for_rebuild` 编辑 `printers.conf` 前先 `systemctl mask cups`，杜绝 systemd 在编辑窗口内把 cupsd 重新拉起、把文件又写崩。
4. 单测：新增 `test_wipe_offline.py`（非 `_printbox` 坏队列被摘 / 健康队列保留 / 紧急全量清场）、`test_stabilize_cups.py`（运行级 mock 实跑：稳定路径与紧急兜底分支均正确）；同步修正 `test_ppd_patch.py` 中把「无 PPD 用户队列」误当健康队列的旧断言（无 PPD 的非 `_printbox` 队列本就该被摘）。

## 0.2.43（2026-10-27）— 根治「repair 重建队列写不出 PPD → cupsd 崩溃循环、三端打不出」

0.2.42 升级盒上 `printbox repair` 跑完三段、AirPrint 服务文件也生成了，但 iOS/鸿蒙/Win **三端仍打不出**，
diag 显示：`crashes in last 2 min: 149`、`!! 队列 ... 的 PPD 不存在`、`存在 RAW 队列`。
根因在 `printers.add_printer` 的驱动候选顺序：
- `pick_driver` 从 `lpinfo -m` 取到带代价前缀的驱动 id `foo2zjs:0/ppd/foo2zjs/HP-LaserJet_P1505n.ppd`；
- 旧代码把它作为**首选**传给 `lpadmin -m`，而本机 CUPS 2.4 下该带 `:N/` 代价的 id 会让 lpadmin
  **返回 rc=0 却写出「引用了 PPD 路径但 /etc/cups/ppd/*.ppd 实际缺失」的半残队列**；
- 因为 rc=0，循环立即 `break`，去前缀的 `foo2zjs:/ppd/...`（手工验证可生成真实 PPD 的形式）**永远没被尝试**；
- cupsd 一加载这个「有 PPD 引用却无 PPD 文件」的队列就 `Scheduler shutting down` 崩溃循环，三端全部失败。

修复：
1. 驱动候选改为**去前缀形式 `scheme:/path` 优先**，带 `:N/` 代价的原 id 仅作兜底；
2. `lpadmin -m` 返回成功后**强制校验 PPD 文件确实生成且非 raw**——若半残则 `lpadmin -x` 删掉，
   继续试下一个候选，绝不留半残队列（这正是 0.2.42 之前三端打不出、cupsd 崩溃循环的根）。
用户手工验证：`lpadmin -p ... -m foo2zjs:/ppd/foo2zjs/HP-LaserJet_P1505n.ppd` 可正常生成真实 PPD 并出纸。

## 0.2.42（2026-10-27）— 修复 0.2.41 引入的 `_run()` 元组解包崩溃

0.2.41 升级盒上 `printbox repair` 在 `_ensure_cups_stopped` 第一行即抛：
`TypeError: cannot unpack non-iterable CompletedProcess object`，导致 repair 当场中断、cupsd 未做任何改动。
根因：本项目的 `init._run()` 返回 `subprocess.CompletedProcess`（用 `.returncode`/`.stdout`/`.stderr` 取值），
但 0.2.41 新增的 `_ensure_cups_stopped` 误把返回值当 `(rc, out, err)` 三元组解包（与 `C.run` 的契约混淆）。
顺带排查出 **pre-0.2.41 已有的同类潜伏 bug**（仅在 avahi 自愈失败 / cupsd.conf 含未知指令时才走到，故历史未触发）：
- `_self_heal_avahi_conf`：`rc, _, _ = _run(systemctl is-active ...)` 与 `_, jl, _ = _run(journalctl ...)`；
- `_repair_cupsd_conf_unknown_directives`：`rc, _, err = _run(cupsd -t ...)`。
全部改为 `.returncode` / `.stdout` / `.stderr` 取值，语义不变。已用 AST 静态扫描确认 `init.py` 内无残留元组解包。
盒上现状：0.2.41 升级在崩溃前未改动任何配置，用户此前 `republish` 建好的健康队列 + AirPrint 文件仍存活，可直接重跑升级。

## 0.2.41（2026-10-27）— 根治「repair 局部清场不净 → 升级后 AirPrint 服务文件为空 → 手机端全部失效」

0.2.40 升级盒上实测：`printbox repair` 跑完、版本号已变 0.2.40、队列也存在，但 **`ls /etc/avahi/services/` 为空**，
于是 iOS「看不到 A4 / 打印失败」、鸿蒙「搜不到打印机」、Win10 仅靠 WSD 勉强能打。根因是：
repair 把「坏队列」摘除后由 `printers.main()` 重建，但重建被崩溃循环挡住、或清场不彻底，
队列仍以坏状态（缺 PPD / raw）存在 → 发布阶段 `_write_airprint_services` 因「队列 raw」**静默跳过** → 服务文件为空。
用户改用**手工 recipe（`systemctl stop cups` → 清空 printers.conf 队列块 + `rm -f /etc/cups/ppd/*.ppd` → `systemctl start cups` →
`lpadmin -m foo2zjs:/ppd/...` → A4）一次成功**，印证「清场重建」才是可靠路径，而旧 repair 只做「局部摘除坏队列」不够。

### 修复
1. 新增 `_wipe_managed_queues_offline()`：**停掉 cupsd 后**把本系统托管的全部 `*_printbox` 队列（连同其 PPD 文件）整块清空，
   制造干净状态；正则用 `(</Printer>|$)` 兜底，连被崩溃截断、缺 `</Printer>` 的损坏 printers.conf 也能清干净。
   非 `_printbox` 队列（用户手动添加的网络/IPP 打印机）绝不碰，保持保守。
2. 新增 `_ensure_cups_stopped()`：轮询直到 cups 真正停止，超时则硬杀 `cupsd` 进程，
   确保离线编辑 printers.conf 时崩溃循环中的 cupsd 不会又把它写崩（0.2.40 仅 `stop` 后立刻改，清场不彻底）。
3. 新增 `_stabilize_cups_for_rebuild()`：封装「停稳 cupsd → 离线清空托管队列 → 离线 PPD 净化 → 放行 paperspecs →
   `start`（非 `restart`）→ 等就绪」，**repair() 与 init() 共用**，等价于用户手工验证可用的可靠 recipe。
4. `repair()` 末尾新增**修复后自检**：遍历队列，若仍是 raw/缺 PPD、或缺少对应 `airprint-<name>.service` 文件，
   明确记 `ERROR`（不再静默放过），交给 diag 复核。
5. `diag_box.sh` 新增 **AIRPRINT SERVICE FILE CHECK**：有效队列却无对应 AirPrint 服务文件直接判 `[FAIL]`，
   把本次回归直接钉进 VERDICT，杜绝「服务文件空了还报健康」。
6. 单测新增 `test_wipe_managed_queues_offline_strips_printbox_and_removes_ppd`（清场托管队列 + 删 PPD + 容忍截断配置）。

### 立即缓解（无需等升级）
队列已是好队列但 AirPrint 文件为空时，盒上 root 直接 `printbox republish` 即可从现有有效队列重新生成 AirPrint 文件，
手机端立即可见（republish 会跳过坏队列、只发有效队列，并补 A4 默认与 foo2xqx 补丁）。

## 0.2.40（2026-10-27）— 根治「repair 重启 cupsd 时坏队列致崩溃循环、PPD 被写崩后再也建不回来」

0.2.39 升级盒上实测：cupsd **仍在崩溃循环（2 分钟 151 次）**，队列 `hp_laserjet_p1505n_printbox` 存在但 **PPD 文件 `/etc/cups/ppd/*.ppd` 已消失**（被历史崩溃写崩），所有端（iOS/Win10/WebUI/鸿蒙）均打不出、iOS 纸张只有美国信纸。

### 根因（diag 直接坐实）
- 历史崩溃把队列的 PPD 写崩/删除后，队列仍在 `printers.conf`，但 PPD 文件没了。
- `add_printer` 的「已存在」分支靠 `patch_ppd_defaults` 补丁，而它 `if not os.path.exists(ppd): return` —— **PPD 不存在就直接静默跳过**，于是 PPD 永远不重建；
  cupsd 每次启动都加载这个没 PPD 的队列 → `Scheduler shutting down` 崩溃循环 → 再加载 → 再崩。
- `repair()` 又**先 `restart cups`、后 `main()` 重建**：cupsd 在坏 PPD 状态下根本起不来，`main()` 跑在崩着的 cupsd 上，永远没机会重建队列。

### 修复
1. 新增 `_ppd_is_raw_or_missing(ppd)`：离线判定 PPD 缺失/损坏/raw（无 *ModelName、无 *OpenUI *PageSize、cupstestppd 失败）。
2. 新增 `_remove_broken_queues_offline()`：**停掉 cupsd 后**直接改 `printers.conf`，摘除 PPD 缺失/损坏/raw 的坏队列，使 cupsd 干净启动不崩。
3. `repair()`/`init()` 在 `restart cups` **之前**先 `systemctl stop cups` + 离线摘除坏队列 + 离线 PPD 净化，再干净重启。
4. `add_printer` 的「已存在」分支：若 PPD 缺失/raw，**`lpadmin -x` 删除后落回新建分支重建真 foo2zjs + A4**，不再静默跳过。
5. 单测新增：`_ppd_is_raw_or_missing`（missing/raw/valid 三态）、`_remove_broken_queues_offline`（离线摘除缺失 PPD 队列、保留健康队列）。

## 0.2.39（2026-10-27）— 根治「A4 永远改不生效 + 队列落成 raw 致打印机报错灯」

0.2.38 升级盒上实测：iOS 已只看到 **1 台**打印机（去重生效），但① 打印时纸张仍是美国信纸、打印对话框里**根本没有 A4 选项**；
② 打印失败、打印机报错灯闪（作业到了打印机但被打回）。两条都是同一类根因：**队列没有落成正经 foo2zjs 驱动**。

### 根因（源码坐实 + 用户 `lpinfo -m` 输出佐证）
1. **`pick_driver` 把带「代价前缀」的驱动 id 原样返回**：`lpinfo -m` 在 Debian trixie 上输出形如
   `foo2zjs:0/ppd/foo2zjs/HP-LaserJet_P1505n.ppd … (recommended)`（其中 `0/` 是代价数字）。该串直接喂给
   `lpadmin -m` 可能解析失败 → `add_printer` **静默回退成 raw 队列**。
2. **raw 队列 = 没有真实 PPD**：自然「无 A4 选项、默认 Letter」，而且 PDF 被原样直传给打印机
   → P1505n 无法识别 PDF → **报错灯闪、不出纸**（用户描述的「有反应、闪错误灯」）。
3. 即便装上了驱动 PPD，`patch_ppd_defaults` 旧逻辑在「PPD 选项列表无 A4」时会 **`return` 静默放弃**（只记 INFO），
   导致默认永远停在 Letter——这就是「让你改 A4 改了 N 次都不生效」的代码级根因。

### 修复
- 新增 `_strip_drv_cost()`：去掉驱动 id 的 `:N/` 代价前缀（`foo2zjs:0/ppd/...` → `foo2zjs:/ppd/...`）。
- `add_printer` 添加队列时：先试原 id，失败再试去前缀变体；**真实打印机（usb/hp:/ipp/...）绝不再静默兜底成 raw**，
  失败时直接报 ERROR 并给出手动重建命令（raw 队列比没建队列更难排查）；若仍落成 raw 也会明确报错。
- `patch_ppd_defaults` 去掉「无 A4 就放弃」的早退：`_inject_ppd_a4` 现在**强制保证** PPD 选项列表含 A4
  （有 OpenUI 块插在 DefaultPageSize 后；无 OpenUI 块也补最小块），随后设 `*DefaultPageSize/PageRegion: A4`，
  cupstestppd 护栏保留（写坏 PPD 会自动放弃保留原文件）。
- 单测新增：`test_patch_ppd_defaults_injects_a4_when_missing`（只有 Letter 的 PPD 注入 A4 + 默认 A4）、
  `test_patch_ppd_defaults_skips_raw`、`test_strip_drv_cost`；原有 5 项测试全过。

## 0.2.38（2026-10-27）— 修复 repair 在「坏 PPD 仍在盘」时重启 cupsd 引发崩溃循环、队列被写崩丢失

0.2.37 升级盒上实测：`printbox 0.2.37` 安装成功，但 **cupsd 进入启动崩溃循环**（每 ~15s 一次
`Scheduler shutting down due to program error`），且 **打印机队列彻底消失**（printers.conf / PPD 目录均为空，
`lpstat` 报 `No destinations added`）；iOS 看到的是崩溃前残留在 mDNS 里的打印机残影，点击打印必失败。

### 根因：repair 的「先重启 cupsd、后修 PPD」顺序制造了死循环，把队列写崩
- 升级前，盒上既有 PPD（历史版本遗留）的 cupsFilter 仍指向 **已删除的 `/usr/local/bin/printbox-foo2xqx-pdf`**。
- `repair()` 的时序是「先 `systemctl restart cups`（init.py:939）→ 后跑 `printers.main()` 补丁 PPD（init.py:960）」。
  坏 PPD 让 cupsd 一加载就 `File "/usr/local/bin/printbox-foo2xqx-pdf" not available` 并崩溃循环；
  崩溃中 cupsd 把 `/etc/cups/printers.conf`、PPD 写崩（截断为空），于是 **升级后队列彻底消失**。
- 随后 `printers.main()` 跑在崩着的 cupsd 上：`lpinfo -v` 拿不到设备 → 既不修也不重建队列，死局固化。
- 0.2.37 的修复代码本身正确（删 MIME 白名单、已存在队列补丁），但**从未被真正执行**——
  因为 repair 在修 PPD 之前就先把 cupsd 崩死了，正是历史反复回潮的同一根因被顺序问题重新触发。

### 修复（0.2.38）
1. **新增 `_sanitize_ppd_filter_paths(ppd_dir="/etc/cups/ppd")`（printers.py）**：在「重启 cupsd 之前」
   **纯文件级**把磁盘上所有残留的 `/usr/local/bin/printbox-foo2xqx-pdf` 改写成 AppArmor 放行的
   `/usr/lib/cups/filter/printbox-foo2xqx-pdf`（带 cupstestppd 护栏，改写不合法则放弃）。不依赖 cupsd，
   从根上打断「坏 PPD → cupsd 启动崩 → 队列写崩」死循环。
2. **`repair()` / `init()` 在 `systemctl restart cups` 之前先调用该净化 + `_allow_cups_paperspecs()`**：
   保证 cupsd 启动时盘上已无坏 PPD；随后 `printers.main()` 才能在健康的 cupsd 上正常重建/补丁队列。
3. **`_allow_cups_paperspecs()`（init.py，best-effort）**：Debian trixie 的 AppArmor cupsd 配置档默认未授权
   读取 `/etc/paperspecs`，cupsd 每次启动刷一条 `apparmor="DENIED" ... name="/etc/paperspecs"`（每 ~15s 一条噪声，
   且可能让全局默认纸张回退 Letter）。通过 local include `/etc/apparmor.d/local/usr.sbin.cupsd` 放行 `r` 并热重载，
   纯 best-effort、任何一步失败都不影响主流程。
4. **diag 崩溃计数不再漏报**：旧 diag 用 `journalctl ... | grep 'Scheduler shutting down'` 计数，但该信息只进
   `error_log`、不进 journald，导致计数恒为 0、误报「0 崩溃」。改为取 journald 里的真实崩溃信号
   `status=1/FAILURE` 与 error_log 的 `Scheduler shutting down` 二者较大值。

### 验证
- `py_compile` printers.py / init.py 通过；`bash -n` diag_box.sh 通过；
- 新增 `test_sanitize_rewrites_dead_wrapper_path_offline`：用临时 PPD 目录验证坏路径被离线改写、好 PPD 不动；
- `test_ppd_patch.py` + `test_printers_parse.py` 全过；tarball 内版本号 = 0.2.38。

## 0.2.37（2026-10-27）— 根治 0.2.36「Live PPD 仍指向已删 wrapper → 0 字节不出纸 + diag 误报健康」

0.2.36 升级盒上实测：iOS 仍双显（其中一台能打）、Win10 测试页能打、但 **iOS/鸿蒙/Win 默认纸张非 A4**、
**鸿蒙打印无反应、安装后自动连打两页测试页**。经源码坐实，根因是 0.2.36 的 `patch_foo2xqx_ppd` **仍有 MIME 白名单漏网**，
且排查手段不足：

### 根因：0.2.36 `patch_foo2xqx_ppd` 的 MIME 白名单把部分 PPD 过滤器行漏掉，Live PPD 永不修正
- **现象**：error_log 满屏 `File "/usr/local/bin/printbox-foo2xqx-pdf" not available` 与
  `Scheduler shutting down due to program error` 崩溃循环；但 0.2.36 的 diag 仍打印「0.2.35 健康 ✅」。
- **根因**：0.2.36 的 `patch_foo2xqx_ppd` 只认 `application/pdf / application/vnd.cups-pdf /
  application/vnd.cups-postscript` 三种 MIME 的 cupsFilter 行。本机 foo2zjs PPD 的过滤器行实际用了
  **`application/vnd.cups-raster` / `application/postscript` / `image/*` 等其它 MIME**，这些行**永远不被改写**，
  PPD 仍指向 0.2.34 已删除的 `/usr/local/bin/printbox-foo2xqx-pdf`（AppArmor 拒执行）→ 作业 0 字节、cupsd 崩溃循环。
  同时 0.2.36 的 `add_printer` 对「已存在非 raw 队列」直接 return 跳过补丁，导致既有队列的 Live PPD 长期不修正。
- **修复（0.2.37）**：
  1. `patch_foo2xqx_ppd` 删除 3 项 MIME 白名单，改为「行内出现 `foomatic-rip` 或旧绝对路径
     `/usr/local/bin/printbox-foo2xqx-pdf` 即重定向到 AppArmor 放行的 `/usr/lib/cups/filter/printbox-foo2xqx-pdf`」，
     与 MIME 无关，覆盖所有 foo2zjs/foo2xqx PPD 变体；
  2. `add_printer` 的「已存在非 raw」分支不再直接 return，补打两份**幂等**补丁
     （`patch_ppd_defaults` 注入 A4 + `patch_foo2xqx_ppd` 重定向过滤器），确保升级/repair 后 Live PPD 始终正确；
  3. `_write_airprint_services` 的 republish 路径新增 `patch_foo2xqx_ppd(name)`（原有 A4 补丁保留），
     修 0.2.36 重发布时只打 A4、漏打过滤器路径的回归；
  4. `_current_uri` 同时解析 `device for NAME: uri`（真实冒号格式）与早期 `is` 写法，避免拿到空 URI
     （空 URI 会让 `_dedupe` 归一时拿到空 key，间接助长双显）。

### 排查手段硬化（diag_box.sh 不再误报健康）
- 崩溃计数改用 `date -d '2 minutes ago' / '5 minutes ago'` 绝对时间喂给 `journalctl --since`，
  替换 0.2.36 非法的 `--since '-2m'`（静默返回空 → 计数恒为 0 → 漏报崩溃）。
- 新增 **LIVE PPD FILTER PATH** 段：遍历队列 PPD，检查是否仍含 `/usr/local/bin/printbox-foo2xqx-pdf`
  （命中置 `PPD_FILTER_BAD=1`），并校验 `cupsFilter` 引用的 `/usr/lib/cups/filter/*` 是否 `-x`；
- 新增 **DEFAULT PAPER SIZE (A4 校验)** 段：用 `lpoptions -p $q -l` 取 `PageSize` 当前值，非 A4 置 `PAPER_BAD=1`；
- 新增 **DEVICE URI (lpstat -v)** banner，直视真实后端是否 `usb://`；
- VERDICT 重写：显示真实 `printbox --version`，并把 `PPD_FILTER_BAD`/`PAPER_BAD` 计入 issues，标题改为「PrintBox 健康核验」。

### 关联修复（0.2.36 已修、0.2.37 继承并加固）
- 安装后自动连打两页测试页：由 `add_printer` 末尾的 `write_airprint_service` + `patch_ppd_defaults` +
  `patch_foo2xqx_ppd` 各触发一次完整 PPD 重写，若升级重跑 `printbox repair` 会触发两次补丁叠加。
  0.2.37 三处补丁均幂等，且仅在「内容确有变化」时 `lpadmin -P` 重载（change 检测），避免无谓重载触发测试页。
- 鸿蒙无反应 / Win10 不能选页：随「已存在队列强制补丁 + 默认 A4 + 单队列单广播」在 repair/升级时落地后应消除，
  待盒上实测确认（见下「升级后请核对」）。

### 升级后请核对
- `lpstat -p -l` 仅 1 个 `hp_laserjet_p1505n_printbox`，状态 idle/enabled；
- `printbox --version` 显示 `0.2.37`；
- diag 的 LIVE PPD FILTER PATH 段：`/usr/local/bin/printbox-foo2xqx-pdf` **不应出现**，
  `/usr/lib/cups/filter/printbox-foo2xqx-pdf` 存在且可执行；
- DEFAULT PAPER SIZE 段：当前 `PageSize` 应为 `A4`；
- error_log 不含 `not available`、`Scheduler shutting down`、`Bad line from driverless`；
- iOS/鸿蒙/Win 均只看到 1 台打印机，默认纸张为 A4。

## 0.2.36（2026-10-26）— 补全 0.2.35 未真正生效的两处修复 + driverless stub 硬化

0.2.35 升级后盒上 diag 实测：**A 已根治（cupsd 无非法指令、不崩），但 B、C 仍没生效，双显与 0 字节打不出照旧**。
根因是 0.2.35 的代码改动有「只加了调用、但内部逻辑有 bug，实际不执行」的漏洞，本次逐一坐实并补全：

### B 修复未生效（iOS 仍双显）— `_dedupe_printers` 正则与真实 `lpstat -v` 输出格式不匹配，整段去重从未执行
- **根因**：`lpstat -v` 真实输出为 `device for NAME: uri`（**冒号**分隔，没有 `is` 这个词），
  但 0.2.35 的代码正则是 `device for\s+(\S+)\s+is\s+(\S+)`（要求 `device for NAME is URI`）。
  二者格式不符，`re.search` **永远返回 None**，去重循环体从不执行，`by_uri` 始终为空直接 return——
  重复队列 `laserjet_p1505n_printbox` 从来没被尝试删除过。（此前曾误判为“队列名带尾随冒号导致 `lpadmin -x` 失败”，
  属误诊；真正问题是正则根本没匹配上任何行。已用单测验证旧正则对真实输出匹配 0 行。）
- **修复**：正则改为 `device for\s+(\S+?):\s+(\S+)`（非贪婪 `(\S+?)` 在冒号前停下，正确取出不带冒号的队列名）。
  去重命中后 `lpadmin -x` 删除重复队列，再由 `prune_phantom_printers()` 末尾的
  `_clean_stale_airprint_services()` 删掉孤儿 AirPrint 服务文件，iOS 不再看到两台。

### C 修复未生效（WebUI/手机打不出）— `patch_foo2xqx_ppd` 门控把旧 PPD 挡在门外
- **根因**：既有队列的 PPD（0.2.34 写入）里过滤器行已是旧绝对路径 `/usr/local/bin/printbox-foo2xqx-pdf`，
  **不再含 `foomatic-rip`**。0.2.35 的 `patch_foo2xqx_ppd` 门控 `if "foomatic-rip" not in head: return`
  直接提前返回，且替换逻辑只认 `foomatic-rip`、不处理旧路径 → PPD 永远没被改回 → 作业 `Unable to start filter
  /usr/local/bin/printbox-foo2xqx-pdf - No such file` → 0 字节不出纸。`repair()` 虽对每台已存在打印机调用了
  `patch_foo2xqx_ppd`，但被这道门控拦死，所以 0.2.35 升级后 PPD 还是旧的。
- **修复**：
  1. 门控改为「旧绝对路径 `FOO2XQX_WRAPPER_OLD` 或 `foomatic-rip` 任一存在即需重打补丁」；
  2. 替换逻辑同时把 `foomatic-rip` 与旧绝对路径重定向到新路径 `/usr/lib/cups/filter/printbox-foo2xqx-pdf`
     （AppArmor 放行目录）。升级重跑 `printbox repair` 即把既有 PPD 改对。

### driverless/dnssd 后端 stub 硬化（0.2.35 引入的回归）
- **根因**：0.2.35 重写 `_disable_driverless_backend` 用 `glob(base+"*")`，当 0.2.34 已把后端改名 `.disabled` 后，
  该 glob 只匹配到 `.disabled`（被 `continue` 跳过），**stub 从未写入**。实测当前 `Bad line from driverless = 0`
  （不崩），但原路径无 stub，diag 误报「建议重跑 repair」。
- **修复**：`_disable_driverless_backend` 重写为「无论是否已改名，都在原路径写入 `exit 0` stub；真实后端改名
  `.disabled` 并去执行位（双保险）」；新增 `_is_printbox_stub()` 避免把自家 stub 当真实后端误改名。幂等可跨版本重复执行。

### 防御性加固
- `_install_units()` 每次安装/修复时幂等重建 `/usr/local/bin/printbox -> <venv>/bin/printbox` 软链，
  避免 hotplug/web/archive 等服务单元因该软链缺失或指向旧版本而跑旧代码（历史上因此重建短名幽灵队列）。
- `diag_box.sh` 后端检查改为核验 stub 内容（含 `PrintBox` 标记），不再仅凭文件存在误判。

## 0.2.35（2026-10-26）— 彻底根治「WebUI 打不出 + iOS 双显 + 重启回潮」

本版一次性坐实并修复了 0.2.33 / 0.2.34 反复未根治的三处**独立**根因（三者并存，任一未修都会让现象复现）：

### A. cupsd.conf 含非法指令 → cupsd 崩溃循环（WebUI 打不出、测试页 0 字节）
- **根因**：0.2.33 曾误写 `BrowseRemoteProtocols none`。该指令在本机 CUPS 2.4 非法，`cupsd -t` 解析失败、
  回退默认配置（默认 `Browsing On`），cupsd 每 ~15s `Scheduler shutting down` 崩溃，所有作业 Filter failed。
- **为什么 0.2.34 没修好**：`_ensure_cups_lan()` 只改写 `Browsing` / `BrowseLocalProtocols`，**从不删除旧行**，
  故 0.2.34 升级后日志仍报 `Unknown directive BrowseRemoteProtocols on line 173`，一直崩。
- **修复**：
  1. `_ensure_cups_lan()` 新增步骤 5：`re.sub` 删除任何遗留 `BrowseRemoteProtocols` 行（含历史误写兜底）；
  2. 写盘后调用新增的 `_repair_cupsd_conf_unknown_directives()`：循环 `cupsd -t -c`，若报
     `Unknown directive X on line N` 自动删该行重试（最多 10 次），作为防御性自愈，杜绝任何版本误写非法指令再崩。

### B. 重启后重复队列 → iOS 看到两台同名打印机（双显根因之一）
- **根因**：`printers.main()` 探测流程**不调用 `prune_phantom_printers()`**。重启后 CUPS 重新枚举设备，
  同物理打印机经 `hp:/usb/...` 与 `usb://...` 多后端暴露，被建成两个队列
  （`hp_laserjet_p1505n_printbox` 与 `laserjet_p1505n_printbox`），两队列都被 Avahi 发布 → iOS 双显。
- **修复**：`printers.main()` 末尾（写 AirPrint 服务文件之前）加 `prune_phantom_printers()`。
  其内 `_dedupe_printers()` 按 `_uri_key`（含序列号）把同设备不同名队列归并，只保留规范名、删短名/双后缀/自引用队列，
  确保「一台物理打印机 = 一个 CUPS 队列 = iOS 只看到一台」。无论重启还是热插拔探测，都即时收敛。
- **配套**：`repair()` 现已在发布前重跑 `printers.main()`，升级时若既有队列落成 raw/损坏会被 `force` 重建。

### C. 自定义 foo2xqx wrapper 装在 /usr/local/bin → 作业 0 字节、不出纸
- **根因**：0.2.34 及之前把 `printbox-foo2xqx-pdf` 装在 `/usr/local/bin`。Debian trixie 默认启用 AppArmor，
  cupsd 的 AppArmor profile 只放行执行 `/usr/lib/cups/filter/` 与 `/usr/lib/cups/backend/` 下文件，
  拒执行 `/usr/local/bin` → `execv failed: Permission denied`（status 113）→ 作业 0 字节、不出纸。
- **修复**：
  1. `FOO2XQX_WRAPPER` 改为 `/usr/lib/cups/filter/printbox-foo2xqx-pdf`（PPD 的 cupsFilter 行仍引用它，
     二进制落在 AppArmor 放行目录，不再被拒）；
  2. `_install_foo2xqx_wrapper()` 装新 wrapper 时顺手删除旧 `/usr/local/bin/printbox-foo2xqx-pdf` 残留，
     避免混淆与残留执行失败。

### 验证要点（升级后请跑 diag_box.sh 核对）
- `lpstat -p -l` 应只有 **1 个** `hp_laserjet_p1505n_printbox` 队列，状态 `idle / enabled`；
- `/var/log/cups/error_log` 不含 `Unknown directive`、`Scheduler shutting down`、`Bad line from driverless`；
- `cupsd -t` 退出码 0；
- `ls /usr/local/bin/printbox-foo2xqx-pdf` 应**不存在**；`/usr/lib/cups/filter/printbox-foo2xqx-pdf` 存在且可执行；
- WebUI 提交测试页 → 出纸；iOS/鸿蒙「打印」里只看到 **1 台** `hp_laserjet_p1505n_printbox`。

---

## 0.2.34（回炉）— 尝试修 Browsing，但因 A/C 未根治而失败
- 删除了 0.2.33 的 `BrowseRemoteProtocols none` 误写，并把 `Browsing` 设为 `Off`；
- **未彻底修 A**：`_ensure_cups_lan` 不删旧行，非法指令残留导致 cupsd 仍崩；
- **未修 B**：`main()` 仍不 prune，重启后双队列回潮；
- **未修 C**：wrapper 仍在 `/usr/local/bin`，AppArmor 仍拒执行 → 0 字节不出纸。
- 结论：0.2.34 现象与 0.2.33 一致，已废止。

## 0.2.33（回炉）— 误引入非法指令
- 为关闭 CUPS 自身广播，误追加 `BrowseRemoteProtocols none`（本机 CUPS 2.4 非法指令），
  触发根因 A。已废止。

## 0.2.32 及之前
- 初始 `Browsing On` + driverless/dnssd 后端启用，导致 cupsd 崩溃循环与 iOS 双显，后经 0.2.33/0.2.34 多次回炉，
  最终在 0.2.35 一次性三处根因全修。
