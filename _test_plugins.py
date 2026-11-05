"""PrintBox 插件系统验证（开发机，不实际跑 apt/docker/samba）。

覆盖：
- manifest 加载 / 配置默认值 / web.route 属性
- storage-share.generate_smb_conf：Time Machine 开/关分别含/不含 fruit 模块
- homeassistant compose 解析：提取官方镜像
- compat.import_compose：把样例 CasaOS 应用转成 PrintBox 容器插件
- manager 路由注册 + dispatch_get 渲染（含中英切换）
"""
import os
import sys
import json
import tempfile

sys.path.insert(0, "src")
import printbox
import printbox.plugins.manager as MG
import printbox.plugins.compat as CP
from printbox.plugins.builtin.storage_share import module as SS
from printbox.plugins.manifest import load as load_manifest

BUILTIN = os.path.join(os.path.dirname(printbox.plugins.manager.__file__), "builtin")
PASS = []


def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)


# 1) manifest 加载 + 默认值 + route
ss_m = load_manifest(os.path.join(BUILTIN, "storage_share"))
ha_m = load_manifest(os.path.join(BUILTIN, "homeassistant"))
check("storage-share 清单加载", ss_m.name == "storage-share" and ss_m.type == "native")
check("homeassistant 清单加载", ha_m.name == "homeassistant" and ha_m.type == "container")
check("storage 配置默认值含 timemachine=True", ss_m.config_defaults().get("timemachine") is True)
check("homeassistant 有 web.route", ha_m.route == "/plugin/homeassistant")

# 2) generate_smb_conf：TM 开 -> fruit；TM 关 -> 无 fruit
conf_on = SS.generate_smb_conf({"share_path": "/mnt/usb", "timemachine": True, "guest_ok": True})
conf_off = SS.generate_smb_conf({"share_path": "/mnt/usb", "timemachine": False, "guest_ok": False})
check("TM 开启含 fruit:time machine", "fruit:time machine = yes" in conf_on)
check("TM 开启含 vfs objects fruit", "vfs objects = catia fruit streams_xattr" in conf_on)
check("TM 关闭不含 fruit", "fruit" not in conf_off)
check("guest ok=no 生效", "guest ok = no" in conf_off)
check("共享路径生效", "path = /mnt/usb" in conf_on)

# 3) homeassistant compose 镜像提取
extract = CP._extract_compose(os.path.join(BUILTIN, "homeassistant", "docker-compose.yml"))
check("HA compose 提取到官方镜像", extract["image"] == "homeassistant/home-assistant:stable")
check("HA compose 使用 host 网络", "network_mode: host" in
      open(os.path.join(BUILTIN, "homeassistant", "docker-compose.yml")).read())

# 4) compat.import_compose：样例 CasaOS 应用
casaos_compose = """
services:
  aria2:
    image: p3terx/aria2-pro:latest
    ports:
      - "6888:6888"
      - "6888:6888/udp"
    volumes:
      - ./data:/data
      - ./config:/config
"""
casaos_meta = {
    "name": "Aria2 Pro",
    "title": "Aria2 Pro 下载",
    "description": "高速下载工具",
    "port": 6888,
    "form_fields": [],
}
with tempfile.TemporaryDirectory() as d:
    cp = os.path.join(d, "docker-compose.yml")
    mp = os.path.join(d, "metadata.json")
    open(cp, "w").write(casaos_compose)
    open(mp, "w").write(json.dumps(casaos_meta))
    man = CP.import_compose(cp, mp)
check("import 产出 container 类型", man["type"] == "container")
check("import 归一化插件名", man["name"] == "aria2-pro")
check("import 需要 docker 权限", "docker" in man["required_capabilities"])
check("import 记录上游镜像", man["_import"]["upstream_image"] == "p3terx/aria2-pro:latest")
check("import 记录上游端口", "6888" in man["_import"]["upstream_ports"])
check("import web.route 正确", man["web"]["route"] == "/plugin/aria2-pro")

# 5) manager 路由注册 + dispatch_get 渲染（中英）
MG.discover()
# 直接让 is_enabled 返回 True，跳过真实 apt/docker/systemctl
MG.is_enabled = lambda name: True
MG.register_routes()
check("路由表含 storage-share", "/plugin/storage-share" in MG.PLUGIN_ROUTES)
check("路由表含 homeassistant", "/plugin/homeassistant" in MG.PLUGIN_ROUTES)


class FakeHandler:
    def __init__(self):
        self.sent = []

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        self.sent.append((code, body))

    def _redirect(self, loc):
        self.sent.append(("302", loc))


h = FakeHandler()
handled = MG.dispatch_get(h, "/plugins", "zh")
check("dispatch /plugins 命中", handled is True and h.sent and h.sent[0][0] == 200)
check("/plugins 页面含插件名", "存储共享" in h.sent[0][1])

h2 = FakeHandler()
MG.dispatch_get(h2, "/plugin/storage-share", "zh")
check("storage 中文页含共享目录", "共享目录" in h2.sent[0][1])

h3 = FakeHandler()
MG.dispatch_get(h3, "/plugin/storage-share", "en")
check("storage 英文页含 Share directory", "Share directory" in h3.sent[0][1])

h4 = FakeHandler()
missed = MG.dispatch_get(h4, "/plugin/does-not-exist", "zh")
check("未注册路由不命中", missed is False and not h4.sent)

# 6) 第三方导入：本地 compose + 落盘 + URL 导入（mock 下载）
import shutil as _shutil
with tempfile.TemporaryDirectory() as d:
    # 落盘目标指向临时目录，避免污染真实 /etc/printbox/plugins.d
    orig_d = MG.PLUGINS_D
    MG.PLUGINS_D = os.path.join(d, "plugins.d")
    try:
        comp = os.path.join(d, "docker-compose.yml")
        open(comp, "w").write(casaos_compose)
        meta = os.path.join(d, "metadata.json")
        open(meta, "w").write(json.dumps(casaos_meta))
        pname, dest = MG.import_plugin(comp, meta, name="aria2-test")
        check("import_plugin 落盘 plugin.json",
              os.path.exists(os.path.join(dest, "plugin.json")))
        check("import_plugin 复制 compose",
              os.path.exists(os.path.join(dest, "docker-compose.yml")))
        man = json.load(open(os.path.join(dest, "plugin.json")))
        check("import_plugin 记录 compose 文件名", man["compose"] == "docker-compose.yml")
        check("import_plugin 返回归一化名", pname == "aria2-test")

        # URL 导入：mock _download 把本地 fixture 当远程写入
        fixture_comp = os.path.join(d, "remote-compose.yml")
        open(fixture_comp, "w").write(casaos_compose)
        fixture_meta = os.path.join(d, "remote-meta.json")
        open(fixture_meta, "w").write(json.dumps(casaos_meta))

        def fake_dl(url, dest_path, timeout=30):
            if url.endswith("meta"):
                _shutil.copy(fixture_meta, dest_path)
            else:
                _shutil.copy(fixture_comp, dest_path)
        MG._download = fake_dl
        pname2, dest2 = MG.import_plugin_from_url("http://x/docker-compose.yml",
                                                  "http://x/metadata.json", name="aria2-url")
        check("import_plugin_from_url 落盘",
              os.path.exists(os.path.join(dest2, "plugin.json")))
        check("import_plugin_from_url 返回名", pname2 == "aria2-url")
    finally:
        MG.PLUGINS_D = orig_d

# 汇总
failed = [n for n, ok in PASS if not ok]
print("\n==== 结果 ====")
print(f"通过 {len(PASS) - len(failed)}/{len(PASS)}")
if failed:
    print("失败项:", failed)
    sys.exit(1)
print("全部通过。")
