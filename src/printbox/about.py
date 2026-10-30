"""PrintBox 关于/发布页内容。

优先读取 /var/lib/printbox/about.json（可由管理员就地编辑，无需重装），
回退到打包的 data/about.json。WebUI 的“关于”页渲染此内容；
用户后续新增的版本功能、改进内容、发布页面链接都放在这个 JSON 里即可。
"""
import json
import os

from . import config as C
from . import __version__


def _pkg_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "about.json")


def load_about(lang="zh"):
    # 英文版：优先读取打包的 about_en.json（若存在），管理员在设备上就地编辑仍以中文 about.json 为准
    if lang == "en":
        en_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "about_en.json")
        if os.path.exists(en_path):
            try:
                with open(en_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data.setdefault("version", __version__)
                return data
            except Exception:
                pass
    state_path = os.path.join(C.STATE_DIR, "about.json")
    for p in (state_path, _pkg_path()):
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data.setdefault("version", __version__)
                return data
            except Exception:
                continue
    return {
        "project": "PrintBox",
        "tagline": "把一台精简 Linux 主机变成无头打印服务器",
        "description": "",
        "homepage": "",
        "releases_url": "",
        "features": [],
        "changelog": [],
        "version": __version__,
    }


def save_about(data):
    """就地保存（管理员可在设备上改关于页内容）。"""
    path = os.path.join(C.STATE_DIR, "about.json")
    C.ensure_dirs()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
