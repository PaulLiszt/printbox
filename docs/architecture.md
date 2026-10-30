# 架构设计：15 条需求 → 实现映射

> 设备：n2810（Intel Celeron N2810, Bay Trail, amd64）+ 4G RAM + 64G 存储
> 外设：rtl8188eus USB 无线网卡、HP LaserJet P1505（USB）
> 接口：仅 USB / 网线(RJ45) / VGA —— 无键盘鼠标显示器，纯无头

---

## 需求 1：无头主机控制打印机
- 不安装任何桌面环境（`task-desktop` 不装）。
- 打印机通过 USB 接入，CUPS 管理；系统全程无显示输出（VGA 仅 BIOS 阶段，GRUB 加 `quiet`）。
- 所有控制经由网络（Web + SSH）。

## 需求 2：主流无线打印协议，全终端可用
| 终端 | 协议 | 实现 |
|---|---|---|
| Windows | IPP + WS-Discovery + LPD | CUPS(`ipp`) + `wsdd` + `cups-lpd` |
| macOS/iOS | AirPrint | Avahi 发布 `_ipp._tcp` + AirPrint TXT 记录 |
| Linux | IPP Everywhere / CUPS Browsing | `cups-browsed` + Avahi |
| Android | Mopria (IPP Everywhere) | 同 IPP Everywhere |
| HarmonyOS | IPP / Mopria | 同 IPP Everywhere |
- 详见 `docs/protocols.md`。

## 需求 3：无线网卡连家庭 WiFi，供设备发现
- `wpa_supplicant` 管理 `wlan0`，SSID/密码来自 `PROVISION.json`。
- 设备发现靠 mDNS（Avahi）在 WiFi 子网内广播，终端自动发现打印机。

## 需求 4：电源键一键开机 / 关机
- **开机**：BIOS 设置 *Power → Restore on AC Power Loss = Power On*（插电自启）；面板电源键短按上电。
- **关机**：`/etc/systemd/logind.conf.d/99-powerkey.conf` 设 `HandlePowerKey=poweroff`，短按电源键触发优雅关机。
- 实现细节：`logind` 捕获 ACPI 电源键事件 → `systemctl poweroff`。

## 需求 5：SSH 便于操作
- `openssh-server` 开机自启，仅密钥+密码认证（可经 PROVISION 注入公钥）。
- 默认普通用户 + sudo，root 可锁。

## 需求 6：U 盘根目录特定文件一次性提供配置
- 文件名固定：`PROVISION.json`（也兼容 `PROVISION.txt` 行式、`PROVISION.yaml`）。
- 仅当 `/data/.provisioned` 不存在时应用；应用后写标记，后续不再自动改（避免每次覆盖）。
- 字段：admin 用户名/密码、WiFi SSID/密码、国家码、主机名、网络模式(DHCP/静态)、IP/网关/DNS。
- 之后变更：SSH（`nano /data/config.json` + 调脚本）或 Web 设置页。

## 需求 7：开机自动运行全套功能
- systemd 服务链：`printserver-firstboot`（仅首启）→ `printserver-provision`（一次性配置）→ `systemd-networkd`/`wpa_supplicant`/`avahi-daemon`/`cups`/`cups-browsed`/`wsdd`/`printserver-archive`/`printserver-web`/`printserver-watchdog`，全部 `enabled`，`multi-user.target` 下并行拉起，无需任何人工干预。

## 需求 8：局域网发布特定网址（非 IP）
- 主机名 `print`（可改），Avahi 发布 `print.local`；终端浏览器打开 `http://print.local`。
- 同时设置 DHCP hostname（`print`），多数路由器会把 `print.lan`/`print.home` 解析出来作为备用。
- Web UI 首页也显示当前 IP 作为兜底。

## 需求 9：兼容大部分有线/无线网卡
- 内核 `linux-image-amd64`（含 backports 新版驱动）。
- 全量固件：`firmware-linux-free` `firmware-linux-nonfree` `firmware-realtek` `firmware-iwlwifi` `firmware-brcm80211` `firmware-atheros` `firmware-ralink` `firmware-zd1211` `firmware-mediatek`。
- 网络用 `systemd-networkd`，`wlan0`(USB 无线) 与 `eth0`(RJ45) 同时可用，WiFi 优先发现。

## 需求 10：兼容大部分打印机
- 驱动集合：`hplip`(+`printer-driver-hpcups` 覆盖 HP P1505)、`printer-driver-gutenprint`、`foomatic-db*`、`printer-driver-foo2zjs`(HP/Minolta 等)、`printer-driver-splix`(Samsung)、`printer-driver-brlaser`(Brother)、`printer-driver-postscript`、`ghostscript`、`poppler-utils`、`ijsgutenprint`。
- 优先用 **IPP-over-USB**（`ipp-usb`）驱动现代网络/USB 打印机；HP 走 HPLIP。
- `printer_setup.py` 自动探测 USB 打印机并选定最佳驱动（含 P1505 的 `hpcups`/`hpijs`）。

## 需求 11：稳定性 —— 断电不丢参、防非法篡改
- **断电安全**：根文件系统 ext4 `data=ordered,barrier=1,commit=60,errors=remount-ro`；配置 `/data` 独立分区，写配置用"写临时→rename"原子提交。
- **防篡改**：
  - 关键文件 `chattr +i`：`/opt/printserver/bin/*`、`webapp.py`、provision 脚本、Grub 配置。
  - `fs.protected_hardlinks=1`、`protected_symlinks=1`、`protected_regular=2`。
  - `printbox-integrity.service` 开机对关键文件做 sha256 校验，异常写入 `/data/logs/integrity.log` 并告警。
  - 可选：把 `/usr` 以只读 overlay 挂载（`config.sh` 开关 `READONLY_USR=1`）。
- **看门狗**：`printserver-watchdog.service`（timer 每 60s）检测 cups/web 存活，异常自动重启对应服务。

## 需求 12：U 盘（或任意介质）刷入固件 —— 单一镜像、整体覆盖
- 本固件交付物是**一个可直接刷写的磁盘镜像文件** `printbox.img`（就像给树莓派 / MMDVM 刷的 `.img`），
  它**本身就是一个完整的操作系统**（内核 + initramfs + Debian 用户态 + 打印服务栈），
  而不是“装在某个系统里的应用”。
- 构建：`build/make-image.sh` 一次产出 `out/printbox.img`（原始可刷写镜像）+ `out/printbox.img.xz`（压缩传输）+ SHA256。
- 刷写（二选一，都符合“一个文件覆盖整盘”的模型）：
  - **A. 取盘直刷（推荐，最贴近 Pi 体验）**：把小主机内部盘（mSATA/SATA/eMMC 等）取下或用 USB 转接卡接到任意电脑，
    用 balenaEtcher / Raspberry Pi Imager / `dd` 把 `printbox.img` 直接写入该盘，装回即可。
  - **B. 无头刷写**：用 `build/flash-internal.sh` 配合一个临时 Debian Live U 盘，把镜像写入内部盘（脚本自动挑选内部盘并要求确认）。
- 刷完后**整盘被覆盖**，原系统不再存在；首次开机由 `printserver-firstboot.service` 把分区扩展到整盘并应用一次性配置。

## 需求 13：包含完整操作系统，不依赖其它 OS
- `printbox.img` 内含自带的内核、驱动、systemd 用户态与全部打印/网络/SSH 栈，
  刷入后独立启动运行，**不依赖宿主机上的任何操作系统**（不是 Windows/Linux 上的一个程序）。
- 注：底层基于 Debian（与 Pi 上的 Raspberry Pi OS / MMDVM 镜像同理，都是“定制过的 Debian 系镜像”），
  而非从零手写内核——这是嵌入式/ appliance 固件的标准做法，能保证驱动与稳定性。
  若你坚持要从零的 LFS 路线，可告知，但会显著增加体积/风险且不带来额外收益。

## 需求 16（追加）：打印文件归档（自动配额 + 轮替）
- 用户希望利用空闲硬盘空间把**每次打印的原始文件**保存下来备查，装满后自动轮替（删最旧）。
- 实现：
  - `print_archive.py` 守护进程：开启 CUPS `PreserveJobFiles`，周期性扫描已完成作业，
    把 spool 中的作业数据文件（即打印时发来的原始文档）复制到 `/data/print-archive`，
    并写 `.meta.json` 记录作业名/用户/格式/时间。
  - **配额自动计算**：`config.compute_archive_quota(total_bytes, ratio, min, cap)`，
    按磁盘总容量 × 比例，并夹在 [min, cap] 之间（默认 ratio=0.25, min=2GB, cap=30GB）。
    例如 64GB 盘约可用 ~14GB 存打印文件，128GB 盘约 ~30GB（封顶）。
  - **轮替**：归档总大小超过配额时，按文件修改时间从旧到新删除，直到回到配额内。
  - **首启计算**：`firstboot_archive.py` 在首次开机按当时磁盘容量写入实际配额；Web/PROVISION 也可改比例与上下限。
  - Web “归档”页可查看用量、开关、调整比例/上下限、清空归档。

## 需求 14：运行/错误记录便于调阅
- `journald` 持久化（`Storage=persistent`）。
- 应用日志统一落 `/data/logs/{web,provision,printer,network,integrity}.log`，带时间戳与级别。
- Web "日志"页聚合展示；命令行 `printbox-logs` 快捷查看。

## 需求 15：尽可能快速启动
- 根文件系统 `minbase`（极小包集），无需桌面/显示管理。
- systemd 并行启动；关闭蓝牙/ModemManager/avahi-autoipd 等无用服务。
- 启动参数 `quiet loglevel=3`；目标：上电到 AirPrint 可见 < 25s（机械/SSD 差异）。

---

## 组件图

```
            ┌─────────────────────────────────────────────┐
            │             PrintServerOS (Debian)            │
            │                                               │
  USB ──────┤  PROVISION.json (一次性) → provision.service  │
  WiFi ─────┤  wpa_supplicant + networkd (wlan0)           │──┐ 发现
  LAN  ─────┤  networkd (eth0)                             │  │ mDNS
            │      │                                        │  │ WS-D
            │  ┌───┴──────────────────────────────────┐    │  │
            │  │ Avahi (print.local)  cups  cups-browsed│   │  │
            │  │ wsdd  ipp-usb  printer_setup           │   │  │
            │  └───────────────────────────────────────┘   │  │
            │      │                                        │  │
            │  printserver-web  (http://print.local:80)     │◄─┘
            │  sshd  (port 22)                              │
            │                                               │
            │  USB Printer (HP P1505) ──┐                   │
            └───────────────────────────┼───────────────────┘
                                        ▼
                                  CUPS spool / driver
```
