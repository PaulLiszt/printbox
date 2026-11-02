"""PrintBox 插件清单（plugin.json）模型。

设计目标：不发明新格式，而是**直接架在既有生态之上**：
- native 插件：声明 apt_packages，底层复用 Debian 官方/社区包（samba、aria2…）。
- container 插件：声明 compose（docker-compose.yml），直接吃 CasaOS/Umbrel/runtipi 等
  应用商店现成镜像；compat 模块还能把它们的商店定义自动转成此清单。

清单字段（全部可选，未列出的视类型而定）：
  name                  str   唯一标识，仅 [a-z0-9-_]（也用作 systemd 单元前缀）
  display_name         {zh,en}
  version              str
  type                 "native" | "container"
  description          {zh,en}
  license              str
  required_capabilities  list[str]  宿主在启用前必须提示用户确认的能力，如
                                      ["root","apt-install","docker"]
  apt_packages         list[str]     native 专用：enable 时由宿主 apt-get install
  compose              str           container 专用：相对插件目录的 docker-compose.yml
  systemd_units       list[str]     宿主托管/启停的 systemd 单元名
  config_schema       dict          配置字段：field -> {type, default, desc:{zh,en}}
  web                 {route, template, title:{zh,en}}
  module              str           可选：相对插件目录的 python 模块（含生命周期钩子）
"""
import json
import os


def _norm_local(d, default_lang="zh"):
    """把 {zh,en} 或纯字符串统一成 {zh,en} 字典。"""
    if isinstance(d, str):
        return {default_lang: d, "en": d}
    if isinstance(d, dict) and ("zh" in d or "en" in d):
        return {"zh": d.get("zh", d.get("en", "")), "en": d.get("en", d.get("zh", ""))}
    return {"zh": str(d or ""), "en": str(d or "")}


class Manifest:
    def __init__(self, data, plugin_dir):
        self.raw = data
        self.dir = plugin_dir
        self.name = data["name"]
        self.display_name = _norm_local(data.get("display_name", self.name))
        self.version = str(data.get("version", "0.0.0"))
        self.type = data.get("type", "native")
        self.description = _norm_local(data.get("description", ""))
        self.license = data.get("license", "MIT")
        self.required_capabilities = list(data.get("required_capabilities", []))
        self.apt_packages = list(data.get("apt_packages", []))
        self.compose = data.get("compose")
        self.systemd_units = list(data.get("systemd_units", []))
        self.config_schema = data.get("config_schema", {})
        self.web = data.get("web", {})
        self.module = data.get("module")
        if self.type == "container" and not self.compose:
            raise ValueError(f"插件 {self.name}: container 类型必须声明 compose")

    @property
    def route(self):
        return self.web.get("route")

    @property
    def template(self):
        return self.web.get("template")

    @property
    def title(self):
        return _norm_local(self.web.get("title", self.display_name))

    def config_defaults(self):
        out = {}
        for field, spec in self.config_schema.items():
            out[field] = spec.get("default")
        return out

    def load_module(self):
        """加载插件的 python 模块（含 install/enable/disable/uninstall/handle_get/handle_post 钩子）。"""
        if not self.module:
            return None
        import importlib.util
        path = os.path.join(self.dir, self.module)
        if not os.path.exists(path):
            return None
        spec = importlib.util.spec_from_file_location(f"_pb_plugin_{self.name}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def as_dict(self):
        return dict(self.raw)


def load(plugin_dir):
    """从插件目录读取 plugin.json，返回 Manifest。"""
    path = os.path.join(plugin_dir, "plugin.json")
    with open(path, "r", encoding="utf-8") as f:
        return Manifest(json.load(f), plugin_dir)


def save(manifest, plugin_dir):
    """把清单写回插件目录（兼容层 import 时用）。"""
    path = os.path.join(plugin_dir, "plugin.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest.raw, f, indent=2, ensure_ascii=False)
