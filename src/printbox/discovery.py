"""局域网打印发现：AirPrint(mDNS) + WS-Discovery(WSD)。

设计目标：让 iOS/macOS/Android/HarmonyOS/Linux 通过 mDNS 发现（AirPrint/IPP），
让 Windows 通过 WS-Discovery 自动看到设备；并为所有系统提供统一的 IPP 打印地址兜底。

所有动作都“尽力而为、可诊断”：发布失败不会让安装崩溃，而是把证据（journalctl、文件内容）
写进 PrintBox 日志，方便远程定位。
"""
import os
import time
import shutil
import subprocess

from . import config as C
from . import printers as PS


def _run(cmd):
    return C.run(cmd)


def _lan_ip():
    """返回局域网 IPv4（优先非 lo、非 docker 接口）。"""
    try:
        out = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show", "scope", "global"],
            text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            # 形如: 2: wlx...  inet 192.168.1.10/24 ...
            parts = line.split()
            for i, p in enumerate(parts):
                if p == "inet":
                    addr = parts[i + 1].split("/")[0]
                    if addr != "127.0.0.1":
                        return addr
    except Exception:
        pass
    return ""


def _wsdd_binary():
    for b in ("wsdd", "wsdd-server", "wsdd2"):
        p = shutil.which(b)
        if p:
            return p
    return ""


def _wsdd_supports_printer(binary):
    """探测 wsdd 是否支持 --device-type printer（christgau/wsdd 支持，部分发行版改名/精简）。"""
    try:
        out = subprocess.run([binary, "--help"], text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=8)
        return "printer" in out.stdout.lower()
    except Exception:
        return False


def _wsdd_has_flag(binary, flag):
    """探测 wsdd 是否支持某命令行开关（兼容 v1 的 --host-name 与 v2 的 --hostname）。"""
    try:
        out = subprocess.run([binary, "--help"], text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=8)
        return flag in out.stdout
    except Exception:
        return False


def _sh_q(s):
    """systemd ExecStart 按空白分词且支持引号；把含空格的参数用双引号包住，避免被拆开。"""
    s = str(s)
    if any(c in s for c in " \t\"'"):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def write_wsdd_unit(lan_ip, printer_names, allow_install=True):
    """生成并启用 wsdd systemd 单元，让 Windows/HarmonyOS 通过 WSD 自动发现打印服务器。

    返回 (ok, msg)。allow_install=True 时若 wsdd 未安装会尝试 apt 补装一次
    （init 阶段用；WebUI 重新发布时传 False，避免每次都触发 apt）。
    """
    binary = _wsdd_binary()
    if not binary and allow_install:
        # 最佳努力补装：不同发行版包名不同
        # Debian 13(trixie)=wsdd2；Ubuntu 24.04=wsdd-server；更旧的 Debian/Ubuntu=wsdd
        _run(["apt-get", "update"])
        for pkg in ("wsdd2", "wsdd-server", "wsdd"):
            _run(["apt-get", "install", "-y", pkg])
            binary = _wsdd_binary()
            if binary:
                break
    if not binary:
        return False, "未安装 wsdd（Windows 自动发现将不可用；可用 IPP 地址手动添加）"
    try:
        iface = _primary_iface()
        # wsdd 不认多个打印机队列，统一以“打印服务器”身份广播；
        # 真正的多队列由每台打印机的 AirPrint(mDNS) 提供，Windows 走 IPP 地址打印。
        location = f"http://{lan_ip}:631/printers/" if lan_ip else "http://printbox.local:631/"
        variant = "wsdd2" if os.path.basename(binary) == "wsdd2" else "wsdd"
        args = [binary]
        if variant == "wsdd2":
            # wsdd2（Debian trixie，C 实现，man wsdd2.8）：
            #   -i 指定接口，-H 指定主机名，-b 提供 WSD 元数据（无 --device-type 概念）。
            if iface:
                args += ["-i", iface]
            args += ["-H", "printbox"]
            # wsdd2 的 -b 解析器对每段只按【第一个冒号】切分 key:value，
            # 且 value 内不允许再出现冒号，否则报 Bad key:val（见 man wsdd2.8）。
            # URL 必含 "://"，因此 presentationurl 这类字段在 wsdd2 下无法承载；
            # 它仅影响 WSD 属性查询回显，对“被发现”非必需，故省略。
            # 同理 model 值的空格没问题（无冒号即可）。
            meta = (f"vendor:PrintBox,model:PrintBox Print Server,"
                    f"serial:printbox-server")
            args += ["-b", meta]
        else:
            # christgau/wsdd（wsdd / wsdd-server）：支持 --device-type printer，
            # 让 Windows 以“打印机”身份发现本机。
            dev_type = "printer" if _wsdd_supports_printer(binary) else "computer"
            if _wsdd_has_flag(binary, "--hostname"):
                args += ["--hostname", "printbox"]
            else:
                args += ["--host-name", "printbox"]
            args += [
                "--display-name", "PrintBox 打印服务器",
                "--device-type", dev_type,
                "--model", "PrintBox Print Server",
                "--vendor", "PrintBox",
                "--serial", "printbox-server",
                "--location", location,
            ]
            if iface:
                args += ["--interface", iface]
        # systemd ExecStart 不展开 $(...)、也不走 shell，必须把全部参数内联、含空格者加引号
        cmd = " ".join(_sh_q(a) for a in args)
        unit = f"""[Unit]
Description=PrintBox WS-Discovery (Windows/HarmonyOS 打印机发现)
After=network-online.target avahi-daemon.service
Wants=network-online.target

[Service]
Type=simple
ExecStart={cmd}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
"""
        upath = "/etc/systemd/system/printbox-wsdd.service"
        with open(upath, "w") as f:
            f.write(unit)
        _run(["systemctl", "daemon-reload"])
        _run(["systemctl", "enable", "printbox-wsdd"])
        rc, _, err = _run(["systemctl", "restart", "printbox-wsdd"])
        if rc != 0:
            return False, f"wsdd 启动失败: {err}"
        note = "（wsdd2：Windows 在网络中可见本机，打印机走 IPP 地址添加）" if variant == "wsdd2" else ""
        C.log("discovery", "INFO", f"已启用 WS-Discovery（{variant}）{note}")
        return True, f"WS-Discovery 已启用 ({variant})"
    except Exception as e:
        return False, f"配置 WS-Discovery 失败: {e}"


def _primary_iface():
    try:
        out = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show", "scope", "global"],
            text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                return parts[1]  # 接口名，如 wlx00117fbf424d
    except Exception:
        pass
    return ""


def verify_airprint(timeout=6):
    """自检：本机 avahi-browse 能否看到 AirPrint(_universal._sub._ipp._tcp) 服务。
    返回 (seen:bool, detail:str)。"""
    end = time.time() + timeout
    while time.time() < end:
        rc, out, _ = _run(["avahi-browse", "-rt", "_universal._sub._ipp._tcp"])
        if rc == 0 and ("HP" in out or "LaserJet" in out or "p1505" in out
                        or "universal" in out or "ipp._tcp" in out):
            return True, out.strip()[:500]
        time.sleep(1)
    return False, (out or "").strip()[:500]


_DIAG_CACHE = {"ts": 0.0, "data": None}


def diagnostics():
    """汇总发现状态，供 WebUI / 排错使用。带 8 秒缓存，避免每次打开页面都跑 5 秒 avahi-browse。"""
    global _DIAG_CACHE
    now = time.time()
    if _DIAG_CACHE["data"] is not None and now - _DIAG_CACHE["ts"] < 8:
        return _DIAG_CACHE["data"]
    lan_ip = _lan_ip()
    # 复用 printers.airprint_visible()（含缓存、双类型查询），仅多取一次输出用于展示
    airprint_seen = PS.airprint_visible()
    rc, browse, _ = _run(["avahi-browse", "-rt", "_universal._sub._ipp._tcp"])
    browse = browse or ""
    wsdd_bin = _wsdd_binary()
    rc2, wsd, _ = _run(["systemctl", "is-active", "printbox-wsdd"])
    wsdd_active = (rc2 == 0)
    printers = PS.list_printers()
    ipp_examples = [f"http://{lan_ip}:631/printers/{n}" for n in printers] if lan_ip else []
    data = {
        "lan_ip": lan_ip,
        "airprint_seen": airprint_seen,
        "airprint_browse": browse.strip()[:800],
        "wsdd_binary": wsdd_bin,
        "wsdd_active": wsdd_active,
        "printers": printers,
        "ipp_examples": ipp_examples,
        "web_url": f"http://{lan_ip}" if lan_ip else "http://<本机IP>",
    }
    _DIAG_CACHE = {"ts": now, "data": data}
    return data


def republish_all():
    """重新发布所有发现协议：AirPrint(mDNS) + WS-Discovery。返回汇总信息。"""
    summary = []
    try:
        PS.republish_airprint()
        seen, detail = verify_airprint()
        if seen:
            summary.append("AirPrint(mDNS) 发布成功，本机已能发现打印机")
        else:
            # 注册不上：把 avahi 日志与文件内容写进 PrintBox 日志，便于远程诊断
            rc, jl, _ = _run(["journalctl", "-u", "avahi-daemon", "-n", "50", "--no-pager"])
            files = ""
            sdir = "/etc/avahi/services"
            if os.path.isdir(sdir):
                for fn in os.listdir(sdir):
                    if fn.endswith(".service"):
                        try:
                            with open(os.path.join(sdir, fn)) as f:
                                files += f"\n--- {fn} ---\n" + f.read()
                        except Exception:
                            pass
            C.log("discovery", "WARN",
                  "AirPrint 自检未发现服务，可能 avahi 未加载服务文件。\n"
                  f"[journalctl avahi]\n{jl}\n[service files]{files}")
            summary.append("AirPrint 自检未通过，已将诊断写入日志（见 /var/log/printbox/）")
    except Exception as e:
        summary.append(f"AirPrint 发布异常: {e}")

    try:
        lan_ip = _lan_ip()
        ok, msg = write_wsdd_unit(lan_ip, PS.list_printers(), allow_install=False)
        summary.append(("WS-Discovery: " + msg) if ok else ("WS-Discovery 跳过: " + msg))
    except Exception as e:
        summary.append(f"WS-Discovery 异常: {e}")
    return summary
