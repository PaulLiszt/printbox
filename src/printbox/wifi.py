"""PrintBox WiFi 管理：切换网络 + 已存网络优先级/回退。

基于 NetworkManager（nmcli）实现，与具体发行版无关（无 NM 时优雅降级）。

用户需求（设计目标）：
  - 通过 WebUI 连接 / 切换到某个 WiFi；
  - 已保存的 WiFi 按「优先级」排序，优先级高者优先连接；
  - 连接新 WiFi 时**不删除**旧网络的已存配置；若新网络连不上，
    NetworkManager 会自动回退到下一个可用的已存网络（与手机行为一致）。
"""
import os
import re
import shutil
import subprocess
import time

from . import config as C


def has_nm():
    """是否由 NetworkManager 实际管理网络。

    仅当 nmcli 存在「且」NetworkManager 服务正在运行才视为可用；否则走 wpa_supplicant 后端。
    先前只看 nmcli 是否存在，导致 NM 被禁用（服务停掉）却仍按 NM 路径读已存网络，
    结果「当前已连接的 WiFi」因不在 NM 连接列表里而消失在「已保存」中。"""
    if shutil.which("nmcli") is None:
        return False
    rc, _, _ = _run(["systemctl", "is-active", "NetworkManager"])
    return rc == 0


def _run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception as e:  # 超时 / 命令不存在等
        return -1, "", str(e)


# ---------------------------------------------------------------------------
# wpa_supplicant 后端（无 NetworkManager 时使用，符合项目「后端无关」设计）
# 仅管理 WiFi 接口，不触碰以太网，因此不会破坏已有的有线/SSH 连接。
# ---------------------------------------------------------------------------
WPA_CONF_DIR = "/etc/wpa_supplicant"


def _wifi_iface():
    """找一个 managed 无线接口（如 wlan0）；无则返回空串。"""
    rc, out, _ = _run(["iw", "dev"])
    ifaces = re.findall(r"Interface\s+(\S+)", out)
    if ifaces:
        return ifaces[0]
    if os.path.isdir("/sys/class/net"):
        for n in sorted(os.listdir("/sys/class/net")):
            if n == "lo":
                continue
            if os.path.exists(f"/sys/class/net/{n}/wireless") or \
               os.path.exists(f"/sys/class/net/{n}/phy80211"):
                return n
    return ""


def _wpa_conf(iface):
    return os.path.join(WPA_CONF_DIR, f"wpa_supplicant-{iface}.conf")


def _read_wpa_networks(conf):
    """解析 wpa_supplicant 配置里的 network={} 块，返回 [{ssid, raw}]。"""
    if not os.path.exists(conf):
        return []
    nets, cur = [], None
    try:
        with open(conf, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                s = line.strip()
                if s.startswith("network={"):
                    cur = {"ssid": "", "raw": [line]}
                    nets.append(cur)
                elif cur is not None:
                    cur["raw"].append(line)
                    if s.startswith("ssid="):
                        cur["ssid"] = s[len("ssid="):].strip().strip('"')
                    if s == "}":
                        cur = None
    except Exception:
        return []
    return [n for n in nets if n["ssid"]]


def _write_wpa_conf(conf, nets):
    os.makedirs(os.path.dirname(conf), exist_ok=True)
    try:
        with open(conf, "w", encoding="utf-8") as f:
            f.write("ctrl_interface=DIR=/run/wpa_supplicant GROUP=netdev\n")
            f.write("update_config=1\n")
            for n in nets:
                f.writelines(n["raw"])
                if not n["raw"][-1].endswith("\n"):
                    f.write("\n")
    except Exception as e:
        return False, str(e)
    return True, ""


def _current_ssid_wpa(iface):
    if not iface:
        return ""
    rc, out, _ = _run(["iw", "dev", iface, "link"])
    m = re.search(r"SSID:\s*(.+)", out)
    return m.group(1).strip() if m else ""


def _start_wpa(iface, conf):
    """启动 wpa_supplicant（优先 systemd 模板单元，否则直接后台运行）并获取 IP。"""
    _run(["systemctl", "stop", f"wpa_supplicant@{iface}.service"])
    _run(["pkill", "-f", f"wpa_supplicant.*-i{iface}"])
    rc, _, _ = _run(["systemctl", "restart", f"wpa_supplicant@{iface}.service"])
    if rc != 0:
        _run(["wpa_supplicant", "-i", iface, "-c", conf, "-B",
              "-D", "nl80211,wext"])
    # DHCP（任选其一）
    if shutil.which("dhclient"):
        _run(["dhclient", iface], timeout=30)
    elif shutil.which("udhcpc"):
        _run(["udhcpc", "-i", iface, "-n", "-q"], timeout=30)
    elif shutil.which("dhcpcd"):
        _run(["dhcpcd", iface], timeout=30)
    # 尽力持久化（systemd 模板单元存在才启用；否则仅运行时生效）
    _run(["systemctl", "enable", f"wpa_supplicant@{iface}.service"])


def _list_saved_from_wpa():
    iface = _wifi_iface()
    if not iface:
        return []
    conf = _wpa_conf(iface)
    nets = _read_wpa_networks(conf)
    cur = _current_ssid_wpa(iface)
    out = []
    for i, n in enumerate(nets):
        out.append({
            "uuid": n["ssid"],
            "name": n["ssid"],
            "priority": len(nets) - i,   # conf 中越靠前优先级越高
            "active": n["ssid"] == cur,
            "device": iface,
        })
    return out


def _connect_wpa(ssid, password=None):
    iface = _wifi_iface()
    if not iface:
        return False, "未发现无线网卡（iw dev 无输出，且 /sys/class/net 下无无线接口）"
    if not shutil.which("wpa_supplicant"):
        return False, "未安装 wpa_supplicant；请在盒上运行：apt install wpasupplicant isc-dhcp-client"
    conf = _wpa_conf(iface)
    nets = _read_wpa_networks(conf)
    block = ["network={\n", f'  ssid="{ssid}"\n']
    if password:
        block.append(f'  psk="{password}"\n')
    else:
        block.append("  key_mgmt=NONE\n")
    block.append("}\n")
    target = next((n for n in nets if n["ssid"] == ssid), None)
    if target:
        target["raw"] = block
        nets = [target] + [n for n in nets if n is not target]
    else:
        nets = [{"ssid": ssid, "raw": block}] + nets
    ok, err = _write_wpa_conf(conf, nets)
    if not ok:
        return False, f"写入 wpa_supplicant 配置失败: {err}"
    _start_wpa(iface, conf)
    for _ in range(2):
        time.sleep(4)
        rc, out, _ = _run(["iw", "dev", iface, "link"])
        if "SSID:" in out or "Connected to" in out:
            _republish()
            return True, f"已连接到 {ssid}（wpa_supplicant@{iface}）"
    return False, (f"已写入配置并启动 wpa_supplicant，但暂未关联到 {ssid}；"
                   f"请检查密码或信号。iw link: {out.strip()[:200]}")


def _connect_saved_wpa(uuid):
    iface = _wifi_iface()
    if not iface:
        return False, "未发现无线网卡"
    conf = _wpa_conf(iface)
    if not os.path.exists(conf):
        return False, f"未找到 wpa_supplicant 配置 {conf}"
    nets = _read_wpa_networks(conf)
    target = next((n for n in nets if n["ssid"] == uuid), None)
    if target is None:
        return False, f"未找到已保存网络 {uuid}"
    nets = [target] + [n for n in nets if n is not target]  # 提到最前 = 优先
    ok, err = _write_wpa_conf(conf, nets)
    if not ok:
        return False, f"写入配置失败: {err}"
    _start_wpa(iface, conf)
    _republish()
    return True, f"已切换到 {uuid}"


def _move_wpa(uuid, direction):
    iface = _wifi_iface()
    if not iface:
        return False, "未发现无线网卡"
    conf = _wpa_conf(iface)
    if not os.path.exists(conf):
        return False, f"未找到 wpa_supplicant 配置 {conf}"
    nets = _read_wpa_networks(conf)
    idx = next((i for i, n in enumerate(nets) if n["ssid"] == uuid), None)
    if idx is None:
        return False, "未找到该网络"
    j = idx - 1 if direction == "up" else idx + 1
    if j < 0 or j >= len(nets):
        return False, "已到顶/底，无法移动"
    nets[idx], nets[j] = nets[j], nets[idx]
    ok, err = _write_wpa_conf(conf, nets)
    if not ok:
        return False, f"写入配置失败: {err}"
    _start_wpa(iface, conf)
    return True, "已调整连接优先级"


def _forget_wpa(uuid):
    iface = _wifi_iface()
    if not iface:
        return False, "未发现无线网卡"
    conf = _wpa_conf(iface)
    if not os.path.exists(conf):
        return False, f"未找到 wpa_supplicant 配置 {conf}"
    nets = _read_wpa_networks(conf)
    target = next((n for n in nets if n["ssid"] == uuid), None)
    if target is None:
        return False, "未找到该网络"
    if target["ssid"] == _current_ssid_wpa(iface):
        return False, "不能删除当前正在使用的连接（请先连到其它网络再删除）"
    nets = [n for n in nets if n is not target]
    ok, err = _write_wpa_conf(conf, nets)
    if not ok:
        return False, f"写入配置失败: {err}"
    if nets:
        _start_wpa(iface, conf)
    else:
        _run(["systemctl", "stop", f"wpa_supplicant@{iface}.service"])
        _run(["pkill", "-f", f"wpa_supplicant.*-i{iface}"])
    return True, f"已忘记网络 {uuid}"


def current_ssid():
    """当前已连接的 WiFi SSID；未连接返回空串。NM 优先，否则走 wpa_supplicant。"""
    if has_nm():
        rc, out, _ = _run(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"])
        for line in out.splitlines():
            active, ssid = line.split(":", 1)
            if active.strip() == "yes":
                return ssid
        return ""
    return _current_ssid_wpa(_wifi_iface())


def _wifi_uuids():
    rc, out, _ = _run(["nmcli", "-t", "-f", "UUID,TYPE", "connection", "show"])
    res = []
    for line in out.splitlines():
        u, t = line.split(":", 1)
        if t.strip() == "802-11-wireless":
            res.append(u.strip())
    return res


def _conn_priority(uuid):
    rc, out, _ = _run(["nmcli", "-t", "-f", "connection.autoconnect-priority",
                       "connection", "show", uuid])
    val = out.split(":", 1)[1].strip() if ":" in out else ""
    try:
        return int(val)
    except ValueError:
        return 0


def _conn_is_active(uuid):
    rc, out, _ = _run(["nmcli", "-t", "-f", "ACTIVE", "connection", "show", uuid])
    return out.split(":", 1)[1].strip() == "yes" if ":" in out else False


def _list_saved_from_files():
    """直接从 NetworkManager 的 keyfile（/etc/NetworkManager/system-connections/*.nmconnection）
    读取已保存的 WiFi 连接。这是『已保存网络』的权威来源，不依赖 nmcli 的 type 过滤，
    能稳定覆盖『实际已连着网、但 nmcli 查询被漏掉/不可用时列表为空』的情况。
    仅保留 type=802-11-wireless 的条目。"""
    conn_dir = "/etc/NetworkManager/system-connections"
    if not os.path.isdir(conn_dir):
        return []
    out = []
    try:
        names = sorted(os.listdir(conn_dir))
    except Exception:
        return []
    for fn in names:
        if not (fn.endswith(".nmconnection") or fn.endswith(".conf")):
            continue
        path = os.path.join(conn_dir, fn)
        try:
            data = {}
            section = None
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("[") and line.endswith("]"):
                        section = line[1:-1].strip()
                        data.setdefault(section, {})
                    elif "=" in line and section is not None:
                        k, v = line.split("=", 1)
                        data[section][k.strip()] = v.strip()
            c = (data or {}).get("connection", {})
            if c.get("type") != "802-11-wireless":
                continue
            uuid = c.get("uuid", "")
            if not uuid:
                continue
            try:
                prio = int(c.get("autoconnect-priority", "0"))
            except ValueError:
                prio = 0
            out.append({
                "uuid": uuid,
                "name": c.get("id", uuid),
                "priority": prio,
                "active": False,
                "device": "",
            })
        except Exception:
            continue
    return out


def _list_saved_from_nmcli():
    """旧逻辑：完全依赖 nmcli（带 type 过滤）。仅作为无 root 读取 keyfile 时的回退。"""
    out = []
    for uuid in _wifi_uuids():
        rc, name_out, _ = _run(["nmcli", "-t", "-f", "NAME", "connection", "show", uuid])
        name = name_out.split(":", 1)[1] if ":" in name_out else name_out
        rc, dev_out, _ = _run(["nmcli", "-t", "-f", "DEVICE", "connection", "show", uuid])
        dev = dev_out.split(":", 1)[1].strip() if ":" in dev_out else ""
        out.append({
            "uuid": uuid,
            "name": name.strip(),
            "priority": _conn_priority(uuid),
            "active": _conn_is_active(uuid),
            "device": dev,
        })
    return out


def list_saved():
    """已保存 WiFi 连接（按优先级降序）。每项: uuid,name,priority,active,device。
    有 NetworkManager 时读 NM keyfile；否则读 wpa_supplicant 配置（后端无关）。
    无论后端，都会把「当前已连接的 SSID」确保纳入列表并标记为使用中——避免 wpa/NM 配置
    未持久化该网络时，已连上的 WiFi（如 MyHomeWiFi）在「已保存」里消失。"""
    if has_nm():
        # 在线状态（UUID -> (active, device)），不按 type 过滤，避免漏掉连接
        active_map = {}
        rc, out, _ = _run(["nmcli", "-t", "-f", "UUID,ACTIVE,DEVICE", "connection", "show"])
        if rc == 0:
            for line in out.splitlines():
                p = line.split(":", 2)
                if len(p) >= 3:
                    active_map[p[0].strip()] = (p[1].strip() == "yes", p[2].strip())
        saved = _list_saved_from_files()
        if not saved:
            saved = _list_saved_from_nmcli()
        for s in saved:
            a, d = active_map.get(s["uuid"], (False, ""))
            s["active"] = a
            s["device"] = d
    else:
        saved = _list_saved_from_wpa()
    # 始终确保当前已连接的网络出现在列表里
    cur = current_ssid()
    if cur:
        existing = next((s for s in saved
                        if s.get("name") == cur or s.get("uuid") == cur), None)
        if existing:
            existing["active"] = True
        else:
            iface = ""
            try:
                iface = _wifi_iface()
            except Exception:
                pass
            top = max((s.get("priority", 0) for s in saved), default=0)
            saved.append({
                "uuid": cur, "name": cur,
                "priority": top + 1, "active": True, "device": iface,
            })
    saved.sort(key=lambda x: (-x["priority"], x["name"]))
    return saved


def list_available():
    """附近可见 WiFi（SSID/信号/加密），供切换时选择。"""
    from . import sysinfo as SYS
    return SYS.wifi_scan()


def _republish():
    """WiFi 切换后 IP 可能变化，刷新 AirPrint 发布。"""
    try:
        from . import printers as PS
        PS.republish_airprint()
    except Exception:
        pass


def connect(ssid, password=None):
    """连接到某 WiFi（如需则带密码）。有 NM 走 nmcli，否则走 wpa_supplicant。"""
    if has_nm():
        return _connect_nm(ssid, password)
    return _connect_wpa(ssid, password)


def _connect_nm(ssid, password=None):
    """连接到某 WiFi（如需则带密码）。保存为 NM 连接配置，不删除其它已存网络。
    成功后将其优先级设为较高值使其成为首选；若连不上，旧网络继续可用（回退）。
    返回 (ok, message)。"""
    if not has_nm():
        return False, "未检测到 NetworkManager（nmcli），无法管理 WiFi"
    if not ssid:
        return False, "SSID 不能为空"
    cmd = ["nmcli", "dev", "wifi", "connect", ssid]
    if password:
        cmd += ["password", password]
    rc, out, err = _run(cmd, timeout=40)
    if rc != 0:
        return False, (err or out).strip() or "连接失败"
    # 设为首选（高于默认 0），但保留旧网络作为回退
    try:
        saved = list_saved()
        max_prio = max((s["priority"] for s in saved), default=0)
        new_prio = max(max_prio + 1, 100)
        _run(["nmcli", "connection", "modify", ssid,
              "connection.autoconnect-priority", str(new_prio)])
    except Exception:
        pass
    _republish()
    return True, f"已连接到 {ssid}（并设为首选；旧网络保留用于回退）"


def connect_saved(uuid):
    if has_nm():
        return _connect_saved_nm(uuid)
    return _connect_saved_wpa(uuid)


def _connect_saved_nm(uuid):
    if not has_nm():
        return False, "未检测到 NetworkManager"
    rc, out, err = _run(["nmcli", "connection", "up", uuid], timeout=40)
    if rc != 0:
        return False, (err or out).strip() or "连接失败"
    _republish()
    return True, "已切换到该已存网络"


def set_priority(uuid, priority):
    if not has_nm():
        return False, "未检测到 NetworkManager"
    try:
        prio = int(priority)
    except ValueError:
        return False, "优先级必须是数字"
    _run(["nmcli", "connection", "modify", uuid,
          "connection.autoconnect-priority", str(prio)])
    return True, f"已将优先级设为 {prio}"


def move(uuid, direction):
    """上移/下移：与相邻优先级交换，实现排序（高者优先）。"""
    if has_nm():
        return _move_nm(uuid, direction)
    return _move_wpa(uuid, direction)


def _move_nm(uuid, direction):
    """上移/下移：与相邻优先级交换，实现排序（高者优先）。"""
    saved = list_saved()
    idx = next((i for i, s in enumerate(saved) if s["uuid"] == uuid), None)
    if idx is None:
        return False, "未找到该连接"
    j = idx - 1 if direction == "up" else idx + 1
    if j < 0 or j >= len(saved):
        return False, "已到顶/底，无法移动"
    a, b = saved[idx], saved[j]
    pa, pb = a["priority"], b["priority"]
    if pa == pb:
        pb = pa + 1 if direction == "up" else pa - 1
    set_priority(a["uuid"], pb)
    set_priority(b["uuid"], pa)
    return True, "已调整顺序"


def forget(uuid):
    if has_nm():
        return _forget_nm(uuid)
    return _forget_wpa(uuid)


def _forget_nm(uuid):
    if not has_nm():
        return False, "未检测到 NetworkManager"
    saved = list_saved()
    target = next((s for s in saved if s["uuid"] == uuid), None)
    if target is None:
        return False, "未找到该连接"
    if target["active"]:
        return False, "不能删除当前正在使用的连接（请先连到其它网络再删除）"
    rc, out, err = _run(["nmcli", "connection", "delete", uuid], timeout=20)
    if rc != 0:
        return False, (err or out).strip() or "删除失败"
    return True, f"已忘记网络 {target['name']}"
