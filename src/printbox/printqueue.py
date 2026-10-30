"""打印队列实时查询与作业管理（CUPS / lpstat / cancel）。

纯标准库，封装对 CUPS 调度器的“只读查询”与“受控写操作”：
  - list_jobs()        列出当前活动 / 排队打印作业（实时队列）
  - printer_states()   列出各打印机状态（idle / printing / disabled）
  - cups_running()     CUPS 调度器是否在运行
  - cancel_job(raw)    取消单个作业
  - cancel_all(name)   取消某打印机（或所有打印机）的剩余作业
  - restart_cups()     重启 CUPS 打印服务（非整机重启）

所有写操作都对作业标识 / 打印机名做严格的白名单校验（仅允许
CUPS 合法字符），并以列表形式调用子进程，杜绝命令注入。
"""
import datetime
import re

from . import config as C

# 作业标识 / 打印机名白名单：仅允许 CUPS 合法字符
_SAFE = re.compile(r"^[A-Za-z0-9_.\-]+$")
# 提交时间令牌形如 HH:MM 或 HH:MM:SS
_TIME = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def _split_time(tok):
    p = tok.split(":")
    hh = p[0]
    mm = p[1] if len(p) > 1 else "00"
    ss = p[2] if len(p) > 2 else "00"
    return hh, mm, ss


def _fmt_job_time(tokens, time_tok):
    """把 lpstat -o 的日期/时间碎片拼成 `YYYY-MM-DD HH:MM:SS`（24h）。

    lpstat 输出顺序因版本/是否跨年而异：日期可能在时间前，4 位年份也可能在时间后，
    故统一从所有非时间令牌里抽取「月/日/年」，不再依赖固定顺序。"""
    mon = day = None
    year = str(datetime.datetime.now().year)
    for tok in tokens:
        t = tok.strip(",")
        if t in _MONTHS and mon is None:
            mon = _MONTHS[t]
        elif re.fullmatch(r"\d{4}", t):
            year = t
        elif t.isdigit() and 1 <= int(t) <= 31 and day is None:
            day = int(t)
    if mon is None or day is None:
        return " ".join(tokens + [time_tok])
    hh, mm, ss = _split_time(time_tok)
    return f"{int(year):04d}-{mon:02d}-{day:02d} {hh}:{mm}:{ss}"


def _safe(value):
    return bool(value) and _SAFE.match(value) is not None


def _human(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def _parse_job_line(line):
    """解析 `lpstat -o` 的一行： dest-jobid user size date time job-name。"""
    parts = line.split()
    if len(parts) < 3:
        return None
    dest_job = parts[0]                 # 形如 hp_laserjet_p1505n-4
    if not _safe(dest_job):
        return None
    user = parts[1]
    try:
        size = int(parts[2])
    except ValueError:
        size = 0
    rest = parts[3:]
    # 在 rest 中定位时间令牌（HH:MM[:SS]）：其前是日期，其后是作业名
    ti = None
    for i, t in enumerate(rest):
        if _TIME.match(t):
            ti = i
            break
    if ti is None:
        submitted = ""
        name = " ".join(rest)
    else:
        # 时间令牌之外所有字段（日期可能在时间前，年份也可能在时间后）一起参与解析
        non_time = rest[:ti] + rest[ti + 1:]
        submitted = _fmt_job_time(non_time, rest[ti])
        name = " ".join(rest[ti + 1:])
        # 个别 lpstat 版本把 4 位年份放在时间之后，会混入作业名，清掉开头的年份
        name = re.sub(r"^\d{4}\s+", "", name)
    if "-" in dest_job:
        printer, _, jobnum = dest_job.rpartition("-")
    else:
        printer, jobnum = dest_job, ""
    return {
        "raw": dest_job,                # 用于 cancel 的完整标识
        "printer": printer,
        "job_id": jobnum,
        "user": user,
        "size": size,
        "size_h": _human(size),
        "submitted": submitted,
        "name": name or "(未命名)",
    }


def list_jobs():
    """返回当前活动 / 排队打印作业列表（实时队列）。"""
    rc, out, _ = C.run(["lpstat", "-o"])
    jobs = []
    if rc == 0 and out:
        for ln in out.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            j = _parse_job_line(ln)
            if j:
                jobs.append(j)
    return jobs


def printer_states():
    """返回 {打印机名: {state, enabled, summary}}。"""
    states = {}
    rc, out, _ = C.run(["lpstat", "-p"])
    if rc == 0 and out:
        for ln in out.splitlines():
            m = re.match(
                r"^printer\s+(\S+)\s+is\s+(\w+)\.?\s*(enabled|disabled)?\s*(.*)$",
                ln)
            if not m:
                continue
            name = m.group(1)
            state = m.group(2)
            enabled = (m.group(3) or "enabled") == "enabled"
            states[name] = {
                "state": state,
                "enabled": enabled,
                "summary": (m.group(4) or "").strip(),
            }
    return states


def cups_running():
    rc, out, _ = C.run(["lpstat", "-r"])
    return "running" in (out or "").lower()


def cancel_job(raw):
    if not _safe(raw):
        return False, "非法作业标识"
    rc, _, err = C.run(["cancel", raw], timeout=10)
    if rc == 0:
        return True, f"已取消作业 {raw}"
    return False, (err or "取消失败").strip()


def cancel_all(printer=None):
    cmd = ["cancel", "-a"]
    if printer:
        if not _safe(printer):
            return False, "非法打印机名"
        cmd.append(printer)
    rc, _, err = C.run(cmd, timeout=15)
    if rc == 0:
        return True, "已取消全部打印任务" + (f"（{printer}）" if printer else "")
    return False, (err or "取消失败").strip()


def restart_cups():
    rc, _, err = C.run(["systemctl", "restart", "cups"])
    if rc == 0:
        return True, "已重启打印服务（CUPS）"
    return False, (err or "重启失败").strip()
