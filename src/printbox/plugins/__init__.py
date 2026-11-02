"""PrintBox 插件系统入口。

宿主只做「管理壳」：插件发现、启停、配置命名空间、Web 路由、生命周期托管。
真正的功能来自两条既有生态（宿主自动兼容）：
- native 插件 -> Debian apt 包（samba / aria2 / transmission…）
- container 插件 -> 现成 Docker 镜像（Home Assistant / Frigate / qBittorrent…），
  并可经 compat 模块直接 import CasaOS / Umbrel / runtipi 的 compose 定义。
"""
from . import manifest as manifest
from . import manager as manager
from . import compat as compat

# 对外暴露的关键符号
Manifest = manifest.Manifest
load_manifest = manifest.load
discover = manager.discover
list_plugins = manager.list_plugins
enable = manager.enable
disable = manager.disable
is_enabled = manager.is_enabled
get_config = manager.get_config
set_config = manager.set_config
register_routes = manager.register_routes
dispatch_get = manager.dispatch_get
dispatch_post = manager.dispatch_post
import_compose = compat.import_compose
PLUGIN_ROUTES = manager.PLUGIN_ROUTES


def init():
    """Web 启动时调用：发现插件并注册路由。"""
    manager.discover()
    manager.register_routes()
