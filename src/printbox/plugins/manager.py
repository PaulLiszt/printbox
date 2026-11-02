"""PrintBox 插件管理器：发现、启停、配置命名空间、路由注册、生命周期。

宿主只负责「管理壳」，真正的应用本体是：
- native 插件 -> Debian apt 包（samba/aria2…），宿主只写配置 + 托管 systemd；
- container 插件 -> 现成 Docker 镜像（Home Assistant/Frigate…），宿主只写 compose + 托管 systemd。

信任模型：插件以 root 运行、可装系统包、可管服务。宿主在 enable 第三方插件前必须
显式列出 required_capabilities 让用户确认（CLI 里落实），装别人插件 = 给 root。
"""
import os
import json
import subprocess

from . import manifest as M
from .. import config as C

BUILTIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "builtin")
# 第三方插件安装目录（用户 import / 手动放置）
PLUGINS_D = "/etc/printbox/plugins.d"

# 路由表：route -> (Manifest, module_or_None)。由 register_routes() 在启动时填充。
PLUGIN_ROUTES = {}
# 已加载的插件清单：name -> Manifest
LOADED = {}


def _plugin_state(cfg, name):
    return (cfg.get("plugins", {}) or {}).get(name, {})


def is_enabled(name):
    cfg = C.load_config()
    return bool(_plugin_state(cfg, name).get("enabled", False))


def get_config(name):
    """读取插件配置：清单默认值 深合并 已保存的用户配置。"""
    m = LOADED.get(name)
    if not m:
        return {}
    cfg = C.load_config()
    saved = _plugin_state(cfg, name).get("config", {})
    out = m.config_defaults()
    out.update(saved or {})
    return out


def set_config(name, data):
    cfg = C.load_config()
    cfg.setdefault("plugins", {}).setdefault(name, {})["config"] = data
    C.save_config(cfg)


def discover():
    """扫描内置 + 第三方目录，返回 Manifest 列表（已去重，name 唯一）。"""
    found = {}
    for base in (BUILTIN_DIR, PLUGINS_D):
        if not os.path.isdir(base):
            continue
        for entry in sorted(os.listdir(base)):
            d = os.path.join(base, entry)
            if os.path.isdir(d) and os.path.exists(os.path.join(d, "plugin.json")):
                try:
                    m = M.load(d)
                    found[m.name] = m
                except Exception as e:
                    C.log("plugins", "WARN", f"跳过无效插件 {d}: {e}")
    LOADED.clear()
    LOADED.update(found)
    return list(found.values())


def list_plugins():
    out = []
    for m in discover():
        out.append({
            "name": m.name,
            "display_name": m.display_name,
            "type": m.type,
            "enabled": is_enabled(m.name),
            "route": m.route,
            "required_capabilities": m.required_capabilities,
        })
    return out


def _apt_install(pkgs):
    if not pkgs:
        return 0
    C.log("plugins", "INFO", f"apt 安装插件依赖: {pkgs}")
    r = subprocess.run(["apt-get", "install", "-y"] + list(pkgs),
                       capture_output=True, text=True)
    if r.returncode != 0:
        C.log("plugins", "ERROR", f"apt 安装失败: {r.stderr[-500:]}")
    return r.returncode


def _systemctl(action, units):
    for u in units:
        subprocess.run(["systemctl", action, u], capture_output=True, text=True)


def enable(name, confirm_capabilities=None):
    """启用插件：装依赖（native）-> 调模块 install 钩子 -> 标记 enabled -> 注册路由。

    confirm_capabilities：调用方（CLI）传已确认的权限列表；若插件要求的权限超出已确认
    集合则拒绝（信任模型）。
    """
    m = LOADED.get(name) or discover() and next((x for x in discover() if x.name == name), None)
    if not m:
        raise ValueError(f"未知插件: {name}")
    needed = set(m.required_capabilities)
    confirmed = set(confirm_capabilities or [])
    if needed - confirmed:
        raise PermissionError(
            f"插件 {name} 需要权限 {sorted(needed - confirmed)}，未确认，拒绝启用")
    if m.type == "native":
        _apt_install(m.apt_packages)
    mod = m.load_module()
    if mod and hasattr(mod, "install"):
        mod.install(m)
    cfg = C.load_config()
    cfg.setdefault("plugins", {}).setdefault(name, {})["enabled"] = True
    C.save_config(cfg)
    if m.type == "container":
        _write_container_unit(m)
        _systemctl("enable", [f"printbox-plugin-{m.name}.service"])
    register_routes()
    return True


def disable(name):
    m = LOADED.get(name) or next((x for x in discover() if x.name == name), None)
    if not m:
        raise ValueError(f"未知插件: {name}")
    if m.type == "container":
        _systemctl("stop", [f"printbox-plugin-{m.name}.service"])
        _systemctl("disable", [f"printbox-plugin-{m.name}.service"])
    mod = m.load_module()
    if mod and hasattr(mod, "disable"):
        mod.disable(m)
    cfg = C.load_config()
    st = cfg.setdefault("plugins", {}).setdefault(name, {})
    st["enabled"] = False
    C.save_config(cfg)
    register_routes()
    return True


def _write_container_unit(m):
    """为 container 插件写一个 systemd 单元：开机自动 docker compose up -d。"""
    compose_path = os.path.join(m.dir, m.compose)
    unit = f"""[Unit]
Description=PrintBox plugin {m.name} (docker compose)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory={m.dir}
ExecStart=/usr/bin/docker compose -f {compose_path} up -d
ExecStop=/usr/bin/docker compose -f {compose_path} down
StandardOutput=journal

[Install]
WantedBy=multi-user.target
"""
    path = f"/etc/systemd/system/printbox-plugin-{m.name}.service"
    with open(path, "w", encoding="utf-8") as f:
        f.write(unit)
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True, text=True)


def register_routes():
    """把所有「已启用且有 web.route」的插件填进 PLUGIN_ROUTES。"""
    PLUGIN_ROUTES.clear()
    for m in discover():
        if is_enabled(m.name) and m.route:
            PLUGIN_ROUTES[m.route] = (m, m.load_module())


def dispatch_get(handler, path, lang):
    """在 _do_get 开头调用：命中插件路由则渲染并返回 True。"""
    if path == "/plugins":
        from . import views
        handler._send(200, views.index(handler, lang))
        return True
    if path in PLUGIN_ROUTES:
        m, mod = PLUGIN_ROUTES[path]
        if mod and hasattr(mod, "handle_get"):
            html = mod.handle_get(handler, lang)
            handler._send(200, html)
        else:
            from . import views
            handler._send(200, views.plugin_page(handler, m, lang))
        return True
    return False


def dispatch_post(handler, path, lang, form):
    """在 _do_post 开头调用：命中插件路由则处理并返回 True。"""
    if path in PLUGIN_ROUTES:
        m, mod = PLUGIN_ROUTES[path]
        if mod and hasattr(mod, "handle_post"):
            html = mod.handle_post(handler, form, lang)
            handler._send(200, html)
        else:
            handler._send(400, "<h1>400</h1><p>该插件不支持 POST。</p>")
        return True
    return False
