# PrintBox

> 把一台已经装好 Linux（Debian / Ubuntu 等 systemd 发行版）的主机，变成无头打印服务器。
> Turn any Linux box (Debian/Ubuntu or another systemd distro) into a headless print server.

> **产品形态 / Product form**
> PrintBox 是一个 **pip 分发的 Python 应用**，不包含操作系统镜像。网络参数、主机名、系统账号等由底层 Linux 自行管理（与系统一致）；PrintBox 只负责把打印服务栈跑起来并提供管理界面。
> PrintBox is a **pip-distributed Python application**, not a full OS image. Network settings, hostname, and system accounts are managed by the underlying Linux (consistent with the system); PrintBox only brings up the print-service stack and provides a management UI.

## 功能特性 / Features

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

**一行命令从 PyPI 安装 / Install from PyPI in one line：**

```bash
pip install printbox
```

初始化（需 root，自动装系统依赖、写服务、探测打印机）/ Initialize (needs root; auto-installs system deps, writes services, probes printers):

```bash
sudo printbox init
```

**或全自动一键 / Or the all-in-one one-liner**（GitHub 安装脚本，自动建 venv + 安装 + 初始化 / GitHub installer: auto venv + install + init）：

```bash
curl -sSL https://raw.githubusercontent.com/PaulLiszt/printbox/main/install.sh | sudo bash
```

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
