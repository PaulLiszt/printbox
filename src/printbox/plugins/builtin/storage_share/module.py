"""storage-share 插件：native 路径 POC。

验证点：PrintBox 不写 Samba 应用本身，只写「配置片 + 托管 systemd」。
Time Machine 只是 smb.conf 里多几行 fruit 模块——纯配置，零重写。

升级点（需求 2 / 3）：
  a) 自动挂载 USB 存储（udev 热插拔触发 printbox usb-mount，或本页一键挂载）
  b) 区分系统盘：绝不挂载 / /boot /efi 所在磁盘（mount.py 的 is_system_device 把关）
  c) 用系统已建立账户访问磁盘：SMB 强制单账户（force user = share_owner），
     磁盘读写由 pb-disk 组控制（valid users = @pb-disk / 组归属）
"""
import os
import shutil
import subprocess

try:
    from printbox.plugins import manager as MG
except Exception:  # 独立单元测试时 printbox 可能不在 path
    MG = None

try:
    from printbox import config as C
except Exception:
    C = None

try:
    from printbox.plugins.builtin.storage_share import mount as MNT
except Exception:
    MNT = None

DISK_GROUP = "pb-disk"
SMB_DROPIN_DIR = "/etc/samba/smb.conf.d"
SMB_DROPIN = os.path.join(SMB_DROPIN_DIR, "printbox-share.conf")
SMB_MAIN = "/etc/samba/smb.conf"
UDEV_RULE_SRC = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "data",
    "99-printbox-usb-storage.rules")
UDEV_RULE_DST = "/etc/udev/rules.d/99-printbox-usb-storage.rules"


def generate_smb_conf(cfg):
    """纯函数：根据配置生成共享段。

    cfg 已是「resolved」后的：share_owner 若留空则不输出 force user（install 层会先解析）。
    Time Machine 开启时追加 fruit VFS 模块。
    """
    path = cfg.get("share_path") or "/srv/printbox-share"
    tm = bool(cfg.get("timemachine", True))
    guest = "yes" if cfg.get("guest_ok", True) else "no"
    owner = (cfg.get("share_owner") or "").strip()
    lines = [
        "[PrintBoxShare]",
        f"   path = {path}",
        "   browseable = yes",
        "   read only = no",
        f"   guest ok = {guest}",
    ]
    # 强制单账户：SMB 访问以 share_owner 落地，不与各人登录密码耦合
    if owner:
        lines.append(f"   force user = {owner}")
    lines.append(f"   force group = {DISK_GROUP}")
    if guest == "yes":
        if owner:
            lines.append(f"   guest account = {owner}")
    else:
        # 非访客：仅允许 pb-disk 组成员（系统已建立账户）访问
        lines.append(f"   valid users = @{DISK_GROUP}")
    lines += [
        "   create mask = 0644",
        "   directory mask = 0755",
    ]
    if tm:
        # macOS 时间机器要求：fruit VFS + aapl 协商
        lines += [
            "   vfs objects = catia fruit streams_xattr",
            "   fruit:aapl = yes",
            "   fruit:time machine = yes",
        ]
    return "\n".join(lines) + "\n"


def _resolve_owner(cfg):
    """非纯函数：解析有效的 SMB 落地账户。

    share_owner 留空时回退到第一个 pb-disk 组成员；都没有则回退 nobody。
    """
    owner = (cfg.get("share_owner") or "").strip()
    if owner:
        return owner
    try:
        from printbox import users as US
        for u in US.group_members(DISK_GROUP):
            if u:
                return u
    except Exception:
        pass
    return "nobody"


def _write_smb(lines):
    with open(SMB_MAIN, "w", encoding="utf-8") as f:
        f.writelines(lines)


def _insert_after_global(lines, block):
    """把 block(行列表) 插入到 [global] 段首行之后；若无 [global] 则补一个。"""
    idx = next((i for i, ln in enumerate(lines) if ln.strip().lower() == "[global]"), None)
    if idx is None:
        lines.append("\n[global]\n")
        idx = len(lines) - 1
    lines[idx + 1:idx + 1] = block


def _ensure_include_line():
    """确保主 smb.conf 的 [global] 段包含我们的 drop-in include（幂等）。

    注意：
    - include 必须放在 [global] 段内才生效，追加到文件末尾无效。
    - **Samba 不会展开 include 里的 `*` 通配符**，旧版本写成 `*.conf` 会导致 drop-in
      永远不加载、共享不出现；这里必须用「精确文件路径」。
    - 同时保证 `map to guest = bad user`，否则访客(匿名)写共享会失败。
    """
    if not os.path.exists(SMB_MAIN):
        return
    with open(SMB_MAIN, "r", encoding="utf-8") as f:
        lines = f.readlines()
    # 去掉旧版通配 include（Samba 不展开），并检查是否已存在精确 include
    out = []
    has_exact = False
    for ln in lines:
        s = ln.strip()
        if s.startswith("include") and SMB_DROPIN_DIR in s:
            if "*" in s:
                continue  # 丢弃通配形式（Samba 不展开，等于没配）
            if SMB_DROPIN in s:
                has_exact = True
        out.append(ln)
    if has_exact:
        # 已正确注入：仅确保 map to guest 存在
        if not any("map to guest" in l for l in out):
            _insert_after_global(out, ["   map to guest = bad user\n"])
            _write_smb(out)
        return
    # 需要插入精确 include（+ map to guest）
    block = ["# PrintBox storage-share drop-in\n",
             "   include = %s\n" % SMB_DROPIN]
    if not any("map to guest" in l for l in out):
        block.append("   map to guest = bad user\n")
    _insert_after_global(out, block)
    _write_smb(out)


DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "data")


def _ensure_udev():
    """安装 USB 自动挂载的 udev 规则 + systemd 模板单元并 reload（幂等）。

    单元里 ExecStart 写死 /usr/local/bin/printbox，由 printbox init 建立的软链指向当前
    venv；这里也兜底补装单元，确保仅 enable 插件（未跑 init）时热插拔也能触发。
    """
    try:
        os.makedirs(SMB_DROPIN_DIR, exist_ok=True)
        os.makedirs("/etc/udev/rules.d", exist_ok=True)
        os.makedirs("/etc/systemd/system", exist_ok=True)
        if os.path.exists(UDEV_RULE_SRC):
            shutil.copy2(UDEV_RULE_SRC, UDEV_RULE_DST)
        for unit in ("printbox-usb-mount@.service", "printbox-usb-umount@.service"):
            usrc = os.path.join(DATA_DIR, unit)
            if os.path.exists(usrc):
                shutil.copy2(usrc, os.path.join("/etc/systemd/system", unit))
        # 兜底：确保 /usr/local/bin/printbox 软链指向当前 venv（服务单元 ExecStart 写死该路径）
        try:
            import sys
            venv_bin = os.path.dirname(os.path.realpath(sys.executable))
            venv_pb = os.path.join(venv_bin, "printbox")
            link = "/usr/local/bin/printbox"
            if os.path.exists(venv_pb) and not os.path.exists(link):
                os.symlink(venv_pb, link)
        except Exception:
            pass
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True, text=True)
        subprocess.run(["udevadm", "control", "--reload-rules"],
                       capture_output=True, text=True)
        subprocess.run(["udevadm", "trigger", "--subsystem-match=block"],
                       capture_output=True, text=True)
    except Exception:
        pass


def install(manifest=None):
    """install 钩子（enable 时调用）：写配置片并重启 samba；安装 udev 规则；按需挂载 USB。

    仅在盒上执行（MG 可用）。
    """
    if not MG:
        return
    cfg = dict(MG.get_config("storage-share") or {})
    # 解析生效的归属账户，写回 cfg 供 generate_smb_conf 使用
    cfg["share_owner"] = _resolve_owner(cfg)
    # 共享目录：建好并归 pb-disk 组（本地 CLI/系统账户也能读写）
    share_path = cfg.get("share_path") or "/srv/printbox-share"
    os.makedirs(share_path, exist_ok=True)
    try:
        subprocess.run(["chown", "root:%s" % DISK_GROUP, share_path],
                       capture_output=True, text=True)
        subprocess.run(["chmod", "775", share_path], capture_output=True, text=True)
    except Exception:
        pass
    os.makedirs(SMB_DROPIN_DIR, exist_ok=True)
    with open(SMB_DROPIN, "w", encoding="utf-8") as f:
        f.write(generate_smb_conf(cfg))
    _ensure_include_line()
    _ensure_udev()
    _warn_if_samba_user_missing(cfg["share_owner"])
    subprocess.run(["systemctl", "restart", "smbd", "nmbd"],
                   capture_output=True, text=True)
    # 若开启自动挂载，立即把当前符合条件的 USB 存储挂进共享目录（使其出现在 SMB 共享里）
    if cfg.get("auto_mount") and MNT:
        try:
            MNT.auto_mount_all(owner=cfg["share_owner"], group=DISK_GROUP,
                               share_root=share_path)
        except Exception:
            pass


def _warn_if_samba_user_missing(owner):
    """若 Samba 已安装但共享归属账户不在 Samba 密码库，记录告警。

    Samba 用独立密码库，Linux 密码不算数。账户没进 Samba 库时，Windows 访问会
    直接『拒绝访问』。此时提示用户用 `printbox users set-pass <账户> <密码>` 同步。
    """
    if not owner:
        return
    pdbedit = shutil.which("pdbedit")
    if not pdbedit:
        return  # 没装 samba，跳过（仅 storage-share 插件未启用时常见）
    try:
        r = subprocess.run([pdbedit, "-L"], capture_output=True, text=True)
        if r.returncode == 0 and owner not in r.stdout:
            if C:
                C.log("storage-share", "WARN",
                      "共享归属账户 %s 尚未加入 Samba 密码库；Windows 访问将被拒绝。"
                      "请运行 `printbox users set-pass %s <密码>` 将其同步进 Samba。"
                      % (owner, owner))
    except Exception:
        pass


def disable(manifest=None):
    if os.path.exists(SMB_DROPIN):
        os.remove(SMB_DROPIN)
    subprocess.run(["systemctl", "restart", "smbd", "nmbd"],
                   capture_output=True, text=True)


def _usb_drives_html(lang):
    """渲染未挂载的 USB 存储列表（含一键挂载按钮）。"""
    if not MNT:
        return ('<p class="muted">挂载模块不可用。</p>')
    try:
        drives = MNT.list_usb_drives()
    except Exception:
        drives = []
    if not drives:
        return ('<p class="muted">' +
                ("当前无可自动挂载的 USB 存储（已挂载或均为系统盘）。"
                 if lang == "zh" else
                 "No mountable USB storage right now (mounted or system disk).") + "</p>")
    rows = []
    for d in drives:
        label = d.get("label") or d.get("model") or d["name"]
        size = d.get("size") or ""
        rows.append(
            "<tr>"
            f"<td>{label}</td>"
            f"<td class='mono'>{d['name']}</td>"
            f"<td>{size}</td>"
            "<td style='white-space:nowrap'>"
            f'<form method="post" action="/plugin/storage-share" style="display:inline">'
            '<input type="hidden" name="act" value="mount_usb">'
            f'<input type="hidden" name="dev" value="{d["name"]}">'
            '<button class="btn tiny" type="submit">挂载</button></form></td></tr>')
    return ("<table><tr><th>" +
            ("设备" if lang == "zh" else "Device") + "</th><th>节点</th><th>" +
            ("容量" if lang == "zh" else "Size") + "</th><th>" +
            ("操作" if lang == "zh" else "Action") + "</th></tr>" +
            "".join(rows) + "</table>")


def _mounted_html(lang):
    if not MNT:
        return ""
    try:
        mounted = MNT.list_mounted_shares()
    except Exception:
        mounted = []
    if not mounted:
        return ('<p class="muted">' +
                ("尚未挂载任何 USB 共享。" if lang == "zh" else "No USB share mounted.") + "</p>")
    rows = []
    for m in mounted:
        rows.append(
            "<tr>"
            f"<td>{m['name']}</td><td class='mono'>{m['mountpoint']}</td>"
            "<td style='white-space:nowrap'>"
            f'<form method="post" action="/plugin/storage-share" style="display:inline">'
            '<input type="hidden" name="act" value="umount_usb">'
            f'<input type="hidden" name="dev" value="{m["name"]}">'
            '<button class="btn tiny danger" type="submit">卸载</button></form></td></tr>')
    return ("<table><tr><th>" +
            ("已挂载" if lang == "zh" else "Mounted") + "</th><th>" +
            ("挂载点" if lang == "zh" else "Mount point") + "</th><th>" +
            ("操作" if lang == "zh" else "Action") + "</th></tr>" +
            "".join(rows) + "</table>")


def _owner_options_html(selected, lang):
    """下拉：首个选项=自动（留空），其余=pb-disk 成员。"""
    opts = [f'<option value="">{"（自动：首个磁盘账户）" if lang=="zh" else "(auto: first disk user)"}</option>']
    users = []
    try:
        from printbox import users as US
        users = US.group_members(DISK_GROUP)
    except Exception:
        pass
    for u in users:
        u = u.strip()
        if not u:
            continue
        sel = " selected" if u == selected else ""
        opts.append(f'<option value="{u}"{sel}>{u}</option>')
    return "".join(opts)


def handle_get(handler, lang):
    from printbox import webapp
    cfg = MG.get_config("storage-share") if MG else {}
    owner = (cfg.get("share_owner") or "").strip()
    tokens = {
        "title": "存储共享" if lang == "zh" else "Storage Share",
        "active": "plugins",
        "share_path": cfg.get("share_path", "/srv/printbox-share"),
        "tm_checked": "checked" if cfg.get("timemachine", True) else "",
        "guest_checked": "checked" if cfg.get("guest_ok", True) else "",
        "auto_checked": "checked" if cfg.get("auto_mount", False) else "",
        "owner_options": _owner_options_html(owner, lang),
        "usb_drives_html": _usb_drives_html(lang),
        "mounted_html": _mounted_html(lang),
        "notice": "",
    }
    tpl = open(os.path.join(os.path.dirname(__file__), "page.html"), "r", encoding="utf-8").read()
    return webapp.render_plugin_page(tpl, tokens, lang)


def handle_post(handler, form, lang):
    from printbox import webapp
    get = lambda k: (form.get(k, [""])[0] or "").strip()
    act = get("act")

    # 一键挂载/卸载（不影响其它配置）
    if act == "mount_usb":
        dev = get("dev")
        ok, msg = (False, "未指定设备")
        if dev and MNT:
            try:
                _cfg = MG.get_config("storage-share") or {}
                owner = _cfg.get("share_owner") or ""
                share_path = _cfg.get("share_path") or "/srv/printbox-share"
            except Exception:
                owner, share_path = "", "/srv/printbox-share"
            ok, msg = MNT.usb_mount_main(dev, force=True, share_root=share_path) \
                if hasattr(MNT, "usb_mount_main") \
                else MNT.mount_usb(dev, owner=owner, group=DISK_GROUP, share_root=share_path)
        notice = f'<p class="badge {"ok" if ok else "err"}">{msg}</p>'
        cfg = MG.get_config("storage-share") if MG else {}
        owner = (cfg.get("share_owner") or "").strip()
        tokens = {
            "title": "存储共享" if lang == "zh" else "Storage Share",
            "active": "plugins",
            "share_path": cfg.get("share_path", "/srv/printbox-share"),
            "tm_checked": "checked" if cfg.get("timemachine", True) else "",
            "guest_checked": "checked" if cfg.get("guest_ok", True) else "",
            "auto_checked": "checked" if cfg.get("auto_mount", False) else "",
            "owner_options": _owner_options_html(owner, lang),
            "usb_drives_html": _usb_drives_html(lang),
            "mounted_html": _mounted_html(lang),
            "notice": notice,
        }
        tpl = open(os.path.join(os.path.dirname(__file__), "page.html"), "r", encoding="utf-8").read()
        return webapp.render_plugin_page(tpl, tokens, lang)

    if act == "umount_usb":
        dev = get("dev")
        ok, msg = (False, "未指定设备")
        if dev and MNT:
            ok, msg = MNT.umount_usb(dev)
        notice = f'<p class="badge {"ok" if ok else "err"}">{msg}</p>'
        cfg = MG.get_config("storage-share") if MG else {}
        owner = (cfg.get("share_owner") or "").strip()
        tokens = {
            "title": "存储共享" if lang == "zh" else "Storage Share",
            "active": "plugins",
            "share_path": cfg.get("share_path", "/srv/printbox-share"),
            "tm_checked": "checked" if cfg.get("timemachine", True) else "",
            "guest_checked": "checked" if cfg.get("guest_ok", True) else "",
            "auto_checked": "checked" if cfg.get("auto_mount", False) else "",
            "owner_options": _owner_options_html(owner, lang),
            "usb_drives_html": _usb_drives_html(lang),
            "mounted_html": _mounted_html(lang),
            "notice": notice,
        }
        tpl = open(os.path.join(os.path.dirname(__file__), "page.html"), "r", encoding="utf-8").read()
        return webapp.render_plugin_page(tpl, tokens, lang)

    # 保存配置
    cfg = {
        "share_path": get("share_path") or "/srv/printbox-share",
        "timemachine": "timemachine" in form,
        "guest_ok": "guest_ok" in form,
        "auto_mount": "auto_mount" in form,
        "share_owner": get("share_owner"),
    }
    if MG:
        MG.set_config("storage-share", cfg)
        install()
    notice = ("已保存并重新加载 Samba 配置。" if lang == "zh"
              else "Saved and reloaded Samba config.")
    owner = (cfg.get("share_owner") or "").strip()
    tokens = {
        "title": "存储共享" if lang == "zh" else "Storage Share",
        "active": "plugins",
        "share_path": cfg["share_path"],
        "tm_checked": "checked" if cfg["timemachine"] else "",
        "guest_checked": "checked" if cfg["guest_ok"] else "",
        "auto_checked": "checked" if cfg["auto_mount"] else "",
        "owner_options": _owner_options_html(owner, lang),
        "usb_drives_html": _usb_drives_html(lang),
        "mounted_html": _mounted_html(lang),
        "notice": notice,
    }
    tpl = open(os.path.join(os.path.dirname(__file__), "page.html"), "r", encoding="utf-8").read()
    return webapp.render_plugin_page(tpl, tokens, lang)
