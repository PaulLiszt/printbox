"""PrintBox 共享配置与日志库（pip 包 printbox）。

路径遵循 FHS：
- 配置  /etc/printbox
- 状态/归档 /var/lib/printbox
- 日志  /var/log/printbox

注意：网络参数、主机名、系统账号统一由底层 Linux 系统管理（与系统一致），
本包不再处理（原 PROVISION 一次性配置功能已移除）。
"""
import json
import os
import sys
import time

CONFIG_DIR = "/etc/printbox"
DATA_DIR = "/var/lib/printbox"
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
ARCHIVE_DIR = os.path.join(DATA_DIR, "archive")
STATE_DIR = os.path.join(DATA_DIR, "state")
LOG_DIR = "/var/log/printbox"
SPOOL = "/var/spool/cups"  # CUPS 默认 spool

# 模板/静态资源目录（包内），运行时由 webapp 计算
PKG_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_CONFIG = {
    # Web 界面语言：zh=中文（默认），en=英文。切换后写入本文件，重启不丢。
    # Web UI language: zh=Chinese (default), en=English. Persisted here, survives reboot.
    "lang": "zh",
    # 留空则运行时取系统主机名（仅用于 Web 展示 http://<host>.local）
    "hostname": "",
    # Web 管理界面是否要求登录（默认关闭，局域网内可直接访问）
    "require_auth": False,
    "admin_user": "admin",
    "admin_password": "",
    # 打印文件归档：按磁盘容量自动算配额，装满轮替删除最旧文件
    "archive": {
        "enabled": True,
        "dir": ARCHIVE_DIR,
        "ratio": 0.25,             # 归档占用磁盘比例（0~1）
        "min_mb": 2048,            # 最小配额（MB）
        "max_mb_cap": 30720,       # 最大配额（MB），默认 30G
        "max_size_mb": 0,          # 实际配额，由 init/Web 自动计算写入（0=未算）
    },
}


def ensure_dirs():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)


def load_config():
    """读取 /etc/printbox/config.json，与默认值深合并。"""
    cfg = _deep_copy(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                user = json.load(f)
            _deep_update(cfg, user)
        except Exception as e:
            log("config", "ERROR", f"读取配置失败，使用默认: {e}")
    return cfg


def save_config(cfg):
    """原子写配置：写临时文件后 rename，避免断电写到一半。"""
    ensure_dirs()
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_FILE)
    os.chmod(CONFIG_FILE, 0o600)


def _deep_copy(d):
    return json.loads(json.dumps(d))


def _deep_update(base, upd):
    for k, v in upd.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
    return base


def log(component, level, msg):
    """写组件日志到 /var/log/printbox/<component>.log，带时间戳。"""
    try:
        ensure_dirs()
        path = os.path.join(LOG_DIR, f"{component}.log")
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts} [{level}] {msg}\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def run(cmd, **kw):
    """封装 subprocess.run，返回 (rc, out, err)。支持 input= 传 stdin。"""
    import subprocess
    kw.setdefault("text", True)
    inp = kw.pop("input", None)
    try:
        p = subprocess.run(cmd, input=inp, capture_output=True, **kw)
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception as e:
        return -1, "", str(e)


def compute_archive_quota(total_bytes, ratio=0.25, min_mb=2048, cap_mb=30720):
    """按磁盘总字节数自动计算归档配额（MB）。quota = clamp(round(total*ratio/1MiB), min, cap)。"""
    try:
        q = int(total_bytes * float(ratio) / (1024 * 1024))
    except Exception:
        q = min_mb
    q = max(q, int(min_mb))
    q = min(q, int(cap_mb))
    return q


def archive_usage():
    """返回归档目录当前用量信息。"""
    cfg = load_config()
    a = cfg.get("archive", {})
    d = a.get("dir", ARCHIVE_DIR)
    max_mb = int(a.get("max_size_mb", 0) or 0)
    used = 0
    count = 0
    last = ""
    last_ts = 0
    try:
        for name in os.listdir(d):
            if name.endswith(".meta.json") or name == "state.json":
                continue
            fp = os.path.join(d, name)
            try:
                stt = os.stat(fp)
            except OSError:
                continue
            used += stt.st_size
            count += 1
            if stt.st_mtime > last_ts:
                last_ts = stt.st_mtime
                last = name
    except OSError:
        pass
    return {
        "dir": d,
        "enabled": bool(a.get("enabled", True)),
        "max_size_mb": max_mb,
        "used_mb": round(used / (1024 * 1024), 1),
        "count": count,
        "last_file": last,
    }


# 浏览器可直接内联渲染的常见格式（无需转换即可预览）
PREVIEW_EXTS = {
    "pdf", "png", "jpg", "jpeg", "gif", "bmp", "webp", "tif", "tiff",
    "txt", "html", "htm",
}
# 需经后端最佳努力转换为 PDF 才能预览的格式（依赖主机已安装相应工具）
CONVERTIBLE_EXTS = {"ps", "urf", "pwg"}


def _ext_of(filename):
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"


def list_archive_jobs(limit=500):
    """返回打印历史（按时间倒序）。字段含文件信息、是否可预览、能否再次打印。"""
    cfg = load_config()
    a = cfg.get("archive", {})
    d = a.get("dir", ARCHIVE_DIR)
    jobs = []
    try:
        for name in os.listdir(d):
            if not name.endswith(".meta.json"):
                continue
            fp = os.path.join(d, name)
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                continue
            base = name[:-len(".meta.json")]
            files = meta.get("files", [])
            if not files:
                files = [base + ".bin"]
            primary = files[0]
            ext = _ext_of(primary)
            total = 0
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(d, fn))
                except OSError:
                    pass
            is_pdf = ext == "pdf"
            can_preview = ext in PREVIEW_EXTS
            # 以文件 mtime 作为排序键：archived_at 仅到秒，同一秒内的批量作业时间戳相同、
            # 顺序会随 os.listdir 乱跳（用户反馈历史「乱的」即源于此）；mtime 带亚秒精度，
            # 保证严格按打印先后倒序（最新在最前）。
            try:
                mtime = os.path.getmtime(fp)
            except OSError:
                mtime = 0
            jobs.append({
                "archived_at": meta.get("archived_at", ""),
                "job_id": meta.get("job_id", ""),
                "printer": meta.get("printer", ""),
                "user": meta.get("user", ""),
                "job_name": meta.get("job_name", "") or primary,
                "format": meta.get("format", ""),
                "files": files,
                "primary": primary,
                "ext": ext,
                "total_bytes": total,
                "total_mb": round(total / (1024 * 1024), 2),
                "is_pdf": is_pdf,
                "can_preview": can_preview,
                "convertible": ext in CONVERTIBLE_EXTS,
                "mtime": mtime,
            })
    except OSError:
        pass
    jobs.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    return jobs[:limit]


def get_default_printer():
    """返回 CUPS 默认打印机名（无则返回空串）。"""
    rc, out, _ = run(["lpstat", "-d"])
    if rc == 0:
        import re
        m = re.search(r"system default destination:\s*(\S+)", out)
        if m:
            return m.group(1)
    return ""


if __name__ == "__main__":
    ensure_dirs()
    print(json.dumps(load_config(), indent=2, ensure_ascii=False))
