"""homeassistant 插件：container 路径 POC。

验证点：PrintBox 不写智能家居逻辑，直接包官方 Home Assistant 容器。
生命周期由宿主经 systemd 单元（docker compose up/down）托管——应用本体 100% 复用上游。
"""
import os
import subprocess

try:
    from printbox.plugins import manager as MG
except Exception:
    MG = None

HA_PORT = 8123


def handle_get(handler, lang):
    from printbox import webapp
    cfg = MG.get_config("homeassistant") if MG else {}
    host = cfg.get("host") or "printbox.local"
    url = f"http://{host}:{HA_PORT}"
    tokens = {
        "title": "智能家居" if lang == "zh" else "Smart Home",
        "active": "plugins",
        "ha_url": url,
        "ha_port": str(HA_PORT),
    }
    tpl = open(os.path.join(os.path.dirname(__file__), "page.html"), "r", encoding="utf-8").read()
    return webapp.render_plugin_page(tpl, tokens, lang)


def handle_post(handler, form, lang):
    # 容器插件暂无表单配置；保留钩子以便将来扩展（如映射额外端口）。
    return handle_get(handler, lang)


def _running():
    r = subprocess.run(["docker", "ps", "--filter",
                       "name=printbox-homeassistant", "--format", "{{.Names}}"],
                      capture_output=True, text=True)
    return "printbox-homeassistant" in r.stdout
