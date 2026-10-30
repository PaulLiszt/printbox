# Runbook：安装 / 配置 / 运维

> 适用：目标机已装好 **Debian / Ubuntu（含衍生版，systemd）**，能联网、能 ssh 登录。
> PrintBox 是安装在该系统之上的 Python 应用（pip 包），**不再包含整个操作系统**。
> 网络、WiFi、主机名、系统账号等由底层 Linux 自行管理（与系统一致）。

---

## 0. 离线交付与升级（固定流程 · 推荐）

适用于「源码在开发机、目标盒离线/内网」的交付方式。**全流程只用两个脚本，产物位置固定，不再手敲命令、不再到处散包。**

### 0.1 开发机：构建源码包（产物唯一位置 = `dist/`）

```bash
cd printserver-firmware
bash tools/build_release.sh
# 输出： dist/printbox-<version>-src.tar.gz   ← 唯一权威交付物（版本号自动取自 pyproject.toml）
```

### 0.2 开发机 → 盒：传包（含升级脚本）

```bash
scp dist/printbox-<version>-src.tar.gz root@<盒IP>:~/
scp tools/upgrade_box.sh             root@<盒IP>:~/
```

### 0.3 盒上（root）：一键升级

```bash
ssh root@<盒IP>
bash upgrade_box.sh printbox-<version>-src.tar.gz
# 脚本自动：预装 setuptools/wheel → 用 --no-build-isolation 装包 → printbox repair
# 末尾应显示新版本号，且 WebUI 概览只列 1 台打印机（hp_laserjet_p1505n_printbox）
```

> **坑（已内置规避，不要手敲裸命令绕开）**：Debian/Ubuntu 的最小 venv **默认不含 setuptools**，
> 直接 `pip install .` 会报 `BackendUnavailable: Cannot import 'setuptools.build_meta'`。
> `upgrade_box.sh` 与 `install.sh --local` 都已内置 `pip install setuptools wheel` + `--no-build-isolation`。

---

## 1. 安装（一条命令）

在目标机上（root 或 sudo）：

```bash
# 方式 A：从 PyPI 在线安装（发布后）
curl -sSL https://raw.githubusercontent.com/PaulLiszt/printbox/main/install.sh | sudo bash

# 方式 B：本地仓库离线安装
sudo ./install.sh --local
```

脚本会自动：创建 venv `/opt/printbox/venv` → 安装 printbox → 软链 `/usr/local/bin/printbox` → 运行 `printbox init`。

`printbox init` 会：安装打印/网络/驱动 apt 依赖 → 写默认配置 `/etc/printbox/config.json` →
安装并启用 systemd 服务（web / archive / 打印机热插拔）→ 探测打印机（自动安装驱动）→ 启动服务。

完成后浏览器打开安装结束时显示的 **本机 IP**（最稳），或 `<主机名>.local` 即可管理。

---

## 2. 手动安装（等价步骤）

```bash
python3 -m venv /opt/printbox/venv
/opt/printbox/venv/bin/pip install printbox
ln -s /opt/printbox/venv/bin/printbox /usr/local/bin/printbox
sudo printbox init
```

> 提示：本机网络/WiFi/主机名请用系统自带工具（如 `nmtui` / `netplan` / `/etc/network`）配置，
> PrintBox 不接管这些。配置好后再运行 `printbox init`。

---

## 3. 常用命令

| 命令 | 作用 |
|---|---|
| `sudo printbox init` | 首次配置本机为打印服务器（装依赖 + 写服务 + 探测打印机） |
| `sudo printbox start` | 启动全部服务（开机自启已由 init 设置） |
| `sudo printbox stop` | 停止全部服务 |
| `sudo printbox restart` | 重启全部服务 |
| `sudo printbox status` | 查看服务运行状态 |
| `sudo printbox detect` | 重新探测/添加打印机（自动安装对应驱动） |
| `printbox web` | 前台运行 Web 管理界面（调试用） |
| `printbox archive --daemon` | 前台运行归档守护（调试用） |
| `printbox version` | 显示版本 |

---

## 4. Web 管理界面

- 地址：`http://<本机主机名>.local`（mDNS，需客户端支持，否则用 IP）。
- 导航：状态 / 打印机 / 归档 / 打印历史 / 设置 / 日志。
- **打印机**：点“检测并添加”自动发现 USB/IPP 打印机；可发测试页。
- **归档**：开关、调整占用比例/上下限、清空。
- **打印历史**：PDF 可预览，其它格式可下载；一键“再次打印”。
- **设置**：Web 登录开关与密码（默认不要求登录，仅局域网）。

---

## 5. 目录与文件

| 用途 | 路径 |
|---|---|
| 配置 | `/etc/printbox/config.json` |
| 状态/归档 | `/var/lib/printbox`（含 `archive/`、`state/`） |
| 日志 | `/var/log/printbox/*.log`（web / printer / drivers / archive / init） |
| systemd 单元 | `/etc/systemd/system/printbox-*.service` |
| udev 热插拔规则 | `/etc/udev/rules.d/99-printbox-printer.rules` |

---

## 6. 排错

- **`print.local` 打不开**：直接用 IP 访问；确认 `systemctl status avahi-daemon` 在运行。
- **打印机没被识别**：`sudo printbox detect` 看日志 `/var/log/printbox/printer.log`；
  特殊机型若驱动未自动装，可手动 `apt-get install printer-driver-<xxx>` 后再次 detect。
- **归档不增长**：确认 CUPS `PreserveJobFiles=yes`（`printbox init`/探测时已开启）；
  查看 `/var/log/printbox/archive.log`。
- **Web 起不来（端口 80 被占）**：确认没有其它服务占用 80；或改 `webapp.PORT` 后 `sudo printbox restart`。
- **升级（离线源码包，推荐）**：用 `tools/upgrade_box.sh`（见第 0 节），一条命令完成装包 + `printbox repair`。
- **升级（PyPI 在线）**：`/opt/printbox/venv/bin/pip install -U printbox` 然后 `sudo printbox init`。
  > 注意：以上 `pip install` 在 Debian 裸 venv 上需先 `pip install setuptools wheel` 并加 `--no-build-isolation`，否则报 `BackendUnavailable`。优先用 `upgrade_box.sh` 避免此坑。

---

## 7. 发布到 PyPI（维护者用）

```bash
python3 -m pip install build twine
python3 -m build
twine upload dist/*
```
发布后，`install.sh`（非 --local）即可从 PyPI 安装。
