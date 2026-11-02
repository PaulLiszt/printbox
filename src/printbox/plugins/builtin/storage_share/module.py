"""storage-share 插件：native 路径 POC。

验证点：PrintBox 不写 Samba 应用本身，只写「配置片 + 托管 systemd」。
Time Machine 只是 smb.conf 里多几行 fruit 模块——纯配置，零重写。
"""
import os
import subprocess

try:
    from printbox.plugins import manager as MG
except Exception:  # 独立单元测试时 printbox 可能不在 path
    MG = None

SMB_DROPIN_DIR = "/etc/samba/smb.conf.d"
SMB_DROPIN = os.path.join(SMB_DROPIN_DIR, "printbox-share.conf")
SMB_MAIN = "/etc/samba/smb.conf"


def generate_smb_conf(cfg):
    """纯函数：根据配置生成共享段。Time Machine 开启时追加 fruit VFS 模块。"""
    path = cfg.get("share_path") or "/srv/printbox-share"
    tm = bool(cfg.get("timemachine", True))
    guest = "yes" if cfg.get("guest_ok", True) else "no"
    lines = [
        "[PrintBoxShare]",
        f"   path = {path}",
        "   browseable = yes",
        "   read only = no",
        f"   guest ok = {guest}",
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


def _ensure_include_line():
    """确保主 smb.conf 包含我们的 drop-in 目录（幂等）。"""
    if not os.path.exists(SMB_MAIN):
        return
    with open(SMB_MAIN, "r", encoding="utf-8") as f:
        content = f.read()
    marker = "include = %s/*.conf" % SMB_DROPIN_DIR
    if marker not in content:
        with open(SMB_MAIN, "a", encoding="utf-8") as f:
            f.write("\n# PrintBox storage-share\nglobal include = %s/*.conf\n" % SMB_DROPIN_DIR)


def install(manifest=None):
    """install 钩子（enable 时调用）：写配置片并重启 samba。仅在盒上执行。"""
    if not MG:
        return
    cfg = MG.get_config("storage-share")
    os.makedirs(SMB_DROPIN_DIR, exist_ok=True)
    os.makedirs(cfg.get("share_path", "/srv/printbox-share"), exist_ok=True)
    with open(SMB_DROPIN, "w", encoding="utf-8") as f:
        f.write(generate_smb_conf(cfg))
    _ensure_include_line()
    subprocess.run(["systemctl", "restart", "smbd", "nmbd"], capture_output=True, text=True)


def disable(manifest=None):
    if os.path.exists(SMB_DROPIN):
        os.remove(SMB_DROPIN)
    subprocess.run(["systemctl", "restart", "smbd", "nmbd"], capture_output=True, text=True)


def handle_get(handler, lang):
    from printbox import webapp
    cfg = MG.get_config("storage-share") if MG else {}
    tokens = {
        "title": "存储共享" if lang == "zh" else "Storage Share",
        "active": "plugins",
        "share_path": cfg.get("share_path", "/srv/printbox-share"),
        "tm_checked": "checked" if cfg.get("timemachine", True) else "",
        "guest_checked": "checked" if cfg.get("guest_ok", True) else "",
    }
    tpl = open(os.path.join(os.path.dirname(__file__), "page.html"), "r", encoding="utf-8").read()
    return webapp.render_plugin_page(tpl, tokens, lang)


def handle_post(handler, form, lang):
    from printbox import webapp
    cfg = {
        "share_path": (form.get("share_path") or ["/srv/printbox-share"])[0],
        "timemachine": "timemachine" in form,
        "guest_ok": "guest_ok" in form,
    }
    if MG:
        MG.set_config("storage-share", cfg)
        install()
    tpl = open(os.path.join(os.path.dirname(__file__), "page.html"), "r", encoding="utf-8").read()
    notice = ("已保存并重新加载 Samba 配置。" if lang == "zh"
              else "Saved and reloaded Samba config.")
    tokens = {
        "title": "存储共享" if lang == "zh" else "Storage Share",
        "active": "plugins",
        "share_path": cfg["share_path"],
        "tm_checked": "checked" if cfg["timemachine"] else "",
        "guest_checked": "checked" if cfg["guest_ok"] else "",
        "notice": notice,
    }
    return webapp.render_plugin_page(tpl, tokens, lang)
