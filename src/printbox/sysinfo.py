"""PrintBox 系统信息（只读展示 + 受控动作）。

提供：硬件用量（CPU/内存/磁盘/温度/负载/开机时长）、网络信息、WiFi 扫描、
系统更新（apt）与 PrintBox 自更新。所有动作均为管理员在 WebUI 显式触发，
且尽力而为、不依赖特定发行版（缺工具则降级提示，不抛异常）。
"""
import os
import re
import time
import subprocess

from . import config as C
from . import __version__

# ---- 模块级状态：后台升级进程跟踪（Web 服务常驻，跨请求有效）----
_UPDATE_PROC = None
_PO_UPDATE_PROC = None
_UPDATE_LOG = os.path.join(C.LOG_DIR, "system.log")


# ---------- 只读信息 ----------
def _read(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().strip()
    except Exception:
        return default


def _run(cmd, timeout=10):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception as e:
        return -1, "", str(e)


def cpu_percent(sample=0.3):
    """两次采样 /proc/stat 计算 CPU 占用百分比（省去对 top 的依赖）。"""
    def _stat():
        s = _read("/proc/stat")
        m = re.search(r"^cpu\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)",
                      s, re.M)
        if not m:
            return None
        vals = list(map(int, m.groups()))
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
        return idle, total
    a = _stat()
    if not a:
        return None
    time.sleep(sample)
    b = _stat()
    if not b:
        return None
    idle_d = b[0] - a[0]
    total_d = b[1] - a[1]
    if total_d <= 0:
        return 0.0
    return round((1 - idle_d / total_d) * 100, 1)


def mem_usage():
    info = _read("/proc/meminfo")
    d = {}
    for line in info.splitlines():
        m = re.match(r"(\w+):\s+(\d+)\s*kB", line)
        if m:
            d[m.group(1)] = int(m.group(2))
    total = d.get("MemTotal", 0)
    avail = d.get("MemAvailable", d.get("MemFree", 0))
    if total <= 0:
        return {"total_mb": 0, "used_mb": 0, "pct": 0}
    used = total - avail
    return {
        "total_mb": total // 1024,
        "used_mb": used // 1024,
        "pct": round(used / total * 100, 1),
    }


def disk_usage():
    try:
        st = os.statvfs("/")
        total = st.f_frsize * st.f_blocks
        free = st.f_frsize * st.f_bfree
        used = total - free
        return {
            "total_gb": round(total / (1024 ** 3), 1),
            "used_gb": round(used / (1024 ** 3), 1),
            "pct": round(used / total * 100, 1) if total else 0,
        }
    except Exception:
        return {"total_gb": 0, "used_gb": 0, "pct": 0}


def _disk_name_from_dev(dev):
    """从分区设备名推导磁盘名：/dev/sda1->sda, /dev/nvme0n1p2->nvme0n1, /dev/mmcblk0p2->mmcblk0。"""
    if not dev.startswith("/dev/"):
        return ""
    name = dev[len("/dev/"):]
    m = re.match(r"^(nvme\d+n\d+|mmcblk\d+)", name)  # nvme / mmc 带 pN 分区
    if m:
        return m.group(1)
    m = re.match(r"^(\D+\d*)", name)                 # sdX / hdX / vdX / xvdX
    if m:
        base = m.group(1)
        return re.sub(r"\d+$", "", base)             # 去掉分区号
    return name


def disk_model():
    """尽力返回根文件系统所在磁盘的型号与容量（GB）。"""
    dev = ""
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "/":
                    dev = parts[0]
                    break
    except Exception:
        return {"model": "", "size_gb": 0, "dev": ""}
    disk = _disk_name_from_dev(dev)
    model = ""
    size_gb = 0.0
    if disk:
        model_path = f"/sys/block/{disk}/device/model"
        size_path = f"/sys/block/{disk}/size"
        try:
            with open(model_path) as f:
                model = f.read().strip()
        except Exception:
            model = ""
        try:
            with open(size_path) as f:
                sectors = int(f.read().strip() or 0)
            size_gb = round(sectors * 512 / (1024 ** 3), 1)
        except Exception:
            size_gb = 0.0
    return {"model": model, "size_gb": size_gb, "dev": dev}


def list_processes(top_n=8):
    """返回 CPU 占用最高的若干系统进程 [{pid,comm,cpu,mem,time}]。"""
    rc, out, _ = _run(["ps", "-eo", "pid,comm,pcpu,pmem,etime", "--sort=-pcpu"],
                      timeout=5)
    rows = []
    if rc == 0:
        for line in out.splitlines()[1:]:
            parts = line.split(None, 4)
            if len(parts) >= 5:
                rows.append({"pid": parts[0], "comm": parts[1], "cpu": parts[2],
                             "mem": parts[3], "time": parts[4]})
    else:
        # 回退：解析 /proc 估算（按 RSS 排序取前 N）
        try:
            procs = []
            for pid in os.listdir("/proc"):
                if not pid.isdigit():
                    continue
                try:
                    with open(f"/proc/{pid}/stat") as f:
                        stat = f.read()
                    with open(f"/proc/{pid}/comm") as f:
                        comm = f.read().strip()
                    rss = 0
                    with open(f"/proc/{pid}/statm") as f:
                        rss = int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
                    procs.append({"pid": pid, "comm": comm, "cpu": "?",
                                  "mem": f"{round(rss/1024/1024,1)}", "time": "?"})
                except Exception:
                    continue
            procs.sort(key=lambda x: float(x["mem"] or 0), reverse=True)
            rows = procs[:top_n]
        except Exception:
            rows = []
    return rows[:top_n]


def logged_in_users():
    """返回当前登录用户 [{user,term,host,method,when}]；method=SSH/本地终端/本地图形/远程。"""
    rc, out, _ = _run(["who"], timeout=5)
    rows = []
    if rc != 0:
        return rows
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        user, term = parts[0], parts[1]
        host = ""
        m = re.search(r"\(([^)]*)\)", line)
        if m:
            host = m.group(1)
        # 登录方式判定
        if term.startswith("pts/") or term.startswith("tty") is False and host and \
                re.match(r"[\w.-]+", host) and host not in (":0", "(:0)"):
            method = "SSH" if re.match(r"\d{1,3}(\.\d{1,3}){3}|[\w.-]+", host) else "远程"
        elif term.startswith("tty"):
            method = "本地终端"
        elif host in (":0", "(:0)") or term.startswith(":"):
            method = "本地图形"
        else:
            method = "远程" if host else "本地"
        # 登录时间（去掉括号里的来源，仅取日期+时间部分）
        when = ""
        if len(parts) >= 4 and host:
            when = " ".join(parts[2:4])
        rows.append({"user": user, "term": term, "host": host,
                     "method": method, "when": when})
    return rows


def temperature():
    """尽力读取 SoC/CPU 温度（°C）。"""
    v = _read("/sys/class/thermal/thermal_zone0/temp")
    if v and v.isdigit():
        return round(int(v) / 1000, 1)
    rc, out, _ = _run(["vcgencmd", "measure_temp"], timeout=3)
    if rc == 0:
        m = re.search(r"(\d+\.?\d*)", out)
        if m:
            return float(m.group(1))
    return None


def load_uptime():
    up = _read("/proc/uptime").split()
    uptime_s = float(up[0]) if up else 0.0
    load = _read("/proc/loadavg").split()
    days = int(uptime_s // 86400)
    hh = int((uptime_s % 86400) // 3600)
    mm = int((uptime_s % 3600) // 60)
    uptime = f"{days}天 {hh}时 {mm}分" if days else f"{hh}时 {mm}分"
    return {
        "uptime": uptime,
        "load1": load[0] if len(load) > 0 else "?",
        "load5": load[1] if len(load) > 1 else "?",
        "load15": load[2] if len(load) > 2 else "?",
    }


def hardware_usage():
    return {
        "cpu": cpu_percent(),
        "mem": mem_usage(),
        "disk": disk_usage(),
        "temp": temperature(),
        **load_uptime(),
    }


def network_info():
    """网络信息：接口/IP、主机名、网关、WiFi SSID。"""
    from . import network as NS
    ns = NS.status()
    gw = ""
    rc, out, _ = _run(["ip", "-j", "route"])
    if rc == 0:
        try:
            import json
            for r in json.loads(out):
                if r.get("dst") == "default":
                    gw = r.get("gateway", "")
        except Exception:
            pass
    ns["gateway"] = gw
    return ns


def _wifi_iface():
    """尽力返回无线网卡接口名（wlan0 / wlp2s0 等）；无则空串。"""
    try:
        for d in os.listdir("/sys/class/net"):
            if os.path.exists(os.path.join("/sys/class/net", d, "wireless")):
                return d
    except Exception:
        pass
    rc, out, _ = _run(["iw", "dev"], timeout=5)
    if rc == 0:
        m = re.search(r"Interface\s+(\S+)", out)
        if m:
            return m.group(1)
    return ""


def _dbm_to_pct(dbm_str):
    """把 iw 的 dBm 粗略换算成 0-100 的信号百分比，便于与 nmcli 显示一致。"""
    try:
        dbm = float(dbm_str)
    except (ValueError, TypeError):
        return ""
    pct = int(2 * (dbm + 100))
    return str(max(0, min(100, pct)))


_WIFI_SCAN_CACHE = {"ts": 0.0, "val": None}


def wifi_scan():
    """尽力扫描附近 WiFi（只读，列出 SSID/信号/加密）。无无线/无工具则返回空。

    探测顺序：
      1) nmcli dev wifi list（最常用，信号为百分比）；
      2) iw dev <iface> scan（现代通用工具，nmcli 失败时兜底）；
      3) iwlist scan（老式兜底）。

    带 15 秒缓存：扫描较重（nmcli 8s / iw 12s 超时），避免系统页加载与 30s 轮询反复触发，
    显著改善 WebUI 切换/刷新卡顿（见反馈 #13）。
    """
    global _WIFI_SCAN_CACHE
    now = time.time()
    if _WIFI_SCAN_CACHE["val"] is not None and now - _WIFI_SCAN_CACHE["ts"] < 15:
        return _WIFI_SCAN_CACHE["val"]
    # 1) NetworkManager
    rc, out, _ = _run(["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY",
                      "dev", "wifi", "list"], timeout=8)
    if rc == 0 and out.strip():
        rows = []
        for line in out.splitlines():
            parts = line.split(":", 2)
            if len(parts) >= 2 and parts[0]:
                rows.append({
                    "ssid": parts[0],
                    "signal": parts[1],
                    "sec": parts[2] if len(parts) > 2 else "",
                })
        seen = {}
        for r in rows:
            seen.setdefault(r["ssid"], r)
        _WIFI_SCAN_CACHE = {"ts": time.time(), "val": list(seen.values())}
        return list(seen.values())
    # 2) iw dev <iface> scan（nmcli 不可用或返回空时的现代兜底）
    iface = _wifi_iface()
    if iface:
        rc, out, _ = _run(["iw", "dev", iface, "scan"], timeout=12)
        if rc == 0 and out.strip():
            rows = []
            # 按 BSS 块切分，逐块取 SSID / 信号 / 加密
            blocks = re.split(r"(?=^\s*BSS )", out, flags=re.M)
            for b in blocks:
                m = re.search(r"SSID:\s*(.*)", b)
                if not m or not m.group(1).strip():
                    continue
                ssid = m.group(1).strip()
                sig = re.search(r"signal:\s*([-\d.]+)", b)
                signal = _dbm_to_pct(sig.group(1)) if sig else ""
                if "RSN:" in b:
                    sec = "WPA2/WPA3"
                elif "WPA:" in b:
                    sec = "WPA"
                elif "WEP:" in b:
                    sec = "WEP"
                else:
                    sec = "open"
                rows.append({"ssid": ssid, "signal": signal, "sec": sec})
            seen = {}
            for r in rows:
                seen.setdefault(r["ssid"], r)
            _WIFI_SCAN_CACHE = {"ts": time.time(), "val": list(seen.values())}
            return list(seen.values())
    # 3) iwlist（老式兜底）
    rc, out, _ = _run(["iwlist", "scan"], timeout=10)
    if rc == 0:
        ssids = re.findall(r'ESSID:"([^"]*)"', out)
        res = [{"ssid": s, "signal": "", "sec": ""}
               for s in dict.fromkeys(ssids) if s]
        _WIFI_SCAN_CACHE = {"ts": time.time(), "val": res}
        return res
    res = []
    _WIFI_SCAN_CACHE = {"ts": time.time(), "val": res}
    return res


# ---------- 动作（受控、后台、尽力而为） ----------
def _spawn(shell_cmd):
    C.ensure_dirs()
    with open(_UPDATE_LOG, "a") as f:
        f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} 执行: {shell_cmd} ===\n")
    return subprocess.Popen(["bash", "-c", shell_cmd],
                            stdout=open(_UPDATE_LOG, "a"),
                            stderr=subprocess.STDOUT)


def start_system_update():
    """后台执行 apt-get update + upgrade；返回是否成功启动。"""
    global _UPDATE_PROC
    if _UPDATE_PROC is not None and _UPDATE_PROC.poll() is None:
        return False
    _UPDATE_PROC = _spawn("apt-get update -y && apt-get upgrade -y")
    return True


def start_printbox_update():
    """后台升级 PrintBox：优先用 venv 的 pip，否则系统 pip。"""
    global _PO_UPDATE_PROC
    if _PO_UPDATE_PROC is not None and _PO_UPDATE_PROC.poll() is None:
        return False
    venv_pip = "/opt/printbox/venv/bin/pip"
    if os.path.exists(venv_pip):
        cmd = f"{venv_pip} install --no-cache-dir --upgrade printbox"
    else:
        cmd = "pip install --no-cache-dir --upgrade printbox"
    _PO_UPDATE_PROC = _spawn(cmd)
    return True


def update_status():
    def _st(proc):
        if proc is None:
            return "idle"
        return "running" if proc.poll() is None else "done"
    tail = ""
    try:
        with open(_UPDATE_LOG, "r", errors="ignore") as f:
            tail = "".join(f.readlines()[-25:])
    except Exception:
        pass
    return {
        "system": _st(_UPDATE_PROC),
        "printbox": _st(_PO_UPDATE_PROC),
        "log_tail": tail,
    }


def apt_upgradable():
    rc, out, _ = _run(["apt-get", "-s", "upgrade"], timeout=20)
    if rc == 0:
        m = re.search(r"(\d+)\s+upgraded", out)
        n = int(m.group(1)) if m else 0
        pkgs = re.findall(r"^Inst\s+(\S+)", out, re.M)
        return n, pkgs[:50]
    return 0, []


def version():
    return __version__
