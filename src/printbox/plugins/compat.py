"""兼容层：把第三方应用商店的 docker-compose 定义自动转成 PrintBox 插件清单。

支持来源（格式大同小异，都是 docker-compose + 一段元数据）：
- CasaOS  AppStore：docker-compose.yml + metadata.json（含 form_fields / port_map / image）
- Umbrel   apps：docker-compose.yml + umbrel-app.yml（含 name / port）
- runtipi      ：docker-compose.yml + config.json（含 name / port）
- 通用 compose：只有 docker-compose.yml 也能转（尽力提取首个 service 的 image/ports/volumes）

转换产物是一个 PrintBox plugin.json 字典（type=container）。真正省时间的地方：
第三方已经写好了应用本体 + 配置，我们只要写「管理壳」（systemd 托管 + Web 链接）。
"""
import os
import json
import re


def _norm_name(raw):
    """CasaOS/Umbrel 的应用名可能含空格/大写，归一成 PrintBox 的 [a-z0-9-_] 标识。"""
    s = raw.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "app"


def _read_json(path):
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _extract_compose(compose_path):
    """读取 compose，提取首个 service 的 image / ports / volumes / container_name。"""
    with open(compose_path, "r", encoding="utf-8") as f:
        text = f.read()
    # 轻量解析：避免引入 yaml 依赖（开发机/盒上可能未装）。只取关键字段。
    services = {}
    # 找 services: 下的第一个 service 块（缩进两层）
    m = re.search(r"services:\s*\n\s{2}([a-zA-Z0-9_-]+):", text)
    svc = m.group(1) if m else None
    image = None
    ports = []
    volumes = []
    if svc:
        # 截取该 service 块（到下一个同级 service 或文档结束）
        start = m.end()
        nxt = re.search(r"\n\s{2}[a-zA-Z0-9_-]+:", text[start:])
        block = text[start: start + (nxt.start() if nxt else len(text))]
        im = re.search(r"image:\s*([^\n#]+)", block)
        if im:
            image = im.group(1).strip().strip('"\'')
        ports = re.findall(r"-\s*\"?(\d+):\d+\"?", block)
        volumes = re.findall(r"-\s*\"?([^:\n]+):/[^:\n]+:?rw\"?", block)
    return {"service": svc, "image": image, "ports": ports, "volumes": volumes}


def _detect_source(meta):
    if "form_fields" in meta or "image" in meta:
        return "casaos"
    if "port" in meta and ("name" in meta):
        return "umbrel_or_runtipi"
    return "generic"


def import_compose(compose_path, metadata_path=None, name=None, display_name=None,
                   description=None):
    """把第三方 compose + 元数据转成 PrintBox 插件清单字典。

    返回 dict（可直接 json.dump 成 plugin.json）。纯函数、无副作用，便于单元测试。
    """
    compose = _extract_compose(compose_path)
    meta = _read_json(metadata_path)
    src = _detect_source(meta)

    raw_name = name or meta.get("name") or meta.get("title") or compose.get("image") or "app"
    pname = _norm_name(raw_name)
    disp = display_name or meta.get("title") or meta.get("name") or raw_name
    desc = description or meta.get("description") or meta.get("short_desc") or f"{disp} (imported from {src})"

    # 端口：元数据优先，其次 compose 提取
    ports = meta.get("port") or meta.get("ports") or compose.get("ports")
    if isinstance(ports, int):
        ports = [str(ports)]
    elif isinstance(ports, list):
        ports = [str(p) for p in ports]
    else:
        ports = ports or []

    # 归一化 compose 文件名（兼容层会把原 compose 复制到插件目录，这里只记录文件名）
    compose_file = os.path.basename(compose_path)

    manifest = {
        "name": pname,
        "display_name": {"zh": disp, "en": disp},
        "version": "0.1.0",
        "type": "container",
        "description": {"zh": desc, "en": desc},
        "license": "see upstream",
        "required_capabilities": ["root", "docker"],
        "compose": compose_file,
        "systemd_units": [f"printbox-plugin-{pname}.service"],
        "web": {
            "route": f"/plugin/{pname}",
            "template": "page.html",
            "title": {"zh": disp, "en": disp},
        },
        # 兼容层附加的结构化信息，供宿主 UI / 调试使用（不影响生命周期）
        "_import": {
            "source": src,
            "upstream_image": compose.get("image"),
            "upstream_ports": ports,
            "upstream_volumes": compose.get("volumes"),
        },
    }
    return manifest
