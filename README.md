# PrintBox

> 把一台已经装好 Linux（Debian / Ubuntu 等 systemd 发行版）的主机，变成无头打印服务器。
> Turn any Linux box (Debian/Ubuntu or another systemd distro) into a headless print server.

> **产品形态 / Product form**
> PrintBox 是一个 **pip 分发的 Python 应用**，不包含操作系统镜像。网络参数、主机名、系统账号等由底层 Linux 自行管理（与系统一致）；PrintBox 只负责把打印服务栈跑起来并提供管理界面。
> PrintBox is a **pip-distributed Python application**, not a full OS image. Network settings, hostname, and system accounts are managed by the underlying Linux (consistent with the system); PrintBox only brings up the print-service stack and provides a management UI.

## 核心能力 / Core capability

> **让老式 USB 打印机焕发新生 / Give your old USB printer a new lease on life**
> 把一台只支持 USB 直连的普通打印机，接上任意一台 Linux 小主机（树莓派 / 旧电脑 / 迷你盒），PrintBox 会把它变成一台**无线打印服务器**：局域网内 **iOS、Android、鸿蒙(HarmonyOS)、macOS、Windows、Linux** 等任意终端，都能像使用原厂网络打印机一样**无线调用**它——无需打印机自带网卡或 Wi‑Fi 模块。
> Connect any ordinary USB-only printer to a small Linux box (Raspberry Pi / old PC / mini appliance); PrintBox turns it into a **wireless print server**. Any device on your LAN — **iOS, Android, HarmonyOS, macOS, Windows, Linux** — can **print to it over the network** just like a native network printer, with no Wi‑Fi/Bluetooth module required on the printer itself.

## 功能特性 / Features

- **旧打印机无线化 / Retrofit any USB printer**：把仅支持 USB 直连的老打印机接入局域网，变成可被手机/电脑无线调用的打印服务器，无需打印机自带网卡。
  Turn a USB-only legacy printer into a LAN-accessible wireless print server — no printer-side network card required.
- **一键启动 / One-command bootstrap**：`sudo printbox init` 后所有服务开机自启；`printbox start/stop/restart/status` 随时管理。
  `sudo printbox init` brings up all services on boot; `printbox start/stop/restart/status` manage them anytime.
- **全协议打印 / Universal printing**：CUPS + IPP Everywhere + AirPrint(iOS/macOS) + Mopria(Android/HarmonyOS) + WS-Discovery(Windows) + LPD。
- **Web 管理界面 / Web UI**：状态、打印机、打印设置、系统、归档、打印历史、设置、日志，局域网内用 `http://<主机名>.local` 或 `http://printbox.local` 直接访问。
  Status, Printers, Print options, System, Archive, History, Settings, Logs — reachable on the LAN via `http://<hostname>.local` or `http://printbox.local`.
- **自动安装驱动 / Auto driver install**：探测到打印机后按厂商自动安装对应驱动包（HP / Brother / Samsung / Canon / Epson …）。
- **打印文件归档 / Job archiving**：每次打印的原始文件自动保存，按磁盘容量自动计算配额，装满轮替删除最旧文件。
- **打印历史 / Print history**：可预览(PDF)、可一键再次打印。
- **配置持久化 / Persistent config**：配置落盘 `/etc/printbox`，日志落盘 `/var/log/printbox`，断电不丢参。

## 安装 / Installation

> **安装位置说明 / About the install location**
> 新版 Debian / Ubuntu（含 trixie）、Fedora、Arch 等发行版，将系统 Python 标记为「外部受管」（PEP 668），**禁止用系统 `pip` 直接安装包**（会报 `externally-managed-environment`）。
> 因此 PrintBox 设计为安装在**独立的虚拟环境（venv）**中：安装脚本会自动创建 `/opt/printbox/venv` 并装进去，命令经该 venv 的 `printbox` 入口执行。
> Modern Debian/Ubuntu (incl. trixie), Fedora, Arch, etc. mark the system Python *externally managed* (PEP 668) and **block installing with the system `pip`** (error: `externally-managed-environment`).
> PrintBox is therefore designed to install into its **own virtual environment (venv)**: the installer creates `/opt/printbox/venv` and installs there; the `printbox` command runs from that venv.

**① 推荐：一行命令全自动（自动建 venv + 安装 + 初始化）/ Recommended — one line, all-in-one (auto venv + install + init):**

```bash
curl -sSL https://raw.githubusercontent.com/PaulLiszt/printbox/main/install.sh | sudo bash
```

**② 或手动安装到 venv / Or install manually into a venv:**

```bash
sudo python3 -m venv /opt/printbox/venv
sudo /opt/printbox/venv/bin/pip install printbox
sudo ln -sf /opt/printbox/venv/bin/printbox /usr/local/bin/printbox
```

初始化（需 root，自动装系统依赖、写服务、探测打印机）/ Initialize (needs root; auto-installs system deps, writes services, probes printers):

```bash
sudo printbox init
```

> PyPI 上的包名就是 `printbox`（`pip install printbox`）；但在 PEP 668 系统上请务必在 venv 内执行（见上），**系统级 `pip install` 会失败**。
> The PyPI package name is `printbox` (`pip install printbox`), but on PEP 668 systems run it **inside a venv** as shown above — a system-wide `pip install` will fail.
> 安装脚本可重复运行（重跑自动重建 venv 与服务）；Web 管理界面始终会启动（不依赖 CUPS 是否就绪），打印功能待 CUPS 就绪后自动可用。
> The installer is idempotent (re-running rebuilds the venv and services); the Web UI always comes up (independent of CUPS), and printing becomes available automatically once CUPS is ready.

## 使用 / Usage

```bash
sudo printbox status     # 查看服务状态 / show service status
sudo printbox detect     # 重新探测/添加打印机（自动装驱动）/ re-probe & add printers
sudo printbox restart    # 重启全部服务 / restart all services
printbox version         # 显示版本 / show version
```

浏览器打开以下任一地址即可进入 Web 界面，管理打印机、查看打印历史与归档：
Open any of the following addresses in a browser to manage printers, view print history and archives:

- **本机 IP / LAN IP**（最稳，如 `http://192.168.1.100`）
- **`http://<主机名>.local`**（你这台机器的主机名，如 `http://myhost.local`）
- **`http://printbox.local`**（PrintBox 品牌别名，可能需等待数秒生效）

## 插件 / Plugins

PrintBox 从 **0.2.55** 起支持插件：打印核心保持内置，其余功能（SMB 共享、时间机器、下载、智能家居、监控等）以插件形式扩展。插件分两类：

PrintBox supports **plugins since 0.2.55**: the print core stays built-in; other capabilities (SMB share, Time Machine, download, smart home, surveillance…) extend via plugins. Two kinds:

- **native（系统包）/ native (system package)**：插件本体是 Debian apt 包（如 `samba`、`aria2`），PrintBox 只写配置 + 托管 systemd。
  The app itself is a Debian apt package (e.g. `samba`, `aria2`); PrintBox only writes config and manages systemd.
- **container（Docker）/ container (Docker)**：直接复用现成镜像（如 Home Assistant、Frigate），PrintBox 只写 compose + 托管 systemd。
  Reuses an existing image (e.g. Home Assistant, Frigate); PrintBox only writes the compose file and manages systemd.

也可通过 `printbox plugin import` 把 CasaOS / Umbrel / runtipi 商店里的 docker-compose 一键转成 PrintBox 插件，直接复用别人做好的应用。
You can also run `printbox plugin import` to convert a CasaOS/Umbrel/runtipi store app's docker-compose into a PrintBox plugin, reusing apps others have built.

### 常用命令 / Common commands

```bash
sudo printbox plugin list                            # 列出已发现插件 / list discovered plugins
sudo printbox plugin enable storage-share --yes      # 启用 SMB 共享插件（自动装 samba、写配置、重启 smbd）/ enable SMB share
sudo printbox restart                                # 重启服务让新插件路由生效 / restart so the new plugin route takes effect
sudo printbox plugin disable storage-share           # 禁用 / disable
```

### 示例：把 USB 移动硬盘变成局域网共享 + 时间机器 / Example: turn a USB drive into a LAN share + Time Machine target

1. 先把移动硬盘挂载到某个目录，例如 `/mnt/usbhdd`（PrintBox 不负责挂载，可用 `mount` 或 fstab）。
   First mount the USB drive somewhere, e.g. `/mnt/usbhdd` (PrintBox doesn't mount it for you; use `mount` or fstab).
2. `sudo printbox plugin enable storage-share --yes`
3. `sudo printbox restart`
4. 浏览器打开 Web 界面 → 「插件」→「存储共享」，把共享目录填成 `/mnt/usbhdd`，按需开关「Time Machine」「允许访客」，保存。
   Open the Web UI → Plugins → Storage Share; set the share path to `/mnt/usbhdd`, toggle Time Machine / guest as needed, save.
5. macOS：访达 → 连接服务器 → `smb://<盒子IP>` → 在「时间机器」设置里即可选它作备份盘（fruit 模块已自动启用）。
   macOS: Finder → Connect to Server → `smb://<box-ip>` → in Time Machine settings it shows up as a backup disk (fruit module auto-enabled).

> ⚠️ **信任模型 / Trust model**：插件以 root 运行、可装系统包、可管服务。`enable` 第三方插件前会列出所需权限并要求确认——装别人写的插件等于授予 root。
> Plugins run as root and may install system packages and manage services. Enabling a third-party plugin lists required capabilities and asks for confirmation — installing someone else's plugin means granting root.

插件标准详见 / See [`PLUGIN_STANDARD.md`](PLUGIN_STANDARD.md) for the full plugin standard.

## 目录约定 / Filesystem layout (FHS)

| 用途 / Purpose | 路径 / Path |
|---|---|
| 配置 / Config | `/etc/printbox/config.json` |
| 状态/归档 / State & archive | `/var/lib/printbox`（含 `archive/`） |
| 日志 / Logs | `/var/log/printbox/*.log` |
| 命令 / CLI | `/usr/local/bin/printbox` |

## 支持的打印机 / Supported printers

- 管理层适配任意 CUPS 打印机。
  The management layer works with any CUPS printer.
- `foo2xqx` 补丁仅对使用 `foo2xqx` / `foomatic` 驱动的 HP 主机型激光打印机生效（如 LaserJet P1005 / P1505 / P2014 / M1005 / M1120 等）。
  The `foo2xqx` patch applies only to HP host-based laser printers driven by `foo2xqx`/`foomatic`.

## 许可证 / License

MIT
