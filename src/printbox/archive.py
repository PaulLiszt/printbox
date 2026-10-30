"""PrintBox 打印文件归档（自动配额 + 环型轮替）。

- 每次打印的原始文件都保存下来，便于追溯。
- 归档空间按磁盘容量自动计算（见 config.compute_archive_quota）。
- 装满后自动删除最旧的文件（轮替），不会无限增长。

依赖 CUPS 的 PreserveJobFiles（由 printers.enable_job_archive 开启）。
"""
import os
import re
import sys
import time
import json
import shutil

from . import config as C

SPOOL = C.SPOOL
POLL_INTERVAL = 15          # 秒
EXT_MAP = {
    "application/pdf": "pdf",
    "application/postscript": "ps",
    "application/vnd.cups-postscript": "ps",
    "image/urf": "urf",
    "image/pwg-raster": "pwg",
    "text/plain": "txt",
    "text/html": "html",
    "application/octet-stream": "bin",
}


def ensure_preserve():
    # PreserveJobFiles 保留原始文件；PreserveJobHistory 让已完成作业在 lpstat -W completed 中
    # 保留更久，避免刚打印完、归档轮询还没跑到时作业就从历史里消失（表现为“历史只有旧记录”）。
    # 0.2.48：改为直接写 cupsd.conf（幂等 no-op），不再调用 cupsctl 做 live reload——
    # 对 PreserveJobFiles/PreserveJobHistory 的 live 重载会让 cupsd 立即崩溃重启、重跑作业。
    from . import printers
    try:
        printers.enable_job_archive()
    except Exception:
        pass


def list_completed_jobs():
    """返回已完成作业列表 [{id, printer, user, name, format}]。"""
    jobs = {}
    rc, out, _ = C.run(["lpstat", "-W", "completed", "-o"])
    for line in out.splitlines():
        line = line.strip()
        m = re.match(r"^(\S+?)-(\d+)\s+(\S+)\s+(\d+)\s+(.*)$", line)
        if m:
            prn, jid, user, _size, rest = m.groups()
            jobs[int(jid)] = {
                "id": int(jid), "printer": prn, "user": user,
                "name": rest.strip(), "format": "",
            }
    rc, out2, _ = C.run(["lpstat", "-l", "-W", "completed", "-o"])
    cur = None
    for line in out2.splitlines():
        if re.match(r"^\S+?-\d+\s", line):
            mm = re.match(r"^(\S+?)-(\d+)", line)
            cur = int(mm.group(2)) if mm else None
        elif cur is not None and "document-format" in line:
            parts = line.split(None, 1)
            if len(parts) == 2:
                jobs.get(cur, {})["format"] = parts[1].strip()
    return list(jobs.values())


def load_state(d):
    sp = os.path.join(d, "state.json")
    if os.path.exists(sp):
        try:
            with open(sp) as f:
                return set(json.load(f).get("processed", []))
        except Exception:
            return set()
    return set()


def save_state(d, processed):
    sp = os.path.join(d, "state.json")
    tmp = sp + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"processed": sorted(processed)}, f)
    os.replace(tmp, sp)


def sanitize(s, maxlen=40):
    s = re.sub(r"[^\w\-_. ]", "_", s or "job").strip() or "job"
    return s[:maxlen]


def ext_for(format_str):
    return EXT_MAP.get((format_str or "").lower(), "bin")


def archive_one(job, d, processed):
    jid = job["id"]
    # CUPS spool 数据文件名是 5 位零填充（如 d00012），不能用裸 d{jid} 匹配；
    # 用 ^d0*{jid} 兼容任意前导零（d12 / d0012 / d00012 都能命中）。
    pat = re.compile(rf"^d0*{jid}(?:-\d+)?$")
    matches = sorted(p for p in os.listdir(SPOOL) if pat.match(p))
    ts = time.strftime("%Y%m%d-%H%M%S")
    user = sanitize(job["user"], 20)
    name = sanitize(job["name"])
    ext = ext_for(job.get("format"))
    base = f"{ts}_{jid}_{user}_{name}"
    saved = []
    if matches:
        for i, fn in enumerate(matches):
            src = os.path.join(SPOOL, fn)
            suffix = f".{i+1}" if len(matches) > 1 else ""
            dst = os.path.join(d, f"{base}{suffix}.{ext}")
            try:
                shutil.copy2(src, dst)
                saved.append(dst)
            except Exception as e:
                C.log("archive", "WARN", f"复制作业 {jid} 文件失败: {e}")
    # 即便 spool 文件已不在（CUPS 清理/未开启保留），也写一条历史元数据包，
    # 保证“最近打印”稳定出现在历史里（仅无原文件可下载）。
    meta = {
        "job_id": jid,
        "printer": job.get("printer"),
        "user": job.get("user"),
        "job_name": job.get("name"),
        "format": job.get("format"),
        "archived_at": ts,
        "files": [os.path.basename(x) for x in saved],
        "no_file": len(saved) == 0,
    }
    with open(os.path.join(d, base + ".meta.json"), "w") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    if saved:
        C.log("archive", "INFO", f"已归档作业 {jid} ({job.get('name')}) -> {len(saved)} 个文件")
    else:
        C.log("archive", "DEBUG",
              f"作业 {jid} 无 spool 原文件（已记历史元数据；可能 PreserveJobFiles 未生效）")
    processed.add(jid)
    return True


def enforce_quota(d, max_bytes):
    if max_bytes <= 0:
        return
    files = []
    total = 0
    for name in os.listdir(d):
        if name.endswith(".meta.json") or name == "state.json":
            continue
        fp = os.path.join(d, name)
        try:
            stt = os.stat(fp)
        except OSError:
            continue
        files.append((stt.st_mtime, fp))
        total += stt.st_size
    if total <= max_bytes:
        return
    files.sort()
    C.log("archive", "INFO",
          f"归档超出配额 ({total/1024/1024:.1f}/{max_bytes/1024/1024:.1f} MB)，开始轮替删除最旧文件")
    for mtime, fp in files:
        if total <= max_bytes:
            break
        try:
            sz = os.path.getsize(fp)
            os.remove(fp)
            total -= sz
            meta = fp[:-4] + ".meta.json" if not fp.endswith(".meta.json") else fp
            if os.path.exists(meta):
                os.remove(meta)
            C.log("archive", "INFO", f"轮替删除: {os.path.basename(fp)}")
        except OSError:
            pass


def run_once(dry=False):
    cfg = C.load_config()
    a = cfg.get("archive", {})
    if not a.get("enabled", True):
        return
    d = a.get("dir", C.ARCHIVE_DIR)
    os.makedirs(d, exist_ok=True)
    ensure_preserve()
    max_mb = int(a.get("max_size_mb", 0) or 0)
    if max_mb <= 0:
        try:
            st = os.statvfs("/")
            total = st.f_frsize * st.f_blocks
            max_mb = C.compute_archive_quota(total)
            a["max_size_mb"] = max_mb
            C.save_config(cfg)
        except Exception:
            max_mb = 2048
    max_bytes = max_mb * 1024 * 1024

    processed = load_state(d)
    jobs = list_completed_jobs()
    new_count = 0
    for job in jobs:
        if job["id"] in processed:
            continue
        if archive_one(job, d, processed):
            new_count += 1
        else:
            C.log("archive", "DEBUG",
                  f"已完成作业 {job['id']} 未在 spool 找到原始数据文件"
                  f"（可能 PreserveJobFiles 关闭、或文件已被 CUPS 清理）")
    if new_count:
        save_state(d, processed)
    if not dry:
        enforce_quota(d, max_bytes)


def main():
    C.ensure_dirs()
    if "--once" in sys.argv:
        run_once()
        return 0
    C.log("archive", "INFO", "打印归档守护进程启动")
    while True:
        try:
            run_once()
        except Exception as e:
            C.log("archive", "ERROR", f"归档循环异常: {e}")
        time.sleep(POLL_INTERVAL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
