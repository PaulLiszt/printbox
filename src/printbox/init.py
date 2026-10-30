"""PrintBox 系统初始化：把一台已装好 Debian/Ubuntu（systemd）的机器配置为打印服务器。

由 `sudo printbox init` 调用。执行：
  1) apt 安装打印/网络/驱动依赖
  2) 写入默认配置
  3) 安装并启用 systemd 服务 + udev 热插拔规则
  4) 探测打印机（自动安装驱动）
  5) 启动服务
"""
import os
import re
import sys
import shutil
import socket
import subprocess

from . import config as C
from . import drivers
from . import discovery

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

SYSTEMD_UNITS = [
    "printbox-web.service",
    "printbox-archive.service",
    "printbox-printer-hotplug.service",
    "printbox-guard.service",
]
# 额外单元：定时器 / 一次性服务（不在上面常驻循环里 enable+restart，单独处理）
EXTRA_UNITS = [
    "printbox-mdns-renew.service",
    "printbox-mdns-renew.timer",
    "printbox-guard.timer",
]
UDEV_RULE = "99-printbox-printer.rules"

# 核心依赖：打印/网络/驱动必备，缺失会直接导致打印不可用（应明确报错）
# 注意：cups-browsed 仅“安装”但不“启用”——它是订阅风暴（Create-Printer-Subscriptions
# 循环）的根因，且本服务器通过显式 Avahi AirPrint 服务文件发布打印机，并不依赖它。
CORE_APT = [
    "cups", "cups-client", "cups-browsed", "cups-filters", "cups-ipp-utils",
    "avahi-daemon", "avahi-utils", "ghostscript", "poppler-utils", "python3",
    # 默认装好 WiFi 管理与 SSID 检测能力，避免 WebUI 误报「未连接 / 未安装 NetworkManager」
    "network-manager", "iw",
]
# 可选依赖：发行版间可能更名/移除（如 wsdd→wsdd-server、ipp-usb 并非所有机器需要），
# 装不上不影响核心功能，静默跳过即可。
OPT_APT = ["ipp-usb", "wsdd2", "wsdd-server", "wsdd"]


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _lan_ip():
    """获取本机局域网 IPv4（不依赖特定接口/发行版，且不要求能出外网）。

    旧实现只靠对 8.8.8.8 的 UDP 探测拿 IP，盒子在随身 WiFi / 封闭网络下出不了公网时
    返回空串，导致 `_publish_printbox_alias` 静默跳过、printbox.local 永不宣告。
    改为：1) `hostname -I`（离线即可列出本机所有 IPv4）取首个非回环地址；
    2) 回退解析 `ip -4 addr` 的非 lo 接口地址；3) 最后才回退到对公网的 UDP 探测。"""
    # 1) 优先 hostname -I（无需外网）
    try:
        rc, out, _ = C.run(["hostname", "-I"])
        if rc == 0:
            for tok in out.split():
                if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", tok) and tok != "127.0.0.1":
                    return tok
    except Exception:
        pass
    # 2) 回退：解析首个非回环 IPv4 接口地址（无需外网连通）
    try:
        rc, out, _ = C.run(["ip", "-o", "-4", "addr", "show"])
        if rc == 0:
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 4 and parts[1] != "lo":
                    addr = parts[3].split("/")[0]
                    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", addr) and addr != "127.0.0.1":
                        return addr
    except Exception:
        pass
    # 3) 最后回退：对公网地址做 UDP 探测（需能出网）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = ""
    finally:
        try:
            s.close()
        except Exception:
            pass
    return ip


def _hostname():
    try:
        return socket.gethostname()
    except Exception:
        return "printbox"


# printbox.local 别名发布：用 avahi-utils 自带的 `avahi-publish` 把 printbox.local 作为本机
# 局域网 IP 的 mDNS 地址记录持续注册。该工具是 avahi 官方标准组件、跨版本可靠；相比 0.2.15
# 误用的 `publish-hosts-in-file`（非标准键、会让 avahi 直接 exit 255 死掉）稳健得多。
# IP 在脚本启动时才取，配合 Restart=always 与 15 分钟 renew 定时器，avahi 重启/换 IP 都会自动重注册。
_ALIAS_SCRIPT = """#!/bin/bash
# PrintBox: 把 printbox.local 作为本机局域网 IP 的 mDNS 地址记录持续注册。
# 由 systemd 单元 printbox-mdns-alias.service 调用；Restart=always 保证 avahi 重启后自动重注册。
IP=$(hostname -I 2>/dev/null | tr ' ' '\\n' | grep -E '^[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+$' | grep -v '^127\\.' | head -n1)
if [ -z "$IP" ]; then
    IP=$(ip -4 -o addr show 2>/dev/null | awk 'NR==1 {print $4}' | cut -d/ -f1)
fi
if [ -z "$IP" ]; then
    echo "printbox-mdns-alias: 无法获取本机局域网 IP，放弃发布 printbox.local" >&2
    exit 1
fi
exec /usr/bin/avahi-publish -a -R printbox.local "$IP"
"""

_ALIAS_UNIT = """[Unit]
Description=PrintBox mDNS 别名发布（printbox.local -> 本机局域网 IP，供 WebUI 访问）
After=avahi-daemon.service
Requires=avahi-daemon.service

[Service]
Type=simple
ExecStart=/usr/local/bin/printbox-mdns-alias.sh
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
"""


def _publish_printbox_alias():
    """发布 printbox.local 别名（让 WebUI 可通过 http://printbox.local 访问）。

    背景与坑：avahi 没有 `publish-hosts-in-file` 这种标准键（官方 [publish] 段只有
    publish-addresses/publish-hostname 等），0.2.15 误加该键导致 avahi 解析配置失败、
    daemon 直接 exit 255、所有 .local 全废。而 /etc/avahi/hosts 是否被自动读取也随版本各异、
    不可靠。

    可靠做法（跨 avahi 版本均有效）：用 avahi-utils 自带的 `avahi-publish -a -R` 进程，
    把 printbox.local 作为本机局域网 IP 的地址记录持续注册到 mDNS。该进程是官方标准工具，
    任何装了 avahi-utils 的盒子都能用；配合 systemd 的 Restart=always，avahi 重启后
    也会自动重新注册。IP 在每次启动时从 hostname -I 现取，并由 15 分钟 renew 定时器刷新。

    本函数负责：写入发布脚本 + systemd 单元、清理旧版不可靠机制，并启用/启动该服务。
    """
    try:
        # 清理 0.2.13 及之前的 device-info 旧 hack（已废弃）
        old = "/etc/avahi/services/printbox-alias.service"
        if os.path.exists(old):
            try:
                os.remove(old)
            except Exception:
                pass
        # 清理 /etc/avahi/hosts 里残留的 printbox 条目（旧机制，已弃用，避免与 avahi-publish 重复宣告）
        hosts = "/etc/avahi/hosts"
        if os.path.exists(hosts):
            try:
                with open(hosts, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
                out = [ln for ln in lines
                       if not (ln.strip() and not ln.strip().startswith("#")
                               and re.search(r"(?<![\w.-])printbox(?![\w.-])", ln))]
                if len(out) != len(lines):
                    with open(hosts, "w", encoding="utf-8") as f:
                        f.writelines(out)
            except Exception:
                pass
        # 写入发布脚本与 systemd 单元
        script_path = "/usr/local/bin/printbox-mdns-alias.sh"
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(_ALIAS_SCRIPT)
        os.chmod(script_path, 0o755)
        unit_path = "/etc/systemd/system/printbox-mdns-alias.service"
        with open(unit_path, "w", encoding="utf-8") as f:
            f.write(_ALIAS_UNIT)
        # 启用并启动：别名服务 Requires=avahi-daemon，systemd 会先把 avahi 拉起
        C.run(["systemctl", "daemon-reload"])
        C.run(["systemctl", "enable", "avahi-daemon"])
        C.run(["systemctl", "start", "avahi-daemon"])
        C.run(["systemctl", "enable", "printbox-mdns-alias.service"])
        C.run(["systemctl", "restart", "printbox-mdns-alias.service"])
        rc, _, _ = C.run(["systemctl", "is-active", "--quiet", "printbox-mdns-alias.service"])
        if rc == 0:
            C.log("init", "INFO", "已通过 avahi-publish 发布 printbox.local 别名（跨版本稳定）")
        else:
            C.log("init", "WARN",
                  "printbox.local 别名服务启动异常，WebUI 仍可经 IP/<主机名>.local 访问")
    except Exception as e:
        C.log("init", "WARN", f"发布 printbox.local 别名失败（可忽略，稍后可手动修复）: {e}")


def _ensure_avahi_hostname():
    """幂等加固 /etc/avahi/avahi-daemon.conf，确保 <主机名>.local 稳定可解析。

    背景：用户反馈 http://<主机名>.local 在放置约两天后无法打开（IP 仍可访问），
    典型为 mDNS 主机名记录过期后未重新宣告，或配置里 publish-hostname/publish-addresses
    未显式开启。    本函数确保以下键值存在且正确（兼容已被注释掉的行）：
      host-name=<系统主机名>      # [server] 段；avahi 据此自动发布 <主机名>.local
      publish-addresses=yes       # [publish] 段；宣告本机 IP
    注意：「发布主机名」不需要任何独立键（avahi 根本没有 publish-hostname 这种键）；
    主机名完全由 [server].host-name 控制。任何补充键都必须先在盒上 `man avahi-daemon.conf`
    或实测确认，并由 _self_heal_avahi_conf() 在写完后真跑一次 avahi 兜底。"""
    path = "/etc/avahi/avahi-daemon.conf"
    host = _hostname()
    # [server] 段只放标准键（跨版本有效）
    server_keys = {
        "host-name": host,
        "use-ipv4": "yes",
        "use-ipv6": "yes",
    }
    # [publish] 段只放标准键（均为 avahi 官方支持的键）：
    # - publish-addresses=yes：宣告本机 IP（<主机名>.local 稳定可解析）
    # 注意：「发布主机名」不需要独立键 —— 主机名由 [server] 段的 host-name 控制，avahi 据此
    # 自动发布 <主机名>.local。下面这些都不是 avahi 的标准键、写入会直接 exit 255 致死
    # （history 已三次踩雷，全部是「不存在的键」）：
    #   - ServerAliases        （0.2.12 引入，致命）
    #   - publish-hosts-in-file（0.2.15 引入，致命）
    #   - publish-hostname     （0.2.15 引入、0.2.16 漏删，致命 —— 此键本身不存在！）
    # 任何补充键前，务必先在盒上 `man avahi-daemon.conf` 或实测确认；保险起见由
    # _self_heal_avahi_conf() 在写完配置后真跑一次 avahi，发现非法键自动剔除。
    publish_keys = {
        "publish-addresses": "yes",
    }
    try:
        cur = ""
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                cur = f.read()
        # 关键修正：所有目标键都必须落在 [server] 段内。
        # 旧实现把缺失的键追加到文件末尾，而 Debian 默认文件末尾是 [rlimits] 段，
        # 导致 publish-hostname 被写到 [rlimits] 下，avahi 启动报
        # “Invalid configuration key publish-hostname in group rlimits” 并 exit 255，
        # 进而 mDNS 全死、局域网发现不了打印机。
        all_keys = list(server_keys.keys()) + list(publish_keys.keys()) + [
            "publish-hostname", "ServerAliases", "publish-hosts-in-file"]
        # 1) 先移除所有行内（含被注释）这些键的出现，避免残留在错误段
        kept = []
        for line in cur.splitlines():
            if re.match(r"^\s*#?\s*(" + "|".join(all_keys) + r")\s*=", line):
                continue
            kept.append(line)
        text = "\n".join(kept)
        # 2) 确保 [server] 段存在并写入标准键
        if not re.search(r"^\s*\[server\]", text, flags=re.MULTILINE):
            if text and not text.endswith("\n"):
                text += "\n"
            text += "\n[server]\n"
        ins_server = "\n".join(f"{k}={v}" for k, v in server_keys.items())
        text = re.sub(r"(\n[^\n]*\[server\][^\n]*\n)", r"\1" + ins_server + "\n",
                      text, count=1, flags=re.MULTILINE)
        # 3) 确保 [publish] 段存在并写入 publish-addresses（正确段！）
        if not re.search(r"^\s*\[publish\]", text, flags=re.MULTILINE):
            if text and not text.endswith("\n"):
                text += "\n"
            text += "\n[publish]\n"
        ins_publish = "\n".join(f"{k}={v}" for k, v in publish_keys.items())
        text = re.sub(r"(\n[^\n]*\[publish\][^\n]*\n)", r"\1" + ins_publish + "\n",
                      text, count=1, flags=re.MULTILINE)
        if not text.endswith("\n"):
            text += "\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        # 写完立即自愈：真跑一次 avahi，若因非法键 exit 255 则自动剔除该键并重试
        # （ServerAliases / publish-hosts-in-file / publish-hostname 都曾让 avahi 全死）。
        _self_heal_avahi_conf()
        C.log("init", "INFO",
              f"已加固 avahi-daemon.conf（host-name={host}, 发布主机名与地址）")
    except Exception as e:
        C.log("init", "WARN", f"加固 avahi-daemon.conf 失败（可忽略，但 .local 可能不稳定）: {e}")


def _remove_avahi_key(key):
    """从 /etc/avahi/avahi-daemon.conf 删除某个键（含被注释的行），不关心所在段。"""
    path = "/etc/avahi/avahi-daemon.conf"
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        out = [ln for ln in lines
               if not re.match(r"^\s*#?\s*" + re.escape(key) + r"\s*=", ln)]
        if len(out) != len(lines):
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(out)
            return True
    except Exception as e:
        C.log("init", "WARN", f"移除 avahi 配置键 [{key}] 失败: {e}")
    return False


def _self_heal_avahi_conf():
    """自愈：avahi 对未知配置键会直接 exit 255 致死（已三次踩雷：ServerAliases /
    publish-hosts-in-file / publish-hostname，全是「不存在的键」）。写完配置后真正拉起
    avahi-daemon，若启动失败则解析 journalctl 的 'Invalid configuration key "X" in group "Y"'，
    把 X 从配置删掉重试，直到 avahi 起来或确认不是配置键问题。彻底杜绝『非法键来回致死』。"""
    import time
    try:
        if _run(["systemctl", "is-enabled", "avahi-daemon"]).returncode != 0:
            return  # 未安装，无需自愈
    except Exception:
        return
    for _ in range(15):
        _run(["systemctl", "restart", "avahi-daemon"])
        time.sleep(1.5)
        if _run(["systemctl", "is-active", "--quiet", "avahi-daemon"]).returncode == 0:
            C.log("init", "INFO", "avahi-daemon 配置自检通过，已正常启动")
            return
        jl = _run(["journalctl", "-u", "avahi-daemon", "-n", "15",
                   "--no-pager", "-o", "cat"]).stdout
        m = re.search(r'Invalid configuration key "([^"]+)"', jl or "")
        if not m:
            C.log("init", "WARN",
                  "avahi-daemon 启动失败，但日志无『非法配置键』，停止自愈（见 journalctl）")
            return
        bad = m.group(1)
        C.log("init", "WARN", f"avahi 配置含非法键 [{bad}]，自动移除后重试")
        if not _remove_avahi_key(bad):
            return
    C.log("init", "WARN",
          "avahi 配置自愈超过 15 次仍未通过，请手动检查 /etc/avahi/avahi-daemon.conf")


def _install_units():
    dst = "/etc/systemd/system"
    os.makedirs(dst, exist_ok=True)
    os.makedirs("/etc/udev/rules.d", exist_ok=True)
    for u in SYSTEMD_UNITS:
        src = os.path.join(DATA_DIR, u)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dst, u))
            C.log("init", "INFO", f"已安装服务单元: {u}")
    # 额外单元（定时器/一次性服务）：仅复制，启用在 provision 里单独处理
    for u in EXTRA_UNITS:
        src = os.path.join(DATA_DIR, u)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dst, u))
            C.log("init", "INFO", f"已安装额外单元: {u}")
    src = os.path.join(DATA_DIR, UDEV_RULE)
    if os.path.exists(src):
        shutil.copy2(src, os.path.join("/etc/udev/rules.d", UDEV_RULE))
        C.log("init", "INFO", f"已安装 udev 规则: {UDEV_RULE}")
    # 确保 /usr/local/bin/printbox 软链指向当前 venv 可执行文件。
    # 所有服务单元（hotplug/web/archive/mdns-*/alias）都写死 ExecStart=/usr/local/bin/printbox，
    # 若该软链缺失或指向旧版本，热插拔/服务会跑旧代码（历史上因此重建短名幽灵队列、双显不收敛）。
    # 这里在每次安装/修复时幂等重建，确保服务单元永远跑当前版本。
    try:
        venv_bin = os.path.dirname(os.path.realpath(sys.executable))
        venv_printbox = os.path.join(venv_bin, "printbox")
        link = "/usr/local/bin/printbox"
        if os.path.exists(venv_printbox):
            if os.path.islink(link):
                if os.path.realpath(link) != os.path.realpath(venv_printbox):
                    os.remove(link)
                    os.symlink(venv_printbox, link)
                    C.log("init", "INFO", f"已更新软链 {link} -> {venv_printbox}")
            elif not os.path.exists(link):
                os.symlink(venv_printbox, link)
                C.log("init", "INFO", f"已建立软链 {link} -> {venv_printbox}")
            else:
                # 已存在且为真实文件（非软链），不擅自删除，仅告警
                C.log("init", "WARN",
                      f"{link} 已是普通文件、未建立软链；服务单元若用绝对路径请确认其指向当前版本")
        else:
            C.log("init", "WARN", f"未找到 venv 可执行文件 {venv_printbox}，跳过软链建立")
    except Exception as e:
        C.log("init", "WARN", f"建立 {link} 软链失败（可忽略，服务单元仍可手动指向 venv）: {e}")


def _apt_install(pkgs):
    """逐包安装：单个包在源中不存在（已更名/移除）时跳过并继续，
    避免整批安装因一个坏包名而全部中止（Debian trixie 常见：wsdd→wsdd-server 等）。"""
    ok = True
    for pkg in pkgs:
        C.log("init", "INFO", f"apt 安装: {pkg}")
        r = _run(["apt-get", "install", "-y", pkg])
        if r.returncode != 0:
            if "Unable to locate" in r.stderr or "Can't find a source" in r.stderr:
                C.log("init", "WARN", f"包在源中不存在，已跳过（可能已更名/移除）: {pkg}")
            else:
                C.log("init", "WARN", f"包安装失败: {pkg} | {r.stderr[-200:]}")
                ok = False
    return ok


def _ensure_cupsd_limits():
    """持久化抬高 CUPS IPP 订阅上限（写进 cupsd.conf，重启后依然生效）。

    防御性措施：即便某本地客户端（历史上 cups-browsed）反复建订阅，也不会再撞
    默认 100 的上限而拖垮调度器（表现为 'Scheduler shutting down due to program
    error' 循环）。

    0.2.48 重构：改用 printers.ensure_cupsd_managed 统一写文件 + 去重，
    不再依赖注释 mark（cupsctl 重写配置时会剥掉注释，导致 mark 失效、每次 init
    又追加 MaxSubscriptions 块，把 cupsd.conf 撑到几十遍重复、放大崩溃）。"""
    try:
        from . import printers
        printers.ensure_cupsd_managed()
    except Exception as e:
        C.log("init", "WARN", f"写入 CUPS 订阅上限失败（可忽略）: {e}")


def _ensure_cups_lan():
    """让 CUPS 监听局域网（而非仅 localhost），并允许本地子网访问。

    根因：Debian 默认 cupsd.conf 为 `Listen localhost:631`，只在 127.0.0.1:631 上监听。
    这导致局域网内其他设备（iPhone / Win10）连接 <host>:631 时直接 `connection refused`，
    即便 mDNS/WSD 能发现打印机，真正连 IPP 端口时也会失败，表现为“发现不了/加不上”。

    本函数幂等：
      1) 把 `Listen localhost:631`（及 127.0.0.1 / [::1] 变体）替换为 `Port 631`
         （监听 IPv4+IPv6 全部网卡）；
      2) 在 <Location /> 与 <Location /printers> 注入 `Allow from @LOCAL`
         （@LOCAL = 所有非回环本地地址）；
      3) 关闭 Browsing（0.2.33 起不再依赖 CUPS 自身广播/浏览，仅由 PrintBox 的 Avahi
         服务文件发布打印机）。详见下方步骤 3 注释——Browsing On 是 0.2.32 及之前
         cupsd 崩溃循环（driverless 坏行）与 iOS 双显的真凶。"""
    path = "/etc/cups/cupsd.conf"
    try:
        if not os.path.exists(path):
            C.log("init", "WARN", "cupsd.conf 不存在，跳过局域网监听配置")
            return
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            cfg = f.read()

        changed = False

        # 1) 监听所有网卡：移除 localhost-only 的 Listen，补一条 Port 631
        listen_patterns = [
            r"^Listen\s+localhost:631\s*$",
            r"^Listen\s+127\.0\.0\.1:631\s*$",
            r"^Listen\s+\[::1\]:631\s*$",
        ]
        new_cfg = cfg
        for pat in listen_patterns:
            new_cfg = re.sub(pat, "", new_cfg, flags=re.MULTILINE)
        if new_cfg != cfg:
            cfg = new_cfg
            changed = True
        if not re.search(r"^\s*Port\s+631\s*$", cfg, flags=re.MULTILINE):
            cfg = "Port 631\n" + cfg
            changed = True

        # 2) 允许局域网访问 <Location /> 与 <Location /printers>
        for loc in ("/", "/printers"):
            block_re = re.compile(
                r"(<Location\s+" + re.escape(loc) + r">.*?</Location>)", re.DOTALL
            )
            m = block_re.search(cfg)
            if m:
                block = m.group(1)
                if "Allow from @LOCAL" not in block:
                    new_block = block.replace(
                        "</Location>", "  Allow from @LOCAL\n</Location>", 1
                    )
                    cfg = cfg[: m.start()] + new_block + cfg[m.end():]
                    changed = True
            else:
                cfg += (
                    f"\n<Location {loc}>\n  Order allow,deny\n  Allow from @LOCAL\n"
                    f"</Location>\n"
                )
                changed = True

        # 3) 关闭 Browsing：0.2.33 起 PrintBox 不再依赖 CUPS 自身的 DNS-SD 广播/浏览。
        #    根因（0.2.32 及之前 Browsing On 的代价，已用 diag_box.sh 在盒上确认）：
        #      - Browsing On 会让 cupsd 把自己“共享”的打印机也通过 Bonjour/DNS-SD 广播出去，
        #        与 PrintBox 的显式 Avahi 服务文件（airprint-*.service）形成【第二份同名广播】，
        #        导致 iOS/macOS 看到“两台同名打印机”（其一是 cupsd 自广播、其二是我们的文件）；
        #      - Browsing On 同时开启 cupsd 对局域网的打印机【浏览/发现】，cupsd 会把 PrintBox
        #        自己发布的 AirPrint（ipp://<队列>._ipp._tcp.local/）当成“新发现的远程打印机”，
        #        调用 driverless 解析其驱动，driverless 对该 URI 返回畸形行
        #        （Bad line from "driverless": "driverless:ipp://..."），cups-driverd 报错后 cupsd
        #        直接 “Scheduler shutting down due to program error” 每 ~15s 崩溃循环，
        #        所有作业因此 Filter failed（WebUI 测试页/手机提交都打不出）。
        #    改 Browsing Off 后：cupsd 不再广播自身共享打印机（仅 PrintBox 的 Avahi 服务文件广播）
        #    → iOS/鸿蒙/Win10 只看到 1 台；已共享（printer-is-shared=true）队列仍由 cupsd 在
        #    Port 631 正常提供 IPP 服务，手机经我们发布的 AirPrint TXT 直连照常打印，不受影响。
        #    ★ 重要教训（0.2.33 踩坑）：绝不能追加 "BrowseRemoteProtocols none"。该指令在本机
        #    CUPS（Debian trixie / CUPS 2.4）上并非合法指令，会被记成
        #    “Unknown directive BrowseRemoteProtocols” → cupsd 解析 cupsd.conf 失败、回退到默认配置
        #    （默认 Browsing On）→ 既重新广播自身打印机（iOS 双显回潮），又继续崩溃循环。
        #    0.2.33 升级后“仍打不出 + iOS 仍双显”正是这行非法指令所致，本版彻底删除。
        #    ★ driverless 崩溃循环不靠 Browsing 开关解决（driverless 是 cups-driverd 独立发起的
        #    扫描，与 Browsing 开关无关）：由 init()/repair() 调用 _disable_driverless_backend()
        #    禁用 driverless/dnssd 后端根治。见该函数注释。
        for key, val in (("Browsing", "Off"),):
            if re.search(r"^\s*" + re.escape(key) + r"\s+", cfg, flags=re.MULTILINE):
                if not re.search(r"^\s*" + re.escape(key) + r"\s+" + re.escape(val) + r"\s*$",
                                 cfg, flags=re.MULTILINE):
                    cfg = re.sub(r"^\s*" + re.escape(key) + r"\s+.*$",
                                 f"{key} {val}", cfg, flags=re.MULTILINE)
                    changed = True
            else:
                cfg += f"\n{key} {val}\n"
                changed = True

        # 4) 关闭 CUPS 自身对本地打印机的 DNS-SD 广播：PrintBox 已通过显式 Avahi 服务文件
        #    发布每台打印机（airprint-*.service），CUPS 再广播同名 _ipp._tcp 会与前者碰撞
        #    （日志中 Failed to add service ... _ipp collision 的根因）。关掉 CUPS 这份重复广播，
        #    既消除碰撞噪音，也不影响手机/电脑发现打印机（靠 PrintBox 的服务文件即可）。
        if re.search(r"^\s*BrowseLocalProtocols\s+", cfg, flags=re.MULTILINE):
            if not re.search(r"^\s*BrowseLocalProtocols\s+none\s*$", cfg, flags=re.MULTILINE):
                cfg = re.sub(r"^\s*BrowseLocalProtocols\s+.*$",
                             "BrowseLocalProtocols none", cfg, flags=re.MULTILINE)
                changed = True
        else:
            cfg += "\nBrowseLocalProtocols none\n"
            changed = True

        # 5) 删除任何遗留的非法 BrowseRemoteProtocols 指令。0.2.33 曾误写该行，本机
        #    CUPS 2.4 不认 → cupsd 解析失败、回退默认(默认 Browsing On) → 崩溃循环 + iOS 双显。
        #    即便本函数历史版本没写过，也兜底清掉，杜绝复发；本机 Browsing Off 已关闭所有
        #    浏览/广播，BrowseRemoteProtocols 已无任何作用。
        new_cfg = re.sub(r"^[ \t]*BrowseRemoteProtocols[^\n]*\n?", "", cfg,
                         flags=re.MULTILINE)
        if new_cfg != cfg:
            cfg = new_cfg
            changed = True

        if changed:
            with open(path, "w", encoding="utf-8") as f:
                f.write(cfg)
            C.log("init", "INFO",
                  "已配置 CUPS 监听局域网并允许本地子网访问（cupsd.conf）")
        else:
            C.log("init", "INFO", "CUPS 局域网监听配置已就绪（无需修改）")

        # 防御性自愈：cupsd -t 校验；若报 Unknown directive，自动删除该非法行并重试，
        # 根绝『某版本误写非法指令 → cupsd 解析失败 → 回退默认 Browsing On → 反复崩溃』
        # 这类问题（0.2.33→0.2.34 反复踩坑即为此）。
        _repair_cupsd_conf_unknown_directives(path)
    except Exception as e:
        C.log("init", "WARN",
              f"配置 CUPS 局域网监听失败（可忽略，但打印将仅限本机）: {e}")


def _repair_cupsd_conf_unknown_directives(path):
    """cupsd -t 校验 cupsd.conf：若报 `Unknown directive X on line N`，把该行删掉后重试，作为防御性
    自愈，避免任何版本误写非法指令导致 cupsd 解析失败、回退默认(默认 Browsing On)、崩溃循环
    （这正是 0.2.33→0.2.34 反复踩的坑）。最多尝试若干次，直到校验通过或遇到非『未知指令』类错误。"""
    import shutil as _sh
    cupsd = _sh.which("cupsd") or "/usr/sbin/cupsd"
    for _ in range(10):
        r = _run([cupsd, "-t", "-c", path])
        rc = r.returncode
        err = r.stderr
        if rc == 0:
            return True
        m = re.search(r"Unknown directive\s+(\S+)\s+on line\s+(\d+)", err or "")
        if not m:
            # 其它语法错误（如缺闭合标签）无法自动修复，交人工处理
            C.log("init", "WARN",
                  f"cupsd -t 校验失败（非未知指令类，需人工处理）: {err}")
            return False
        lineno = int(m.group(2))
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            if not (1 <= lineno <= len(lines)):
                return False
            bad = lines[lineno - 1].strip()
            del lines[lineno - 1]
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            C.log("init", "WARN",
                  f"cupsd.conf 含非法指令已自动删除: '{bad}' (line {lineno})")
        except Exception as e:
            C.log("init", "WARN", f"删除 cupsd.conf 非法指令失败: {e}")
            return False
    return False


def _wait_cups_ready(timeout=30):
    """轮询直到 CUPS 调度器真正就绪（lpstat -r 返回 "scheduler is running"）。

    避免 detect/切驱动在调度器还没起来时就跑（那时 lpadmin 会报 Service Unavailable）。
    注意：必须精确匹配 "scheduler is running"，否则 "scheduler is not running" 也会因含
    "running" 子串被误判为就绪。"""
    import time
    for _ in range(timeout):
        r = _run(["lpstat", "-r"])
        if "scheduler is running" in (r.stdout or ""):
            return True
        time.sleep(1)
    return "scheduler is running" in (_run(["lpstat", "-r"]).stdout or "")


def _ensure_cups_stopped(timeout=20):
    """轮询直到 cups 服务真正停止；超时则硬杀 cupsd 进程，确保我们离线改 printers.conf 时
    cupsd 不会又把它写崩（崩溃循环场景下最常见：cupsd 一边崩一边被 systemd 拉起，我们的编辑
    还没落盘就被覆盖/截断）。

    0.2.40 及之前 repair 仅 `systemctl stop cups` 后立刻改 printers.conf 并 `restart`，
    崩溃循环中的 cupsd 可能仍持有/重写该文件，导致清场不彻底、cupsd 重启即崩、main() 永远没机会
    重建队列。这里显式等其退出、不行就 pkill，等价于用户手工验证可用的「先彻底停稳再改」。

    0.2.44：无论是否超时，最后都再 `pkill -x cupsd` 一次并稍候，确保没有任何残留 cupsd 进程
    （崩溃循环里 systemd 可能在 `is-active` 返回非活跃后又把 cupsd 拉起，赶在我们编辑文件前重写
    printers.conf）。配合上方调用处的 `systemctl mask cups`，可彻底杜绝编辑窗口内的重写。"""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _run(["systemctl", "is-active", "--quiet", "cups"]).returncode != 0:
            break
        time.sleep(1)
    # 兜底：无论如何都杀掉任何残留 cupsd 进程，避免它在我们离线编辑 printers.conf 期间重写该文件。
    _run(["pkill", "-x", "cupsd"])
    time.sleep(1)
    return True


def _cupsd_recently_crashed(window=30):
    """检查最近 window 秒内 systemd 是否记录 cups.service 崩溃（status=1/FAILURE）。

    用于 _stabilize_cups_for_rebuild 在「cupsd 看似起来了但仍崩溃循环」时触发紧急全量清场。
    注意：cupsd 的 `Scheduler shutting down due to program error` 只写 error_log、不进 journald；
    journald 里对应的真实崩溃信号是 cups.service 的 `status=1/FAILURE`（与 diag_box.sh 同源判断）。"""
    r = _run(["journalctl", "-u", "cups", "--since", f"{window} seconds ago",
              "--no-pager", "-o", "cat"])
    return "status=1/FAILURE" in (r.stdout or "")


def _nuclear_reset_printers():
    """核弹级兜底（手动恢复等价）：mask+stop+确保退出 → 把 printers.conf **整体移走**（而非只编辑块）
    → 删除全部 PPD → 干净重启。等价于用户手工验证可用的
    `mv printers.conf printers.conf.old; rm -f /etc/cups/ppd/*.ppd; systemctl start cups`。

    为何需要这一层（0.2.45 新增）：_stabilize 的离线清场是「正则匹配 <Printer> 块后删除」。
    但崩溃循环里 cupsd 会把 printers.conf 截断/写崩（memory 反复记过），一旦损坏到正则匹配不到
    任何块，编辑就空手而归、损坏文件原样留下 → cupsd 启动即崩、main() 永远没机会重建。
    只有「整体移除文件」能确保 cupsd 拿到可解析的空配置并稳定启动。manual 恢复正是这么做的，
    故把它固化进自动化修复链路。"""
    import time as _t
    from . import printers
    _run(["systemctl", "mask", "cups"])
    _run(["systemctl", "stop", "cups"])
    _ensure_cups_stopped(timeout=20)
    try:
        pc = "/etc/cups/printers.conf"
        if os.path.exists(pc):
            bak = f"{pc}.bak.{int(_t.time())}"
            os.rename(pc, bak)
            C.log("init", "INFO", f"已整体移走损坏的 printers.conf -> {bak}（crash 循环核弹级兜底）")
        ppd_dir = "/etc/cups/ppd"
        if os.path.isdir(ppd_dir):
            for fn in os.listdir(ppd_dir):
                if fn.endswith(".ppd"):
                    try:
                        os.remove(os.path.join(ppd_dir, fn))
                    except Exception:
                        pass
    except Exception as e:
        C.log("init", "WARN", f"核弹级重置失败: {e}")
    _run(["systemctl", "unmask", "cups"])
    _run(["systemctl", "enable", "cups"])
    _run(["systemctl", "start", "cups"])
    _wait_cups_ready(timeout=60)


def _emergency_full_wipe_and_restart():
    """紧急全量清场（两层兜底）：
      第一层：mask+stop+等退出 → 离线摘除【全部】打印机块与 PPD → unmask+enable+start → 等就绪；
      第二层（第一层后 cupsd 仍崩）：核弹级 —— 整体移除 printers.conf（手动恢复等价）。
    仅在「常规清场后 cupsd 仍崩溃」的极端情况下调用，作为打断崩溃循环的最后手段。"""
    from . import printers
    _run(["systemctl", "mask", "cups"])
    _run(["systemctl", "stop", "cups"])
    _ensure_cups_stopped(timeout=20)
    try:
        printers._wipe_all_printers_offline()
    except Exception as e:
        C.log("init", "WARN", f"紧急全量清场失败: {e}")
    _run(["systemctl", "unmask", "cups"])
    _run(["systemctl", "enable", "cups"])
    _run(["systemctl", "start", "cups"])
    if _wait_cups_ready(timeout=60) and not _cupsd_recently_crashed():
        return
    # 第一层仍崩：结构化解场因文件损坏无法匹配坏块，升级为核弹级（整体移走 printers.conf）
    C.log("init", "WARN",
          "结构化解场后 cupsd 仍崩，升级为核弹级重置（整体移走 printers.conf）")
    _nuclear_reset_printers()


def _rebuild_printers_with_guard():
    """探测并重建打印机队列，并在重建后复核 cupsd 稳定性。

    为何需要（0.2.45 新增）：provision/repair 在 _stabilize 后调用 printers.main() 重建队列；
    极端情况下 main() 重建的队列（如驱动/id 解析偶发异常）会让 cupsd 再次崩溃。这里在重建后
    复核，若 cupsd 仍崩，则核弹级重置并二次重建一次，确保「修复后盒子可用」而非「修完仍崩」。"""
    from . import printers
    try:
        printers.main()
    except Exception as e:
        C.log("init", "WARN", f"打印机探测异常: {e}")
    if not _wait_cups_ready(timeout=10) or _cupsd_recently_crashed(45):
        C.log("init", "WARN", "重建队列后 cupsd 仍不稳定，核弹级重置并二次重建")
        _nuclear_reset_printers()
        try:
            printers.main()
        except Exception as e:
            C.log("init", "WARN", f"二次重建打印机异常: {e}")


def _stabilize_cups_for_rebuild():
    """repair/init 在重建打印机队列前，先把 CUPS 稳定在「无坏队列」的干净状态。

    步骤（等价于用户手工验证可用的 recipe，正是 0.2.40 升级后盒上仍失败、用户手工命令却一次成功的根因对照）：
      1) mask + stop cups，并等其彻底退出（必要时硬杀 cupsd）。mask 防止崩溃循环期间 systemd
         不断把 cupsd 重新拉起、把我们离线编辑的 printers.conf 又写崩；
      2) 离线【清空坏队列】：本系统托管的全部 _printbox 队列 + 任何 PPD 缺失/损坏/raw 的坏队列
         （无论名字是否带 _printbox，避免残留老队列在 cupsd 加载时崩溃，见 0.2.44 修复）；
      3) 离线 PPD 净化（把残留旧 wrapper 路径改写为 AppArmor 放行的 /usr/lib/cups/filter/...）；
      4) 放行 cupsd 读取 /etc/paperspecs；
      5) unmask + enable + start cups（用 start 而非 restart，确保从停稳状态干净起）；
      6) _wait_cups_ready 直到调度器真正就绪；
      7)【紧急兜底】若 cupsd 启动后仍不稳定（最近 30s 内又有 status=1/FAILURE，说明仍有残留坏队列
         导致崩溃），则 mask + 全量清场（摘除 printers.conf 全部打印机块 + 删除全部 PPD）后再次干净启动，
         确保 main() 有干净起点可重建。
    异常均内部兜底记录 WARN，不向上抛（修复流程不应因单步失败中断）。"""
    from . import printers
    # 0.5) 若 cupsd 当前已稳定运行（在跑 且 近 15s 内无崩溃），则【不拆除它】——
    # 直接返回，由后续 main() 在稳定 cupsd 上重建队列即可。否则 repair 会因「核弹级重置重启 cupsd」
    # 制造出一段 USB 枚举空窗，main() 的探测恰逢其会拿到空 → 漏建队列（0.2.45 升级后实测：
    # 用户手工已稳住 cupsd，repair 仍把刚稳定的 cupsd 又拆又重启，导致队列建不出来）。
    # 仅在确实不稳定（崩溃循环）时才走离线清场/核弹级重置。
    _cups_ok = "scheduler is running" in (_run(["lpstat", "-r"]).stdout or "")
    if _cups_ok and not _cupsd_recently_crashed(15):
        C.log("init", "INFO",
              "cupsd 当前已稳定运行，跳过离线清场与重启（避免重建竞争中 USB 枚举空窗导致漏建队列）")
        try:
            printers._sanitize_ppd_filter_paths()
        except Exception as e:
            C.log("init", "WARN", f"在线 PPD 净化失败（可忽略）: {e}")
        return
    # 0) 停服【前】先判定是否处于崩溃循环（journalctl 持久，停服不影响判断）。
    #    崩溃循环里 cupsd 每次重启后会有瞬间 "running"，_wait_cups_ready 易误判为已就绪、
    #    从而跳过紧急全量清场 → 损坏/raw 的 printers.conf 原样留下、继续崩（0.2.45 复盘真因）。
    #    因此一旦检测到崩溃循环，直接走核弹级整体移走 printers.conf（正则清场在损坏文件上不可靠）。
    was_crashing = _cupsd_recently_crashed(120)
    # 1) mask 防 systemd 在编辑期间把 cupsd 重新拉起、写崩 printers.conf
    _run(["systemctl", "mask", "cups"])
    _run(["systemctl", "stop", "cups"])
    _ensure_cups_stopped(timeout=20)
    if was_crashing:
        C.log("init", "WARN",
              "检测到 cupsd 崩溃循环，直接执行核弹级重置（整体移走 printers.conf，"
              "避免正则清场在损坏文件上落空导致继续崩）")
        _nuclear_reset_printers()
        return
    # 2) 离线清空坏队列（托管 + 任何 PPD 缺失/raw 的坏队列，不论名字）
    try:
        printers._wipe_managed_queues_offline()
    except Exception as e:
        C.log("init", "WARN", f"离线清空托管队列失败（可忽略）: {e}")
    # 3) 离线 PPD 净化
    try:
        printers._sanitize_ppd_filter_paths()
    except Exception as e:
        C.log("init", "WARN", f"离线 PPD 净化失败（可忽略）: {e}")
    # 4) 放行 cupsd 读取 /etc/paperspecs（AppArmor）
    try:
        _allow_cups_paperspecs()
    except Exception as e:
        C.log("init", "WARN", f"放行 cupsd 读取 /etc/paperspecs 失败（非致命）: {e}")
    # 5) 干净启动
    _run(["systemctl", "unmask", "cups"])
    _run(["systemctl", "enable", "cups"])
    _run(["systemctl", "start", "cups"])
    # 6) 等就绪；7) 任一不满足 → 紧急全量清场（结构化清场不够则核弹级移除 printers.conf）
    if not _wait_cups_ready(timeout=60) or _cupsd_recently_crashed():
        C.log("init", "WARN",
              "cupsd 启动后仍不稳定/检测到崩溃，执行紧急全量清场（含核弹级兜底）")
        _emergency_full_wipe_and_restart()


def _fix_cups_driverd():
    """移除会让 cups-driverd 崩溃的损坏 .defs 文件（打印机服务器无需盲文驱动）。

    Debian trixie 上 `printer-driver-indexbraille`（cups 的 Recommends，默认被自动安装）
    附带两个语法损坏的 .defs 文件（indexbraille-media.defs / indexbraille-filter.defs）。
    cups-driverd 每次解析它们都会崩溃，导致 cupsd 调度器反复
    "Scheduler shutting down due to program error"，打印作业因此在崩溃/重启间隙被不断
    重发（表现为"一直打印不停"）。本服务器不需要盲文打印机，直接 purge 并兜底删文件。"""
    try:
        if _run(["dpkg", "-s", "printer-driver-indexbraille"]).returncode == 0:
            _run(["apt-get", "remove", "--purge", "-y", "printer-driver-indexbraille"])
            C.log("init", "INFO", "已移除 printer-driver-indexbraille（其坏 .defs 会让 cups-driverd 崩溃）")
        for f in ("/usr/share/cups/drv/indexbraille-media.defs",
                  "/usr/share/cups/drv/indexbraille-filter.defs"):
            if os.path.exists(f):
                os.remove(f)
                C.log("init", "INFO", f"已删除损坏的 CUPS 驱动信息文件: {f}")
    except Exception as e:
        C.log("init", "WARN", f"清理 cups-driverd 崩溃源失败（可忽略）: {e}")


def _fix_cups_spool_and_backends():
    """修复 cupsd 启动期崩溃的两类本地根因（本盒 0.2.45→0.2.46 实测真凶，固化进代码防复发）：

    1) /var/spool/cups/tmp 等 CUPS 运行目录缺失/权限错：HP 私有后端 hpfax 启动即报
       `Failed to create /var/spool/cups/tmp/.hplip`，连同权限错会拖垮 cupsd 进入崩溃循环，
       进而让 lpinfo 枚举空、add_printer 兜底 raw、队列永远建不起来。
    2) HP 私有后端 hpfax/hp：本机是 foo2xqx USB 激光打印机，foo2xqx 只走标准 usb 后端，
       hpfax/hp 完全用不到，且启动即报错，直接停用（改名 .disabled 备份，幂等可还原）。

    均为幂等操作；只改本地状态，不影响已发布 AirPrint（后者由 avahi 服务文件负责）。"""
    # 1) 修 CUPS 运行目录权限（hpfax 报 .hplip 创建失败即此目录问题）
    dirs = [
        ("/var/spool/cups/tmp", 0o1770),
        ("/var/spool/cups", 0o0755),
        ("/var/cache/cups", 0o0755),
        ("/run/cups", 0o0755),
        ("/var/log/cups", 0o0700),
    ]
    for d, mode in dirs:
        try:
            os.makedirs(d, exist_ok=True)
            os.chmod(d, mode)
            _run(["chown", "-R", "root:lp", d])
        except Exception as e:
            C.log("init", "WARN", f"修复 CUPS 目录权限失败（可忽略）: {e}")
    # 2) 停用 HP 私有后端（本机 foo2xqx USB 激光机不需要；hpfax 启动即报错且可能拖垮 cupsd）
    for b in ("hpfax", "hp"):
        p = f"/usr/lib/cups/backend/{b}"
        disabled = p + ".disabled"
        if os.path.exists(p) and not os.path.exists(disabled):
            try:
                os.rename(p, disabled)
                C.log("init", "INFO", f"已停用 HP 私有后端 {b}（本机 foo2xqx USB 激光机不需要）")
            except Exception as e:
                C.log("init", "WARN", f"停用后端 {b} 失败（可忽略）: {e}")


def _is_printbox_stub(path):
    """判断某后端文件是否已是 PrintBox 写入的空操作 stub（避免把自家 stub 当成真实后端误改名）。"""
    try:
        with open(path, "r") as f:
            return "PrintBox" in f.read(512)
    except Exception:
        return False


def _disable_driverless_backend():
    """禁用 driverless / dnssd 后端，根治 cupsd 崩溃循环（driverless 坏行）。

    真因（0.2.33 升级后盒上 diag 坐实）：cups-driverd 在每次驱动枚举时都会调用 driverless 后端去
    扫局域网 Avahi，把我们【自己发布的 AirPrint 打印机】
    （ipp://hp_laserjet_p1505n_printbox._ipp._tcp.local/）当成“可被 driverless 驱动的 IPP 打印机”
    去解析，产出畸形行（Bad line from "driverless": "driverless:ipp://..."），cups-driverd 报错后
    cupsd 直接 “Scheduler shutting down due to program error” 每 ~15s 崩溃循环，所有作业 Filter failed
    （WebUI 测试页/手机提交都打不出）。

    关键认知：Browsing Off 只关掉 cupsd 自身的广播/浏览，但 driverless 扫描是 cups-driverd **独立**
    发起的、与 Browsing 开关无关——所以 0.2.33 仅靠 Browsing Off 没能止住崩溃，必须连后端一并禁用。

    本服务器用的是固定 USB 打印机 + 显式 foo2xqx PPD，从不走 driverless/网络发现自动加打印机，
    因此禁用这两个后端完全安全（不影响既有队列、不影响手机经我们发布的 AirPrint 直连打印）。

    双保险做法（幂等、可跨版本重复执行）：
      1) 把真实后端重命名为 .disabled 备份，并去掉其执行位——即便 cups-driverd 仍尝试运行该名
         文件，非执行位也令其被忽略，绝不会再扫网产出畸形行；
      2) 在原路径写入一个只 `exit 0` 的空操作 stub，使 cups-driverd 枚举该后端时拿到空输出，
         不再产出 'driverless:ipp://...' 畸形行 → 彻底不再触发 'Bad line from driverless' 崩溃。
    注意：0.2.34 曾把后端改名 .disabled 但未写 stub；本函数必须“无论是否已改名”都在原路径重建
    stub，否则升级到 0.2.35+ 后原路径 stub 缺失，diag 会误报“建议重跑 repair”。
    driverless 另含 -fax 伴随后端，一并覆盖。
    """
    import glob
    backends = {"driverless": ["driverless", "driverless-fax"], "dnssd": ["dnssd"]}
    disabled = 0
    for base, names in backends.items():
        for name in names:
            real = "/usr/lib/cups/backend/" + name
            bak = real + ".disabled"
            # 1) 真实后端仍在（且不是我们自己的 stub）→ 改名备份 + 去执行位
            if os.path.exists(real) and not _is_printbox_stub(real):
                try:
                    if os.path.exists(bak):
                        os.remove(real)          # 备份已存在，直接删原文件避免重复
                    else:
                        os.rename(real, bak)
                    try:
                        os.chmod(bak, 0o644)     # 双保险：去掉执行位
                    except Exception:
                        pass
                    C.log("init", "INFO", f"已禁用 {name} 后端（备份为 {bak}），根治 driverless 坏行崩溃")
                    disabled += 1
                except Exception as e:
                    C.log("init", "WARN", f"禁用 {name} 后端失败: {e}")
            # .disabled 备份若仍带执行位（旧版遗留），去掉以防被枚举扫网
            if os.path.exists(bak) and os.access(bak, os.X_OK):
                try:
                    os.chmod(bak, 0o644)
                except Exception:
                    pass
            # 2) 在原路径写空操作 stub（幂等）。即便上一步没动到原路径，这里也保证 stub 存在。
            try:
                with open(real, "w") as f:
                    f.write(
                        "#!/bin/bash\n"
                        "# PrintBox: 禁用 driverless/dnssd 后端（空操作），"
                        "避免 cups-driverd 产出畸形行导致 cupsd 崩溃\nexit 0\n"
                    )
                os.chmod(real, 0o755)
            except Exception as e:
                C.log("init", "WARN", f"写入 {name} 空操作 stub 失败（可忽略）: {e}")
    if disabled == 0:
        C.log("init", "INFO", "driverless/dnssd 后端已处于禁用状态（无需修改）")


def _ensure_cups_apparmor():
    """授予 cupsd AppArmor CAP_NET_ADMIN，根治『AppArmor 拒绝 net_admin 致 cupsd 启动即崩』循环。

    真因（<box> 盒 2026-10-29 坐实）：cupsd 启动期为设置广播/多播 socket 需要 CAP_NET_ADMIN，
    但 Debian trixie 自带 cupsd AppArmor profile 未授权该 capability，被拒绝
    （audit: apparmor="DENIED" operation="capable" capability=12 capname="net_admin"）后
    该操作失败，cupsd 当作致命错误退出（"Scheduler shutting down due to program error"），
    systemd ~15s 后重启 → 空闲态每 ~15s 崩溃循环。此根因与 driverless/Browsing 无关，
    故此前禁用 driverless 无效（driverless 畸形行计数恒为 0）。

    修复：向 cupsd 的 AppArmor 本地包含（/etc/apparmor.d/local/usr.sbin.cupsd，不被包更新覆盖）
    追加 `capability net_admin,` 并 reload profile。幂等。"""
    local = "/etc/apparmor.d/local/usr.sbin.cupsd"
    prof = "/etc/apparmor.d/usr.sbin.cupsd"
    need = "capability net_admin,"
    try:
        import shutil
        os.makedirs(os.path.dirname(local), exist_ok=True)
        cur = ""
        if os.path.exists(local):
            with open(local, "r", encoding="utf-8", errors="ignore") as f:
                cur = f.read()
        if need.strip() not in cur:
            with open(local, "a", encoding="utf-8") as f:
                f.write(
                    "\n# PrintBox: 授予 cupsd CAP_NET_ADMIN，修复 AppArmor 拒绝导致的 cupsd 启动崩溃循环\n"
                    + need + "\n"
                )
            C.log("init", "INFO", "已为 cupsd AppArmor 本地包含追加 capability net_admin")
        else:
            C.log("init", "INFO", "cupsd AppArmor 已含 net_admin 授权（无需修改）")
    except Exception as e:
        C.log("init", "WARN",
              f"写入 AppArmor 本地包含失败（可忽略，但 cupsd 可能仍崩）: {e}")
        return
    try:
        import shutil
        parser = shutil.which("apparmor_parser") or "/sbin/apparmor_parser"
        if os.path.exists(prof):
            r = _run([parser, "-r", prof])
            if r.returncode == 0:
                C.log("init", "INFO", "已重载 cupsd AppArmor profile")
            else:
                rr = _run(["systemctl", "restart", "apparmor"])
                if rr.returncode == 0:
                    C.log("init", "INFO", "已通过重启 apparmor 服务重载 profile")
                else:
                    C.log("init", "WARN",
                          "重载 cupsd AppArmor profile 失败，下次重启小主机将自动加载")
    except Exception as e:
        C.log("init", "WARN", f"重载 AppArmor profile 异常: {e}")


def _allow_cups_paperspecs():
    """消除 cupsd 读取 /etc/paperspecs 的 AppArmor DENIED（Debian trixie 默认未授权）。

    该拒绝本身非致命（cupsd 读到 EACCES 会回退默认纸张），但每 ~15s 刷一条审计日志，
    且可能让全局默认纸张停留在 Letter。通过 local include 放行读取并热重载 cupsd 配置档解决。
    纯 best-effort：任何一步失败都不影响 repair 主流程。"""
    local = "/etc/apparmor.d/local/usr.sbin.cupsd"
    rule = "/etc/paperspecs r,"
    try:
        d = os.path.dirname(local)
        if d:
            os.makedirs(d, exist_ok=True)
        need = True
        if os.path.exists(local):
            try:
                with open(local, errors="ignore") as f:
                    if rule in f.read():
                        need = False
            except Exception:
                pass
        if need:
            with open(local, "a") as f:
                f.write(
                    "\n# PrintBox: 放行 CUPS 读取纸张规格库（Debian trixie AppArmor 默认未授权）\n"
                    + rule + "\n"
                )
            C.log("init", "INFO", f"已为 cupsd AppArmor 放行 {rule}")
        # 热重载 cupsd 配置档（下次 cupsd 重启即生效；repair 末尾会 restart cups）
        _run(["apparmor_parser", "-r", "-W", "/etc/apparmor.d/usr.sbin.cupsd"])
    except Exception as e:
        C.log("init", "WARN", f"放行 cupsd 读取 /etc/paperspecs 失败（可忽略，非致命）: {e}")


def _stabilize_networking():
    """防止 network-manager 接管当前活动网卡，导致安装/运行期 IP 反复变化。

    背景：0.2.10 把 network-manager 纳入默认安装后，NM 的 postinst 会启动并默认接管所有
    网卡；在通过随身 WiFi / DHCP 联网的盒子上会触发 DHCP 重协商、IP 反复变化，进而把正在
    跑的 install / SSH 会话冲掉，导致 `printbox init` 没跑完（WebUI 起不来、手机搜不到打印机）。

    处理：
      - 写入 /etc/NetworkManager/conf.d/printbox-unmanaged.conf（managed=false），
        即便 NM 被启动也不会去碰任何网卡（双保险）；
      - 禁用并停止 NetworkManager 服务（仅保留 nmcli 二进制，供 WebUI 检测 / SSID 走 iw）。
    说明：本机联网此前并不依赖 NM（Debian 默认用 ifupdown / wpa_supplicant），停用 NM 接管
    后网卡回到原管理方式，IP 即稳定。SSID 检测走 iw（network.wifi_ssid 已有回退），不受影响。
    """
    try:
        conf_dir = "/etc/NetworkManager/conf.d"
        os.makedirs(conf_dir, exist_ok=True)
        with open(os.path.join(conf_dir, "printbox-unmanaged.conf"), "w", encoding="utf-8") as f:
            f.write("# PrintBox: 不让 NetworkManager 接管网卡，避免 IP 反复变化\n")
            f.write("[main]\n")
            f.write("managed=false\n")
        C.log("init", "INFO", "已写入 NetworkManager managed=false 配置")
    except Exception as e:
        C.log("init", "WARN", f"写入 NM 配置失败（可忽略）: {e}")
    try:
        _run(["systemctl", "disable", "--now", "NetworkManager"])
        C.log("init", "INFO", "已禁用 NetworkManager 服务（nmcli 仍在，SSID 走 iw）")
    except Exception as e:
        C.log("init", "WARN", f"禁用 NetworkManager 失败（可忽略，但 IP 可能变化）: {e}")


def _ensure_locale_and_fonts(install_fonts=True):
    """修复小主机命令行中文显示为 ◆（豆腐块/无字形）的问题。

    根因：盒子默认 locale 可能不是 UTF-8，或没有中文字体，中文输出在终端里被渲染成占位方块。
    处理：
      1) 设置系统 locale 为 C.UTF-8（glibc 内置、无需生成，最稳），确保中文以 UTF-8 输出；
      2) 安装中文字体 fonts-wqy-zenhei（最佳努力，失败不阻断），让需要本地渲染中文的场景有字形；
      3) 写入 /etc/default/locale、/etc/environment、/etc/profile.d，使登录 shell 即 UTF-8。
    注：若你是通过 SSH 客户端（如 Windows 终端）连接，终端自身也需启用支持中文的字体
    （如 Cascadia Code / Sarasa / 等宽黑体），否则仍是客户端字体问题，与服务端无关。
    """
    try:
        loc = "C.UTF-8"
        with open("/etc/default/locale", "w", encoding="utf-8") as f:
            f.write(f'LANG="{loc}"\n')
            f.write('LANGUAGE="C.UTF-8"\n')
            f.write('LC_ALL=\n')
        try:
            with open("/etc/environment", "r", encoding="utf-8") as f:
                env = f.read()
        except Exception:
            env = ""
        if "LANG=" not in env:
            with open("/etc/environment", "a", encoding="utf-8") as f:
                f.write(f'LANG="{loc}"\n')
        os.makedirs("/etc/profile.d", exist_ok=True)
        with open("/etc/profile.d/printbox-locale.sh", "w", encoding="utf-8") as f:
            f.write(f'export LANG="{loc}"\n')
            f.write('export LC_ALL=\n')
        C.log("init", "INFO", f"已设置系统 locale 为 {loc}（UTF-8，中文不再乱码）")
    except Exception as e:
        C.log("init", "WARN", f"设置 locale 失败（可忽略）: {e}")
    if install_fonts:
        try:
            _apt_install(["fonts-wqy-zenhei"])
            C.log("init", "INFO", "已安装中文字体 fonts-wqy-zenhei")
        except Exception as e:
            C.log("init", "WARN", f"安装中文字体失败（可忽略，SSH 客户端请用中文字体）: {e}")


def provision(skip_apt=False):
    C.ensure_dirs()
    # 先固定网络：写入 NM managed=false 配置，避免稍后安装 NM 时它接管网卡导致 IP 翻转
    _stabilize_networking()

    if not skip_apt:
        print("[1/5] 安装打印/网络依赖（需联网，可能耗时数分钟）...")
        _run(["apt-get", "update"])
        # 核心依赖：缺失会直接导致打印不可用，必须明确提示
        all_pkgs = CORE_APT + drivers.BASE_DRIVER_PKGS
        if not _apt_install(all_pkgs):
            print("  警告：部分核心依赖安装失败，打印功能可能不可用；"
                  "请检查网络与 apt 源后重试 `sudo printbox init`。", file=sys.stderr)
        # 可选依赖：更名/移除则跳过，不影响核心
        _apt_install(OPT_APT)
        # 依赖装完后再次固定网络：禁用 NM 服务（仅保留二进制供 WebUI 检测 / iw 走 SSID）
        _stabilize_networking()
        # 修复命令行中文显示为 ◆（locale + 字体）
        _ensure_locale_and_fonts()
    else:
        print("[跳过] apt 安装（--no-apt / repair 模式）")
        # repair 模式下也确保 locale/字体与网络稳定（locale 不依赖网络，字体不联网安装）
        _ensure_locale_and_fonts(install_fonts=False)
        _stabilize_networking()

    print("[2/5] 写入默认配置...")
    if not os.path.exists(C.CONFIG_FILE):
        C.save_config(C.DEFAULT_CONFIG)
        C.log("init", "INFO", "已写入默认配置 /etc/printbox/config.json")

    print("[3/5] 安装并启用 systemd 服务 + 打印机热插拔规则...")
    _install_units()
    _run(["systemctl", "daemon-reload"])

    print("[4/5] 稳定 CUPS 并探测打印机并自动安装驱动...")
    from . import printers
    # 先修掉 cups-driverd 的崩溃源（indexbraille 坏 .defs），否则 CUPS 调度器起不来，
    # 后续 lpinfo 枚举设备会返回空 → 探测不到打印机（全新安装尤易踩：CUPS 首次启动慢）。
    _fix_cups_driverd()
    # 修 cupsd 启动期崩溃的本地根因：spool 目录权限 + 停用 HP 私有后端（本盒实测真凶，0.2.46 固化）
    _fix_cups_spool_and_backends()
    # 禁用 driverless/dnssd 后端：否则 cups-driverd 会把我们自己的 AirPrint 打印机当成 driverless
    # 目标去解析，产出畸形行让 cupsd 每 ~15s 崩溃（0.2.33 升级后已坐实）。本机是固定 USB 打印机 +
    # 显式 PPD，不需要网络发现后端，禁用完全安全。
    _disable_driverless_backend()
    # 授权 cupsd AppArmor CAP_NET_ADMIN：根治 AppArmor 拒绝导致 cupsd 启动即崩的循环（0.2.48 固化）
    _ensure_cups_apparmor()
    # 停掉 cups-browsed：它是 IPP 订阅风暴（Create-Printer-Subscriptions 循环）的根因，
    # 会把 CUPS 调度器拖进 “Scheduler shutting down due to program error” 崩溃循环，
    # 表现为 lpadmin/lp 全部 Service Unavailable、打印机永远配不上驱动。
    # 本服务器用显式 Avahi AirPrint 服务文件发布打印机，不需要 cups-browsed，
    # 因此默认禁用它（仅在用户主动启用远端打印机浏览时才需要）。
    _run(["systemctl", "disable", "--now", "cups-browsed"])
    # 确保 CUPS 已启用并稳定（打印功能依赖；若未安装则跳过，Web 界面仍可访问）
    if _run(["dpkg", "-s", "cups"]).returncode == 0:
        _ensure_cupsd_limits()
        _ensure_cups_lan()
        # ★ 0.2.41：先把 CUPS 稳定在「无坏队列」的干净状态（停稳 cupsd → 离线清空托管队列 →
        #   干净重启），再重建打印机。等价于用户手工验证可用的 rm PPD + 清 printers.conf 队列块；
        #   0.2.40 仅「局部摘除坏队列」清不干净，cupsd 仍在崩溃循环、main() 永远没机会重建队列。
        _stabilize_cups_for_rebuild()
        # 确保 CUPS 留存作业原始文件（PreserveJobFiles=yes），否则打印后文件被清走、
        # 打印历史/归档抓不到原始内容。
        try:
            printers.enable_job_archive()
        except Exception as e:
            C.log("init", "WARN", f"开启 PreserveJobFiles 失败（可忽略）: {e}")
        if not _wait_cups_ready(timeout=10):
            C.log("init", "WARN", "CUPS 已安装但未能稳定启动，打印功能暂不可用；"
                  "请检查 `systemctl status cups` 与 /var/log/cups/error_log。")
    else:
        C.log("init", "WARN", "CUPS 未安装，打印功能不可用；Web 管理界面仍可访问。")
    # 现在 CUPS 已稳定，再探测打印机（lpinfo 才可靠）；重建后复核稳定性（核弹级兜底）
    _rebuild_printers_with_guard()

    print("[5/5] 启动服务 + 发布到局域网...")
    for s in ["printbox-web", "printbox-archive", "printbox-printer-hotplug"]:
        _run(["systemctl", "enable", s])
        _run(["systemctl", "restart", s])
    _run(["udevadm", "control", "--reload-rules"])
    _run(["udevadm", "trigger"])

    # 确保 avahi-daemon 运行，并把已配置打印机以 AirPrint/mDNS 形式重新发布到局域网
    # （手机/电脑通过 mDNS 发现打印机，依赖 avahi；cups-browsed 已被禁用，不再负责发布）。
    try:
        # 先加固 avahi 主机名配置（发布 <主机名>.local，避免长期后无法解析）
        _ensure_avahi_hostname()
        _publish_printbox_alias()
        printers.ensure_avahi()
        printers.prune_phantom_printers()
        printers.republish_airprint()
    except Exception as e:
        C.log("init", "WARN", f"发布打印机到局域网失败（可忽略，稍后可在 WebUI 重新发布）: {e}")

    # mDNS 重新宣告定时器：周期性 SIGHUP/重启 avahi，保持 <主机名>.local 长期可解析。
    # 规避“放置两天后 .local 打不开、但 IP 仍能访问”的过期失效问题。
    try:
        for u in EXTRA_UNITS:
            _run(["systemctl", "enable", u])
        # 定时器必须 start 才会立即生效（enable 只保证开机启动）。升级/修复后就要马上生效，
        # 不能等下次重启。mdns-renew 与 guard 两个定时器都在此启动；oneshot .service 不需要 start。
        for u in EXTRA_UNITS:
            if u.endswith(".timer"):
                _run(["systemctl", "start", u])
        C.log("init", "INFO", "已启用并启动 mDNS 重新宣告与打印洪流防护（guard）定时器")
    except Exception as e:
        C.log("init", "WARN", f"启用定时器失败（可忽略）: {e}")

    # 将已建立的打印机队列标记为「共享」，确保局域网客户端可提交作业
    # （配合上面的 cupsd.conf 局域网监听 + Allow from @LOCAL 才真正可打印）。
    try:
        for p in printers.list_printers():
            _run(["lpadmin", "-p", p, "-o", "printer-is-shared=true"])
            # 对 foo2xqx(host-based) 打印机补上 PDF 归一化补丁，修复移动端空白页
            try:
                printers.patch_foo2xqx_ppd(p)
            except Exception as e:
                C.log("init", "WARN", f"foo2xqx 补丁应用失败（可忽略）: {e}")
        C.log("init", "INFO", "已将已配置打印机标记为共享（printer-is-shared=true）")
    except Exception as e:
        C.log("init", "WARN", f"标记打印机共享失败（可忽略）: {e}")

    # WS-Discovery（WSD）：让 Windows / HarmonyOS 也能在「添加打印机」里自动看到本服务器。
    # 尽力而为：wsdd 未安装或不可用时不阻断安装，仅记录提示（可用 IPP 地址手动添加）。
    try:
        lan_ip = _lan_ip()
        ok, msg = discovery.write_wsdd_unit(lan_ip, printers.list_printers())
        C.log("init", "INFO" if ok else "WARN", f"WS-Discovery: {msg}")
    except Exception as e:
        C.log("init", "WARN", f"WS-Discovery 配置失败（可忽略）: {e}")

    host = _hostname()
    ip = _lan_ip()
    _publish_printbox_alias()
    print()
    print("==========================================")
    print("PrintBox 安装完成！")
    print("  管理界面（任选其一打开）:")
    if ip:
        print(f"    http://{ip}        （本机局域网 IP，最稳）")
    print(f"    http://{host}.local  （局域网主机名，需支持 mDNS 的设备）")
    print("    http://printbox.local （品牌域名别名，可能需等待数秒生效）")
    print("  常用命令:")
    print("    sudo printbox status   查看服务状态")
    print("    sudo printbox detect    重新探测/添加打印机")
    print("    sudo printbox restart   重启全部服务")
    print("  日志: /var/log/printbox/")
    print("==========================================")


def repair():
    """修复模式：不跑 apt（避免联网/安装中断），仅补齐配置、启动服务、发布到局域网。

    适用：上一次 `printbox init` 因网络抖动 / 安装中断没跑完，导致 WebUI 起不来、
    手机搜不到打印机、或中文显示为 ◆。先稳定网络（禁用 NM 接管）并设置 locale/字体，
    再重做服务与 mDNS 发布，把系统拉回可用状态。"""
    C.ensure_dirs()
    # 稳定网络 + 中文显示（repair 不触发 apt 字体安装，避免再次卡在网络）
    _stabilize_networking()
    _ensure_locale_and_fonts(install_fonts=False)
    print("[1/3] 写入/修正配置与服务单元...")
    if not os.path.exists(C.CONFIG_FILE):
        C.save_config(C.DEFAULT_CONFIG)
        C.log("init", "INFO", "已写入默认配置 /etc/printbox/config.json")
    _install_units()
    _run(["systemctl", "daemon-reload"])

    print("[2/3] 稳定 CUPS 并启动服务...")
    from . import printers
    _fix_cups_driverd()
    # 修 cupsd 启动期崩溃的本地根因：spool 目录权限 + 停用 HP 私有后端（本盒实测真凶，0.2.46 固化）
    _fix_cups_spool_and_backends()
    # 禁用 driverless/dnssd 后端（根治 driverless 坏行导致的 cupsd 崩溃循环；本机固定 USB 打印机 +
    # 显式 foo2xqx PPD，不需要网络发现后端，禁用安全）。
    _disable_driverless_backend()
    # 授权 cupsd AppArmor CAP_NET_ADMIN：根治 AppArmor 拒绝导致 cupsd 启动即崩的循环（0.2.48 固化）
    _ensure_cups_apparmor()
    _run(["systemctl", "disable", "--now", "cups-browsed"])
    if _run(["dpkg", "-s", "cups"]).returncode == 0:
        _ensure_cupsd_limits()
        _ensure_cups_lan()
        # ★ 0.2.41：先把 CUPS 稳定在「无坏队列」的干净状态（停稳 cupsd → 离线清空托管队列 →
        #   干净重启），再重建打印机。等价于用户手工验证可用的 rm PPD + 清 printers.conf 队列块；
        #   0.2.40 仅「局部摘除坏队列」清不干净，cupsd 仍在崩溃循环、main() 永远没机会重建队列。
        _stabilize_cups_for_rebuild()
        try:
            printers.enable_job_archive()
        except Exception as e:
            C.log("init", "WARN", f"开启 PreserveJobFiles 失败: {e}")
        if not _wait_cups_ready(timeout=10):
            print("  [警告] CUPS 调度器未能就绪，打印功能暂不可用；"
                  "请检查 `systemctl status cups` 与 /var/log/cups/error_log")
    else:
        C.log("init", "WARN", "CUPS 未安装，打印功能不可用；Web 管理界面仍可访问。")
    for s in ["printbox-web", "printbox-archive", "printbox-printer-hotplug"]:
        _run(["systemctl", "enable", s])
        _run(["systemctl", "restart", s])
    _run(["udevadm", "control", "--reload-rules"])
    _run(["udevadm", "trigger"])

    print("[3/3] 发布打印机到局域网（mDNS / AirPrint / WS-Discovery）...")
    # 升级/修复场景下，先重新探测并修复打印机队列：若既有队列因历史 bug 落成 raw/损坏，
    # 必须重跑 main()（force 重建）才能真正修好「打不出」；仅 prune 无法重建坏队列。
    # main() 内部已含 prune_phantom_printers（收敛双队列）+ 重发布 avahi，这里再兜底一次。
    # 重建后复核 cupsd 稳定性（核弹级兜底），确保修复后盒子真正可用。
    _rebuild_printers_with_guard()
    try:
        _ensure_avahi_hostname()
        _publish_printbox_alias()
        printers.ensure_avahi()
        printers.prune_phantom_printers()
        printers.republish_airprint()
    except Exception as e:
        C.log("init", "WARN", f"发布打印机失败: {e}")
    # 修复后自检：确认队列为真实驱动且已发布 AirPrint（否则 iOS/鸿蒙/Win 将搜不到/打不出）。
    # 一旦仍有 raw/缺 PPD 或缺 AirPrint 服务文件，明确记录 ERROR，交给 diag 复核，不再静默放过
    # （0.2.40 正是因「发布阶段静默跳过坏队列」导致 AirPrint 服务文件为空、手机端全部失效）。
    try:
        for q in printers.list_printers():
            ppd = printers._ppd_path(q)
            svc = f"/etc/avahi/services/airprint-{q}.service"
            if not os.path.exists(ppd) or printers.is_raw_printer(q):
                C.log("init", "ERROR",
                      f"修复后队列 {q} 仍为 raw/缺 PPD（{ppd}），iOS/鸿蒙/Win 将打不出；"
                      f"请重跑 printbox repair 或贴回 diag 日志")
            elif not os.path.exists(svc):
                C.log("init", "ERROR",
                      f"修复后队列 {q} 缺少 AirPrint 服务文件 {svc}，手机将搜不到；"
                      f"请运行 printbox republish 或贴回 diag 日志")
            else:
                C.log("init", "INFO", f"修复后校验通过：队列 {q} 为真实驱动且已发布 AirPrint")
    except Exception as e:
        C.log("init", "WARN", f"修复后自检异常（可忽略）: {e}")
    try:
        for u in EXTRA_UNITS:
            _run(["systemctl", "enable", u])
        # 定时器必须 start 才会立即生效（enable 只保证开机启动）。升级/修复后就要马上生效，
        # 不能等下次重启。mdns-renew 与 guard 两个定时器都在此启动。
        for u in EXTRA_UNITS:
            if u.endswith(".timer"):
                _run(["systemctl", "start", u])
        C.log("init", "INFO", "已启用并启动 mDNS 重新宣告与打印洪流防护（guard）定时器")
    except Exception as e:
        C.log("init", "WARN", f"启用定时器失败: {e}")
    try:
        for p in printers.list_printers():
            _run(["lpadmin", "-p", p, "-o", "printer-is-shared=true"])
            # 对 foo2xqx(host-based) 打印机补上 PDF 归一化补丁，修复移动端空白页
            try:
                printers.patch_foo2xqx_ppd(p)
            except Exception as e:
                C.log("init", "WARN", f"foo2xqx 补丁应用失败（可忽略）: {e}")
        C.log("init", "INFO", "已将已配置打印机标记为共享")
    except Exception as e:
        C.log("init", "WARN", f"标记打印机共享失败: {e}")
    try:
        lan_ip = _lan_ip()
        ok, msg = discovery.write_wsdd_unit(lan_ip, printers.list_printers())
        C.log("init", "INFO" if ok else "WARN", f"WS-Discovery: {msg}")
    except Exception as e:
        C.log("init", "WARN", f"WS-Discovery 配置失败: {e}")

    print("修复完成。请用本机 IP 或 <主机名>.local 打开 WebUI；手机应能在数秒内发现打印机。")
    print("若 IP 在修复过程中仍有波动，重启一次小主机即可彻底稳定（ifupdown 会干净接管网卡）。")
