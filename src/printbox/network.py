"""PrintBox 网络状态查询（只读，后端无关）。

尽量不依赖特定网络后端（NetworkManager / wpa_supplicant / systemd-networkd），
统一通过 `ip` / `iw` / `ss` / `/proc` / `/sys` 读取真实状态；仅在需要连接/切换 WiFi
时才用到 nmcli（见 wifi.py）。这样即便本机用 wpa_supplicant 手动接好了 WiFi，
“网络”页也能正确显示已连接状态与网卡参数，不再误报“(未连接)”。
"""
import json
import os
import re
import subprocess
import time


def _run(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def hostname():
    r = _run(["hostname"])
    return r.stdout.strip() if r and r.returncode == 0 else ""


def _is_wireless(name):
    if re.match(r"^(wlan|wlp|wlo|wifi|wifi\d+)", name or ""):
        return True
    for p in (f"/sys/class/net/{name}/wireless",
              f"/sys/class/net/{name}/phy80211"):
        if os.path.exists(p):
            return True
    return False


def _links():
    r = _run(["ip", "-j", "link"])
    if not r or r.returncode != 0:
        return []
    try:
        return json.loads(r.stdout)
    except Exception:
        return []


def _addrs():
    r = _run(["ip", "-j", "addr"])
    if not r or r.returncode != 0:
        return {}
    try:
        data = json.loads(r.stdout)
    except Exception:
        return {}
    out = {}
    for ifc in data:
        name = ifc.get("ifname")
        ips = []
        for a in ifc.get("addr_info", []):
            if a.get("family") in ("inet", "inet6"):
                local = a.get("local", "")
                if not local.startswith("fe80"):   # 过滤链路本地 IPv6
                    ips.append(local)
        out[name] = ips
    return out


def interfaces(include_down=True):
    """返回所有非回环接口（含未插线/未启用的，便于排障与网络设置）。
    [{name, type:'ether'/'wifi', state, mac, addresses}]。
    设 include_down=False 时仅保留已启用或有 IP 的接口（旧行为）。"""
    links = _links()
    addrs = _addrs()
    out = []
    for l in links:
        name = l.get("ifname")
        if name == "lo":
            continue
        flags = " ".join(l.get("flags", []))
        oper = l.get("operstate", "")
        up = ("UP" in flags) and oper in ("UP", "UNKNOWN")
        wireless = _is_wireless(name)
        mac = l.get("address", "")
        ips = [ip for ip in addrs.get(name, []) if not ip.startswith("fe80")]
        if not include_down and not ips and not up:
            continue  # 跳过未插线/未启用的网卡，避免列表噪音
        out.append({
            "name": name,
            "type": "wifi" if wireless else "ether",
            "state": "up" if up else "down",
            "mac": mac,
            "addresses": ips,
        })
    return out


def _default_routes():
    r = _run(["ip", "-j", "route"])
    routes = []
    if r and r.returncode == 0:
        try:
            for rt in json.loads(r.stdout):
                if rt.get("dst") == "default" and "dev" in rt:
                    routes.append({
                        "dev": rt["dev"],
                        "gateway": rt.get("gateway", ""),
                        "metric": int(rt.get("metric", 65535)),
                    })
        except Exception:
            pass
    return routes


def active_interface():
    """当前用于上外网的网卡（默认路由所在接口）。
    网线（以太网）优先级永远高于 WiFi：在拥有默认路由的接口中，优先选以太网。"""
    routes = _default_routes()
    if not routes:
        return ""
    by_name = {i["name"]: i for i in interfaces()}
    for rt in sorted(routes, key=lambda x: x["metric"]):
        i = by_name.get(rt["dev"])
        if i and i["type"] == "ether" and i["state"] == "up":
            return rt["dev"]
    return sorted(routes, key=lambda x: x["metric"])[0]["dev"]


def gateway():
    for rt in _default_routes():
        if rt.get("gateway"):
            return rt["gateway"]
    return ""


def dns():
    ns = []
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                if line.strip().startswith("nameserver"):
                    parts = line.split()
                    if len(parts) >= 2:
                        ns.append(parts[1])
    except Exception:
        pass
    return ns


def wifi_ssid():
    """尽力获取当前 WiFi SSID（无线已连时）。多后端回退：iwgetid → nmcli → iw。"""
    r = _run(["iwgetid", "-r"])
    if r and r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    r = _run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"])
    if r and r.returncode == 0:
        for line in r.stdout.splitlines():
            a, _, s = line.partition(":")
            if a.strip() == "yes" and s.strip():
                return s.strip()
    for l in _links():
        if _is_wireless(l.get("ifname", "")) and "UP" in " ".join(l.get("flags", [])):
            r = _run(["iw", "dev", l["ifname"], "link"])
            if r and r.returncode == 0:
                m = re.search(r"SSID:\s*(.+)", r.stdout)
                if m:
                    return m.group(1).strip()
    return ""


_KNOWN_PORTS = {
    22: "SSH", 53: "DNS", 80: "Web 管理", 631: "CUPS / IPP 打印",
    5353: "mDNS / Avahi", 3702: "WS-Discovery 发现",
}


def listening_services():
    """返回正在监听的端口 [{proto, local, port, proc, svc}]。"""
    r = _run(["ss", "-tulnp"])
    rows = []
    if not r or r.returncode != 0:
        return rows
    for line in r.stdout.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 5:
            continue
        proto = cols[0]                       # tcp / udp
        local = cols[4]                        # 0.0.0.0:631 / [::]:631
        port = local.rsplit(":", 1)[-1] if ":" in local else ""
        proc = ""
        m = re.search(r'users:\(\("([^"]+)"', line)
        if m:
            proc = m.group(1)
        try:
            pnum = int(port)
        except ValueError:
            pnum = 0
        svc = _KNOWN_PORTS.get(pnum, "")
        rows.append({
            "proto": proto, "local": local, "port": port,
            "proc": proc, "svc": svc,
        })
    return rows


_STATUS_CACHE = {"ts": 0.0, "data": None}


def status():
    """网络状态快照（带 8 秒缓存，避免页面/轮询频繁调用 ip/ss）。"""
    global _STATUS_CACHE
    now = time.time()
    if _STATUS_CACHE["data"] is not None and now - _STATUS_CACHE["ts"] < 8:
        return _STATUS_CACHE["data"]
    data = {
        "hostname": hostname(),
        "active": active_interface(),
        "interfaces": interfaces(),
        "gateway": gateway(),
        "dns": dns(),
        "wifi": {"ssid": wifi_ssid()},
        "listening": listening_services(),
    }
    _STATUS_CACHE = {"ts": now, "data": data}
    return data


# ---------- 网卡参数设置（ifupdown，Debian/Ubuntu 通用）----------
def _read_interfaces_text():
    """读取 /etc/network/interfaces 与 interfaces.d/*（不含 PrintBox 托管片段）用于解析当前配置。"""
    parts = []
    main = "/etc/network/interfaces"
    if os.path.exists(main):
        try:
            with open(main) as f:
                parts.append(f.read())
        except Exception:
            pass
    d = "/etc/network/interfaces.d"
    if os.path.isdir(d):
        try:
            for fn in sorted(os.listdir(d)):
                if fn.startswith("printbox-"):
                    continue  # 自身托管片段稍后单独写，不读入解析避免重复
                fp = os.path.join(d, fn)
                if os.path.isfile(fp):
                    try:
                        with open(fp) as f:
                            parts.append(f.read())
                    except Exception:
                        pass
        except Exception:
            pass
    return "\n".join(parts)


def _netmask_to_prefix(mask):
    try:
        parts = [int(x) for x in mask.split(".")]
        return sum(bin(p).count("1") for p in parts)
    except Exception:
        return 0


def _prefix_to_netmask(prefix):
    try:
        p = int(prefix)
    except (ValueError, TypeError):
        p = 24
    mask = (0xffffffff << (32 - p)) & 0xffffffff
    return ".".join(str((mask >> s) & 0xff) for s in (24, 16, 8, 0))


def _apply_address(cfg, fam, val):
    if fam == "inet":
        parts = val.split()
        ip = parts[0]
        if "/" in ip:
            ip, _, pre = ip.partition("/")
            cfg["address_v4"] = ip
            cfg["prefix_v4"] = pre
        else:
            cfg["address_v4"] = ip
        if len(parts) > 1 and parts[1] == "netmask" and len(parts) > 2:
            cfg["netmask_v4"] = parts[2]
            p = _netmask_to_prefix(parts[2])
            if p:
                cfg["prefix_v4"] = str(p)
    else:
        if "/" in val:
            ip, _, pre = val.partition("/")
            cfg["address_v6"] = ip
            cfg["prefix_v6"] = pre
        else:
            cfg["address_v6"] = val


def iface_config(iface):
    """读取某网卡当前 ifupdown 配置（/etc/network/interfaces + interfaces.d/*）。
    返回字段含 method_v4/address_v4/netmask_v4/prefix_v4/gateway_v4/dns_v4
    与 method_v6/address_v6/prefix_v6/gateway_v6/dns_v6。"""
    cfg = {
        "method_v4": "dhcp", "address_v4": "", "netmask_v4": "",
        "prefix_v4": "", "gateway_v4": "", "dns_v4": "",
        "method_v6": "auto", "address_v6": "", "prefix_v6": "",
        "gateway_v6": "", "dns_v6": "",
    }
    try:
        text = _read_interfaces_text()
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            m = re.match(r"iface\s+(\S+)\s+(inet|inet6)\s+(\S+)", line)
            if m and m.group(1) == iface:
                fam = m.group(2)
                method = m.group(3)
                if fam == "inet":
                    cfg["method_v4"] = method
                else:
                    cfg["method_v6"] = method
                j = i + 1
                while j < len(lines):
                    nxt = lines[j].strip()
                    if not nxt or nxt.startswith("#"):
                        j += 1
                        continue
                    if nxt.startswith(("iface ", "source", "auto ", "allow-",
                                      "mapping ", "bond-", "vlan-")):
                        break
                    if nxt.startswith("address "):
                        _apply_address(cfg, fam, nxt[len("address "):].strip())
                    elif nxt.startswith("netmask "):
                        cfg["netmask_v4"] = nxt[len("netmask "):].strip()
                        if fam == "inet" and not cfg["prefix_v4"]:
                            p = _netmask_to_prefix(cfg["netmask_v4"])
                            if p:
                                cfg["prefix_v4"] = str(p)
                    elif nxt.startswith("gateway "):
                        cfg["gateway_" + ("v4" if fam == "inet" else "v6")] = \
                            nxt[len("gateway "):].strip()
                    elif nxt.startswith("dns-nameservers") or nxt.startswith("dns-nameserver"):
                        sp = nxt.split(" ", 2)
                        cfg["dns_" + ("v4" if fam == "inet" else "v6")] = \
                            sp[2] if len(sp) > 2 else ""
                    j += 1
                i = j
                continue
            i += 1
    except Exception:
        pass
    return cfg


def _remove_iface_stanza(iface):
    """从 /etc/network/interfaces 与 interfaces.d/*（不含 PrintBox 托管片段）删除
    所有 `iface <iface> ...` 段落，避免重复声明导致 networking 启动失败。"""
    targets = ["/etc/network/interfaces"]
    d = "/etc/network/interfaces.d"
    if os.path.isdir(d):
        for fn in os.listdir(d):
            if fn.startswith("printbox-"):
                continue
            targets.append(os.path.join(d, fn))
    pat = re.compile(r"^\s*iface\s+" + re.escape(iface) + r"\s+(inet|inet6)\b")
    for path in targets:
        if not os.path.isfile(path):
            continue
        try:
            with open(path) as f:
                lines = f.readlines()
        except Exception:
            continue
        out = []
        skip = False
        for line in lines:
            if pat.match(line):
                skip = True
                continue
            if skip:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith(("iface ", "source", "auto ", "allow-",
                                        "mapping ", "bond-", "vlan-")):
                    skip = False
                    out.append(line)
                    continue
                continue  # 缩进的从属行（address/netmask/gateway/dns…）丢弃
            out.append(line)
        try:
            with open(path, "w") as f:
                f.writelines(out)
        except Exception:
            pass


def set_iface_config(iface, data):
    """写入 PrintBox 托管的 ifupdown 配置片段并重启该网卡。
    返回 (ok, msg)。【危险】会改变本机网络，可能短暂断网；调用方需已向用户确认。
    默认方法：IPv4=dhcp、IPv6=auto（SLAAC）；可选 static 手动指定地址/掩码/网关/DNS。"""
    # 1) 先从其它文件移除同名 iface 段落，避免重复声明
    _remove_iface_stanza(iface)
    # 2) 构造托管片段
    lines = [f"# PrintBox managed interface config for {iface}", ""]
    m4 = (data.get("method_v4") or "dhcp").strip() or "dhcp"
    if m4 == "static":
        addr = (data.get("address_v4") or "").strip()
        if not addr:
            return False, "静态 IPv4 需要填写地址"
        prefix = data.get("prefix_v4") or _netmask_to_prefix(data.get("netmask_v4", "")) or 24
        try:
            prefix = int(prefix)
        except (ValueError, TypeError):
            prefix = 24
        lines.append(f"iface {iface} inet static")
        lines.append(f"  address {addr}/{prefix}")
        if (data.get("gateway_v4") or "").strip():
            lines.append(f"  gateway {data['gateway_v4'].strip()}")
        if (data.get("dns_v4") or "").strip():
            lines.append(f"  dns-nameservers {data['dns_v4'].strip()}")
    else:
        lines.append(f"iface {iface} inet dhcp")
    m6 = (data.get("method_v6") or "auto").strip() or "auto"
    if m6 == "static":
        addr6 = (data.get("address_v6") or "").strip()
        if addr6:
            prefix6 = (data.get("prefix_v6") or "64").strip() or "64"
            lines.append(f"iface {iface} inet6 static")
            lines.append(f"  address {addr6}/{prefix6}")
            if (data.get("gateway_v6") or "").strip():
                lines.append(f"  gateway {data['gateway_v6'].strip()}")
            if (data.get("dns_v6") or "").strip():
                lines.append(f"  dns-nameservers {data['dns_v6'].strip()}")
    elif m6 == "dhcp":
        lines.append(f"iface {iface} inet6 dhcp")
    elif m6 == "auto":
        lines.append(f"iface {iface} inet6 auto")
    # m6 == "none" 时不写 inet6 段落（禁用 IPv6）
    path = f"/etc/network/interfaces.d/printbox-{iface}"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        return False, f"写入配置失败: {e}"
    # 3) 应用：先 ifdown 再 ifup（可能短暂断网）
    C.run(["ifdown", iface], timeout=15)
    rc, _, err = C.run(["ifup", iface], timeout=30)
    if rc != 0:
        return False, f"网卡 {iface} 重新启用失败: {err}"
    # 4) 使缓存失效
    global _STATUS_CACHE
    _STATUS_CACHE = {"ts": 0.0, "data": None}
    return True, f"已应用网络配置到 {iface}（IPv4={m4}, IPv6={m6}）"
