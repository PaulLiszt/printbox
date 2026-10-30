"""PrintBox 打印机自动探测与添加。

通用、与具体硬件/发行版无关的探测逻辑：
  - 通过 `lpinfo -l -v` 枚举 CUPS 可见设备（USB / IPP-over-USB / 网络打印机等）；
  - 正确解析 URI 与 device-id 中的型号（兼容带/不带引号、有无 `uri =` 前缀）；
  - 同一物理打印机可能暴露多个 URI（如 `hp:/usb/...` 与 `usb://...`），按机型去重只加一个；
  - 按型号归一化匹配驱动（兼容下划线/连字符/空格差异），driverless/IPP 设备用 CUPS
    内置 `everywhere`，最终兜底 `raw` 保证至少能进队列。
CUPS + Avahi（显式 AirPrint 服务文件，见 write_airprint_service）负责把打印机以
AirPrint/IPP 形式发布到局域网；cups-browsed 默认不启用（它是订阅风暴根因，且服务器
无需浏览远端打印机）。
"""
import os
import re
import sys
import time
import shutil
import glob
import gzip
import tempfile
import xml.parsers.expat
import subprocess

from . import config as C
from . import drivers

# 模块级缓存：AirPrint 可见性检查结果（避免页面/定时任务频繁触发 avahi-browse）
_AV_CACHE = {"ts": 0.0, "val": None}


def _xml_esc(s):
    """对写入 Avahi 服务 XML 的文本做转义，避免打印机型号含 & < > " 等字符导致 XML 不合法、
    被 avahi 以『mismatched tag』拒绝加载。"""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _norm(s):
    """归一化：小写并把非字母数字折叠为单空格。
    用于跨机型鲁棒匹配（兼容 'LaserJet P1505n' 与 'hp-laserjet_p1505n-...' 这类差异）。"""
    return re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).strip()


def _slug(s):
    """由型号生成合法且稳定的 CUPS 打印机名（仅 [a-z0-9_-]）。"""
    s = _norm(s).replace(' ', '_')
    s = re.sub(r'[^a-z0-9_-]', '', s)
    return s or 'print'


def get_devices():
    """返回 [(uri, mdl)]，mdl 取自 device-id。

    双路探测以提高兼容性：
      1) 优先 `lpinfo -l -v`（verbose，含 device-id/型号）；
      2) 若为空则回退 `lpinfo -v`（非 verbose，每行 `<class> <uri>`）。
    """
    devs = _parse_verbose()
    if not devs:
        devs = _parse_simple()
    return devs


def _is_self_dnssd(uri, existing):
    """判断一个设备 URI 是否「本机已发布队列经 DNS-SD 被 CUPS 自身发现回来」的自引用设备。

    本机写出的 AirPrint 广播会让 CUPS 的 dnssd 后端把已存在的本地队列当成新设备
    `ipp://<queue>._ipp._tcp.local/`（或 dnssd://...）重新列进 `lpinfo -v`。detect 若把它当
    新打印机去 add_printer，会因为 IPP 优先级最高而抢在真实 USB 设备之前被选中，而 IPP-everywhere
    又往往建不出 PPD → 回退 raw → 造出 `<queue>_printbox` 双后缀 raw 队列、甚至把正确队列覆盖成
    raw，表现为「队列反复被改 / 出不了纸」。这里在 detect 阶段直接跳过这类自引用设备。"""
    u = (uri or "").lower()
    if not (u.startswith("ipp://") or u.startswith("ipps://") or u.startswith("dnssd://")):
        return False
    # 形如 ipp://NAME._ipp._tcp.local/ 或 dnssd://NAME._ipp._tcp.local./...
    host = u.split("://", 1)[1].split("/")[0]  # NAME._ipp._tcp.local
    marker = "._ipp._tcp.local"
    if marker not in host:
        return False
    name = host.split(marker)[0]
    name = name.replace("%20", " ").replace("%2f", "/").strip()
    return name in existing


def _extract_uri(device_line):
    """从 `Device:` 行提取真实 URI，兼容多种输出风格（不依赖特定硬件/版本）：
        Device: uri = "usb://HP/..."
        Device: uri = usb://HP/...
        Device: usb://HP/...
        Device: uri = hp:/usb/HP...
    """
    rest = device_line.split("Device:", 1)[1].strip()
    if rest.lower().startswith("uri"):          # 去掉可选 'uri =' 前缀
        rest = rest.split("=", 1)[1].strip()
    if len(rest) >= 2 and rest[0] in "\"'":      # 去掉包裹引号，避免误伤 URI 内部字符
        if rest[-1] in "\"'":
            rest = rest[1:-1]
    # 去掉可能尾随的 "描述" 字段（某些 lpinfo 输出形如 `uri "desc"`）
    if '"' in rest:
        rest = rest.split('"')[0].strip()
    return rest.strip()


def _extract_mdl(block_lines):
    """从一段设备块（已去掉 `Device:` 行）提取型号。
    `lpinfo -l -v` 块内为 `key = value` 格式；优先 make-and-model，
    其次 device-id 中的 MDL=，再次 info。"""
    mm = did = info = ""
    for ln in block_lines:
        s = ln.strip()
        if "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip().lower()
        v = v.strip().strip('"')
        if k == "make-and-model":
            mm = v
        elif k == "device-id":
            did = v
        elif k == "info":
            info = v
    if mm:
        return mm
    for kv in did.split(";"):
        if "=" in kv and kv.split("=", 1)[0].strip().upper() == "MDL":
            return kv.split("=", 1)[1].strip()
    return info


def _parse_verbose(text=None):
    """解析 `lpinfo -l -v` 的多行设备块。text 可外部注入（便于单测）。"""
    if text is None:
        rc, out, _ = C.run(["lpinfo", "-l", "-v"])
        text = out
    devices = []
    cur_uri = None
    cur_block = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("Device:"):
            if cur_uri:
                devices.append((cur_uri, _extract_mdl(cur_block)))
            cur_uri = _extract_uri(s)
            cur_block = []
        elif cur_uri is not None:
            cur_block.append(line)
    if cur_uri:
        devices.append((cur_uri, _extract_mdl(cur_block)))
    return [(u, m) for u, m in devices if u]


def _parse_simple():
    rc, out, _ = C.run(["lpinfo", "-v"])
    devs = []
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        cls, rest = parts
        # rest 可能为 `usb://HP/...?serial=.. "HP LaserJet..."`：URI 只取首段，
        # 引号里的描述不是 URI 的一部分。
        uri = rest.split()[0] if rest.split() else ""
        if _is_real_printer(uri):
            # 简单探测本身没有机型字段；但 URI 往往内嵌机型（如
            # usb://HP/LaserJet%20P1505n?serial=...），提取出来供 pick_driver 选驱动，
            # 避免落到 raw（raw 队列无法出纸、也不会被发布到 AirPrint）。
            devs.append((uri, _model_from_uri(uri)))
    return devs


def _model_from_uri(uri):
    """从 URI 猜测机型（仅用于 URI 中已内嵌机型时，如 usb://HP/LaserJet%20P1505n）。
    返回归一化前的可读机型串，失败返回空串。"""
    try:
        rest = uri.split("://", 1)[1]
    except Exception:
        return ""
    # 去掉 ?query
    rest = rest.split("?", 1)[0]
    # 去掉 vendor/ 前缀：usb://HP/LaserJet... -> LaserJet...
    seg = rest.split("/", 1)
    mdl = seg[1] if len(seg) > 1 else seg[0]
    mdl = mdl.replace("%20", " ").replace("+", " ").strip()
    return mdl


def _uri_priority(uri):
    """同机型多 URI 时选最优：ipps > ipp > usb:// > hp:/usb > 其它。

    HP 打印机在装有 HPLIP 时会同时暴露两个 USB URI：
      - `usb://HP/...`  —— CUPS 标准 USB 后端（所有标准驱动都认：foo2zjs/foo2xqx、
        gutenprint、splix、hpcups、brlaser…）；
      - `hp:/usb/...`   —— HPLIP 私有后端，专供 hpcups/hplip PPD 使用。
    把 `hp:/usb` 配给 foo2zjs/foo2xqx 这类驱动属于错误搭配：HPLIP 后端会反复重发
    数据，表现为“单个作业打印出多页 / 打印不停”。因此标准 usb:// 优先级高于 hp:/usb。
    """
    u = (uri or "").lower()
    if u.startswith("ipps://"):
        return 0
    if u.startswith("ipp://"):
        return 1
    if u.startswith("usb://"):
        return 2
    if u.startswith("hp:/usb") or u.startswith("hp://"):
        return 3
    return 4


def _is_real_printer(uri):
    """过滤掉 beh / cups-brf / 伪后端，只认真正可打印的传输协议。

    注意：
    - 裸 `serial:`、`http`、`https`、`ipp`、`lpd` 等只是 CUPS 后端名，并不表示有真实
      打印机挂在其上，必须带具体端点（:// 或厂商方案）才算。
    - `parallel:/` 与 `scsi:/` 是「伪设备」：无真实并口/SCSI 打印机的盒子（如本机 headless
      小主机）`lpinfo -v` 仍会列出 `parallel:/dev/lp0` / `scsi:/...`，早期版本据此建出了
      `print_printbox` 这种通用名幽灵队列、手机端看到空名/白纸。本机目标打印机是 USB，
      故直接把 parallel:/ 与 scsi:/ 视为非真实打印机过滤掉（避免反复误建幽灵队列）。
    """
    u = (uri or "").lower()
    # parallel:/ 与 scsi:/ 不在此列（见上文），cups-brf/beh 也不在
    for p in ("usb://", "hp:/usb", "hp://", "ipp://", "ipps://", "socket://",
              "lpd://", "smb://", "dnssd://", "mdns://"):
        if u.startswith(p):
            return True
    return False


def pick_driver(mdl, uri=""):
    """为给定机型选择驱动 model 名（`lpinfo -m` 中的一项）。

    通用策略（不绑定特定硬件/发行版）：
      1) 型号归一化后子串匹配 `lpinfo -m` 的驱动 id 与描述；
      2) 命中多个时优先「厂商关键字驱动」（hpcups/gutenprint/foo2zjs/...），
         再优先厂商标注的 `(recommended)` 项，最后取首个；
      3) 拿不到型号时，从 URI 厂商方案（hp:/usb、usb://…）反推候选；
      4) IPP/driverless 设备（uri 以 ipp:// 开头）优先用 CUPS 内置 'everywhere'；
      5) 返回 None 表示未命中（调用方再决定兜底 raw）。
    """
    entries = []  # [(driver_id, description)]
    # lpinfo -m 偶发在 cupsd 刚就绪、驱动后端（cups-driverd）尚未枚举完成时返回空，
    # 若此时直接返回 None，add_printer 会对真实 USB 打印机兜底成 raw → 被 CUPS 2.4 拒绝、
    # 队列永远建不出来（0.2.46 升级实测：repair 里 pick_driver 拿到空 lpinfo -m → 误判 raw）。
    # 故先确保 cups 在跑、再对 lpinfo -m 做几次重试，给驱动后端枚举时间（与 main() 枚举设备同源）。
    for _attempt in range(5):
        if not _ensure_cups():
            break
        rc, out, _ = C.run(["lpinfo", "-m"])
        for ln in (out or "").splitlines():
            if not ln.strip():
                continue
            parts = ln.split(None, 1)
            entries.append((parts[0], parts[1] if len(parts) > 1 else ""))
        if entries:
            break
        time.sleep(1)
    if not entries:
        C.log("printer", "WARN",
              "lpinfo -m 多次重试仍为空（cupsd 可能未就绪或驱动库缺失），无法自动选驱动；"
              "将交由 add_printer 显式报错而非静默 raw")

    def _match(prefix):
        p = _norm(prefix)
        return [(m, d) for m, d in entries
                if p in _norm(m) or p in _norm(d)]

    mdl_n = _norm(mdl)
    cands = _match(mdl_n) if mdl_n else []
    if not cands:
        # 型号缺失时从 URI 反推（hp:/usb -> hp、ipp -> 通用）
        if (uri or "").lower().startswith(("ipp://", "ipps://")):
            return "everywhere" if any("everywhere" in m.lower() for m, _ in entries) else None
        for key in ("hp", "hewlett", "brother", "samsung", "xerox", "kyocera",
                    "lexmark", "canon", "epson"):
            if key in (uri or "").lower():
                cands = _match(key)
                if cands:
                    break
    if not cands:
        return None
    # 1) 厂商标注的 recommended（上游最稳妥的驱动）优先
    for m, d in cands:
        if "recommended" in d.lower():
            return m
    # 2) 厂商关键字驱动优先
    for kw in ("hpcups", "gutenprint", "foo2zjs", "foomatic", "splix",
               "brlaser", "escpr", "postscript", "pcl"):
        for m, _ in cands:
            if kw in m.lower():
                return m
    return cands[0][0]


def printer_exists(name):
    rc, _, _ = C.run(["lpstat", "-p", name])
    return rc == 0


def _ppd_path(name):
    return f"/etc/cups/ppd/{name}.ppd"


def _current_uri(name):
    """读取打印机当前设备 URI。

    真实 `lpstat -v` 输出为 `device for X: uri`（冒号分隔，没有 "is" 这个词），
    但也兼容早期某些版本 `device for X is uri` 的写法。两种都解析，避免拿到空 URI
    导致 _maybe_repoint_uri 误判为“空 URI 强制重写”、或 _dedupe 归一时拿到空 key。"""
    rc, out, _ = C.run(["lpstat", "-v", name])
    if rc != 0:
        return ""
    m = re.search(r"device for\s+\S+\s*:\s*(\S+)", out)
    if not m:
        m = re.search(r"is\s+(\S+)", out)
    return m.group(1) if m else ""


def _maybe_repoint_uri(name, preferred_uri, mdl):
    """自愈：已存在且驱动正常的打印机，若当前 URI 不是最优（如 hp:/usb 配 foo2zjs 会重复打印），
    且二者指向同一物理设备（型号/序列号一致），则把后端重新指向更优 URI。

    仅当 preferred_uri 优先级严格优于当前 URI 时才改，避免误操作。
    特别地：若当前 URI 为空（打印机收不到作业、作业卡队列、显示“就绪”却不出纸），
    必须直接写入正确 URI——这是 0.2.23 修复“设备 URI 为空 / Win10 测试页无响应 / 作业卡住”的根因。
    """
    cur = _current_uri(name)
    if not cur:
        # 空 URI：直接校正为更优 URI，否则作业永远卡在队列里
        rc, _, err = C.run(["lpadmin", "-p", name, "-v", preferred_uri,
                            "-o", "printer-error-policy=abort-job"])
        if rc == 0:
            C.log("printer", "INFO",
                  f"已将 {name} 空设备 URI 校正为 {preferred_uri}（修复卡队列/出纸）")
        else:
            C.log("printer", "WARN", f"空 URI 校正失败: {err}")
        return
    if _uri_priority(cur) <= _uri_priority(preferred_uri):
        return  # 当前已是最优或更好
    # 同一物理设备才改：用型号关键字确认，避免把不同打印机误改
    key = _norm(mdl) or _norm(cur)
    if key and key not in _norm(cur + " " + preferred_uri):
        return
    rc, _, err = C.run(["lpadmin", "-p", name, "-v", preferred_uri,
                        "-o", "printer-error-policy=abort-job"])
    if rc == 0:
        C.log("printer", "INFO",
              f"已将 {name} 设备 URI 从 {cur} 校正为 {preferred_uri}（避免后端与驱动不匹配导致重复打印）")
    else:
        C.log("printer", "WARN", f"URI 校正失败: {err}")


def is_raw_printer(name):
    """判断队列是否为 raw/无 PPD（raw 队列无法对 PostScript/PDF 做实际渲染，会“收数据不出纸”）。"""
    p = _ppd_path(name)
    if not os.path.exists(p):
        return True
    try:
        with open(p, errors="ignore") as f:
            head = f.read(4000)
        if re.search(r'\*ModelName:\s*"raw"', head, re.I) \
           or re.search(r'\*NickName:\s*"raw"', head, re.I):
            return True
    except Exception:
        return True
    return False


# ---------------------------------------------------------------------------
# CUPS 配置托管（0.2.48 重构）
#
# 历史坑（<box> 盒 2026-10-29 实证）：早期 enable_job_archive 用
#   `cupsctl PreserveJobFiles=yes PreserveJobHistory=yes`
# 做 live reload。在 Debian trixie + CUPS 2.4 上，对这两条指令的 live 重载会让
# cupsd 立即 `Scheduler shutting down due to program error` 崩溃重启，进而把活动/
# 已完成作业重跑 —— 表现为「双打 / iOS 首打 2 份 / 鸿蒙·iOS 无限循环」。
# printbox-guard 每 ~15s 一轮，又把崩溃循环放大成持续雪崩。
# 同时，cupsctl 重写 cupsd.conf 时会剥掉注释，导致 _ensure_cupsd_limits 依赖的
# 注释 mark 失效、每次 init 又追加 MaxSubscriptions 块，把 cupsd.conf 撑到几十遍
# 重复（实证抓到 40+ 遍），进一步放大崩溃。
#
# 修复原则：
#   1. 所有 PrintBox 托管的 cupsd.conf 指令一律【直接写文件 + 去重】，绝不调用
#      cupsctl 做 live reload；cupsd 在下一次正常启动（repair / 升级都会触发）时
#      读取生效。这些是指令型配置，本就只需在启动时落地，无需 live 重载。
#   2. 写盘前先判定「是否已满足」，满足则纯 no-op —— archive 守护进程每轮的
#      ensure_preserve 因此彻底变成空操作，崩溃循环从源头断开。
#   3. 写盘用临时文件 + os.replace 原子替换，写前用 `cupsd -t` 预校验，失败则
#      保留原文件不破坏。
# ---------------------------------------------------------------------------
CUPSD_CONF = "/etc/cups/cupsd.conf"

# PrintBox 托管的所有 cupsd.conf 顶层指令（键 -> 目标值）。写盘时统一去重、保证唯一。
_CUPSD_MANAGED = {
    "PreserveJobFiles": "yes",
    "PreserveJobHistory": "yes",
    "MaxSubscriptions": "100000",
    "MaxSubscriptionsPerJob": "1000",
    "MaxEvents": "100000",
}
_CUPSD_MANAGED_MARK = "# PrintBox managed directives (edited by printbox, do not edit by hand)"


def _cupsd_readlines():
    try:
        with open(CUPSD_CONF, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []
    except Exception:
        return []


def _cupsd_directives_already(desired):
    """判定 cupsd.conf 是否已处于目标干净态：每个受管指令都「恰好出现一次」且值正确。
    只要有任一指令缺失或重复（历史 cupsctl 撑出的 40+ 遍重复块），即判为未满足，
    触发去重写盘。忽略大小写/多余空白。"""
    lines = _cupsd_readlines()
    for key, val in desired.items():
        pat = re.compile(r"^\s*" + re.escape(key) + r"\s+" + re.escape(val) + r"\s*$", re.I)
        if sum(1 for ln in lines if pat.match(ln)) != 1:
            return False
    return True


def _cupsd_write_directives(desired):
    """幂等写盘：删除所有受管指令的既有行 + PrintBox mark 注释，末尾追加干净的唯一块。
    返回是否发生了实际变更。仅在 /etc/cups 目录存在时动作（避免在无 CUPS 的开发机/CI 上
    产生副作用）；写前用 cupsd -t 预校验，失败则保留原文件。"""
    if not os.path.isdir(os.path.dirname(CUPSD_CONF)):
        return False
    try:
        lines = _cupsd_readlines()
        keys = list(desired.keys())
        kept = []
        for ln in lines:
            if ln.strip() == _CUPSD_MANAGED_MARK:
                continue  # 旧 mark 注释，丢弃（稍后统一重加）
            drop = False
            for k in keys:
                if re.match(r"^\s*" + re.escape(k) + r"(\s|$)", ln, re.I):
                    drop = True
                    break
            if not drop:
                kept.append(ln)
        block = [_CUPSD_MANAGED_MARK] + [f"{k} {v}" for k, v in desired.items()]
        new_text = "\n".join(kept + ["", _CUPSD_MANAGED_MARK] + [f"{k} {v}" for k, v in desired.items()] + [""]) + "\n"

        # 预校验（若系统有 cupsd 二进制）
        cupsd_bin = shutil.which("cupsd")
        if cupsd_bin:
            tmp = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False, encoding="utf-8")
            try:
                tmp.write(new_text)
                tmp.close()
                r = subprocess.run([cupsd_bin, "-t", "-c", tmp.name],
                                   capture_output=True, text=True, timeout=15)
                if r.returncode != 0:
                    C.log("printer", "WARN",
                          f"cupsd -t 校验新配置失败，放弃写入以免破坏 cupsd.conf: {r.stderr[-300:]}")
                    return False
            finally:
                try:
                    os.unlink(tmp.name)
                except Exception:
                    pass

        old_text = "\n".join(lines) + ("\n" if lines else "")
        if old_text == new_text:
            return False
        tmp_path = CUPSD_CONF + ".printbox.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(new_text)
        os.replace(tmp_path, CUPSD_CONF)
        return True
    except Exception as e:
        C.log("printer", "WARN", f"写入 cupsd.conf 托管指令失败（可忽略）: {e}")
        return False


def ensure_cupsd_managed():
    """一次性幂等设置全部 PrintBox 托管指令（去重 + 不 live reload）。
    同时清理历史 cupsctl 反复追加造成的重复块。返回是否发生了变更。"""
    if _cupsd_directives_already(_CUPSD_MANAGED):
        return False
    return _cupsd_write_directives(_CUPSD_MANAGED)


def enable_job_archive():
    """确保 CUPS 留存作业原始文件（PreserveJobFiles=yes）+ 保留历史（PreserveJobHistory=yes），
    用于打印文件归档。

    重要（0.2.48 修复）：现改为直接写 cupsd.conf 文件（见 _CUPSD_MANAGED 说明），
    绝不调用 cupsctl 做 live reload —— 那会让 cupsd 触发 'Scheduler shutting down' 崩溃重启、
    重跑作业（双打/循环）。已满足时纯 no-op（archive 守护进程每轮调用的 ensure_preserve
    因此不触发任何 cupsd 操作）。配置由 cupsd 下一次正常启动读取生效（repair / 升级都会重启 cupsd）。"""
    try:
        changed = ensure_cupsd_managed()
        if changed:
            C.log("printer", "INFO",
                  "已确保 CUPS PreserveJobFiles/PreserveJobHistory=yes（写入 cupsd.conf，重启生效）")
    except Exception as e:
        C.log("printer", "WARN", f"开启作业留存失败（可忽略）: {e}")


# foo2xqx（host-based）打印机的 PDF 归一化包装脚本：必须装在 /usr/lib/cups/filter/ 下。
# 原因：Debian trixie 默认启用 AppArmor，cupsd 的 AppArmor 配置只放行执行
# /usr/lib/cups/filter/ 与 /usr/lib/cups/backend/ 下的文件；装在 /usr/local/bin 会被
# 'execv failed: Permission denied'（status 113）拒绝，导致作业 0 字节、打不出纸。
# FOO2XQX_FILTER_NAME 是写入 PPD 的过滤器名（CUPS 按名从 /usr/lib/cups/filter/ 解析）。
FOO2XQX_WRAPPER = "/usr/lib/cups/filter/printbox-foo2xqx-pdf"
FOO2XQX_FILTER_NAME = "printbox-foo2xqx-pdf"
# 0.2.34 及之前误装到 /usr/local/bin 的死路径，升级时清理掉，避免混淆与残留执行失败
FOO2XQX_WRAPPER_OLD = "/usr/local/bin/printbox-foo2xqx-pdf"


def _resolve_filter(name):
    """定位 CUPS 过滤器/可执行文件。

    pdftopdf 等 CUPS 过滤器装在 /usr/lib/cups/filter/ 下，通常不在 $PATH，
    因此先查 which，再回退到常见过滤器目录，避免白页补丁因查不到而静默跳过。"""
    p = shutil.which(name)
    if p:
        return p
    for d in ("/usr/lib/cups/filter", "/usr/libexec/cups/filter",
              "/usr/local/lib/cups/filter"):
        cand = os.path.join(d, name)
        if os.path.exists(cand):
            return cand
    return None


def _cups_inserts_pdftopdf():
    """判定 CUPS 是否会在 PDF 链路自动插入 pdftopdf（mime 转换 application/pdf →
    application/vnd.cups-pdf）。绝大多数发行版（含 Debian trixie）默认有此规则，此时
    pdftopdf 已由 CUPS 作为中间 filter 调用（收 COPIES=1、只做页码/尺寸归一化）。wrapper
    若【再】调一次 pdftopdf 处理 copies，会出现两次乘法 → 实际份数 = 请求份数²。
    返回 bool。此判定直接读 mime 规则文件，不依赖 cupsd 运行、不依赖具体 PPD。"""
    for f in ("/usr/share/cups/mime/cupsfilters.convs",
              "/usr/share/cups/mime/mime.convs",
              "/etc/cups/mime.convs"):
        if os.path.exists(f):
            try:
                with open(f, errors="ignore") as fh:
                    txt = fh.read()
            except Exception:
                continue
            if re.search(r'application/pdf\s+application/vnd\.cups-pdf\s+\d+\s+pdftopdf', txt):
                return True
    return False


def _install_foo2xqx_wrapper():
    """安装 foo2xqx 的 PDF 归一化包装脚本到 /usr/lib/cups/filter/。

    注意：必须装在 /usr/lib/cups/filter/ 下。Debian trixie 默认启用 AppArmor，cupsd 的
    AppArmor 配置只放行执行 /usr/lib/cups/filter/ 与 /usr/lib/cups/backend/ 下的文件；
    装在 /usr/local/bin 会被 'execv failed: Permission denied'（status 113）拒绝，
    导致作业 0 字节、打不出纸（这是 0.2.34 及之前「WebUI 打不出 / 测试页空白」的真凶）。
    故这里同时清理 0.2.34 及更早误装到 /usr/local/bin 的旧 wrapper，避免残留文件混淆。

    iOS/鸿蒙发来的 JPEG(0 DPI) 经 imagetopdf 得到 PDF，foo2xqx 的 foomatic-rip 收到 PDF 后
    自行用 gs ps2write 转 PS，却因 0 DPI 算不出页面尺寸（aspect=-nan）只发几百字节空白页。
    包装脚本先 pdftopdf（cups-filters 的 PDF→PDF 归一化，会按 PPD 默认纸张 A4 重排页面尺寸，
    并负责处理份数/页码范围，把份数直接翻进页数），再交 foomatic-rip 出图。关键：foomatic 必须
    以 COPIES=1 调用，否则 foo2xqx 会按 COPIES 把已翻倍的页数再乘一次，导致实际份数 = 请求份数²
    （如请求 2 份打出 4 份）。任何一步失败都回退到原始 foomatic-rip 行为（此时保留原 COPIES，
    pdftopdf 未生效），绝不比当前更差（Win10 等正常 PDF/PS 作业走 pdftopdf 后等价，不会变坏）。"""
    # ★ 关键修复（0.2.50）：不再因安装时探测不到 pdftopdf/foomatic-rip 就放弃写盘。
    # 旧版在此 `if not pdftopdf or not foomatic: return False` —— 一旦部署那一刻
    # `_resolve_filter` 没找到（pdftopdf 装在 /usr/lib/cups/filter 不在 $PATH，探测偶发失败），
    # 包装脚本就永远不更新，盒上残留旧版（把 COPIES 也传给 foomatic → 实际份数=请求份数²）。
    # 现改为：探测不到也照写，并在生成的脚本里做运行时兜底解析，确保磁盘上的 wrapper 永远是
    # 最新版。探测结果仅用于「安装时」内联一个优选路径，脚本运行时会先验证其可执行，不可用时
    # 再回退 command -v / 过滤器目录。
    pdftopdf = _resolve_filter("pdftopdf") or ""
    foomatic = _resolve_filter("foomatic-rip") or ""
    script = (
        "#!/bin/bash\n"
        "# PrintBox foo2xqx PDF 归一化包装：先 pdftopdf 归一化页面尺寸，再交 foomatic-rip 出图。\n"
        "set -u\n"
        f'PDFTOPDF="{pdftopdf}"\n'
        f'FOOMATIC="{foomatic}"\n'
        '# 安装时若 pdftopdf/foomatic-rip 未探测到（过滤器装在 /usr/lib/cups/filter，可能不在 $PATH），\n'
        '# 运行时再找一次，避免脚本因内联路径为空而失效。\n'
        'if [ ! -x "$PDFTOPDF" ]; then\n'
        '  PDFTOPDF=$(command -v pdftopdf 2>/dev/null)\n'
        '  if [ -z "$PDFTOPDF" ]; then\n'
        '    for d in /usr/lib/cups/filter /usr/libexec/cups/filter /usr/local/lib/cups/filter; do\n'
        '      [ -x "$d/pdftopdf" ] && { PDFTOPDF="$d/pdftopdf"; break; }\n'
        '    done\n'
        '  fi\n'
        'fi\n'
        'if [ ! -x "$FOOMATIC" ]; then\n'
        '  FOOMATIC=$(command -v foomatic-rip 2>/dev/null)\n'
        '  if [ -z "$FOOMATIC" ]; then\n'
        '    for d in /usr/lib/cups/filter /usr/libexec/cups/filter /usr/local/lib/cups/filter /usr/bin; do\n'
        '      [ -x "$d/foomatic-rip" ] && { FOOMATIC="$d/foomatic-rip"; break; }\n'
        '    done\n'
        '  fi\n'
        'fi\n'
        'JID="$1"; USER="$2"; TITLE="$3"; COPIES="$4"; shift 4\n'
        'ARGS=("$@")\n'
        'INFILE=""\n'
        'if [ ${#ARGS[@]} -gt 0 ]; then\n'
        '  last="${ARGS[${#ARGS[@]}-1]}"\n'
        '  if [ -n "$last" ] && [ -e "$last" ]; then INFILE="$last"; fi\n'
        'fi\n'
        'TMP="$(mktemp /tmp/printbox-f2x-XXXXXX.pdf)" || exec "$FOOMATIC" "$JID" "$USER" "$TITLE" "$COPIES" "${ARGS[@]}"\n'
        'if "$PDFTOPDF" "$JID" "$USER" "$TITLE" "$COPIES" "${ARGS[@]}" > "$TMP" 2>/dev/null && [ -s "$TMP" ] && [ "$(head -c4 "$TMP")" = "%PDF" ]; then\n'
        '  # pdftopdf 已在上面按 COPIES 把页数翻倍（copies/page-range 是 pdftopdf 的职责），\n'
  '  # 故交给 foomatic 时必须令其 COPIES=1，否则 foo2xqx 会再乘一次 → 实际份数 = 请求份数²。\n'
  '  FARGS=("$JID" "$USER" "$TITLE" "1")\n'
        '  if [ -n "$INFILE" ]; then\n'
        '    for ((i=0;i<${#ARGS[@]}-1;i++)); do FARGS+=("${ARGS[$i]}"); done\n'
        '  else\n'
        '    FARGS+=("${ARGS[@]}")\n'
        '  fi\n'
        '  cat "$TMP" | "$FOOMATIC" "${FARGS[@]}"\n'
        '  rc=$?\n'
        '  rm -f "$TMP"\n'
        '  exit $rc\n'
        'fi\n'
        'rm -f "$TMP"\n'
        'exec "$FOOMATIC" "$JID" "$USER" "$TITLE" "$COPIES" "${ARGS[@]}"\n'
    )
    # 0.2.51：若 CUPS 已在 PDF 链路自动插入 pdftopdf（mime 转换），则模式 A 覆盖——wrapper
    # 不再调 pdftopdf（否则 copies 被乘两次 = 请求份数²），直接透传 + foomatic 收原始 COPIES。
    if _cups_inserts_pdftopdf():
        script = (
            "#!/bin/bash\n"
            "# PrintBox foo2xqx 包装（模式A：CUPS 已自动插入 pdftopdf，wrapper 不再调用，避免份数翻倍）。\n"
            "set -u\n"
            f'FOOMATIC="{foomatic}"\n'
            'if [ ! -x "$FOOMATIC" ]; then\n'
            '  FOOMATIC=$(command -v foomatic-rip 2>/dev/null)\n'
            '  if [ -z "$FOOMATIC" ]; then\n'
            '    for d in /usr/lib/cups/filter /usr/libexec/cups/filter /usr/local/lib/cups/filter /usr/bin; do\n'
            '      [ -x "$d/foomatic-rip" ] && { FOOMATIC="$d/foomatic-rip"; break; }\n'
            '    done\n'
            '  fi\n'
            'fi\n'
            'JID="$1"; USER="$2"; TITLE="$3"; COPIES="$4"; shift 4\n'
            'exec "$FOOMATIC" "$JID" "$USER" "$TITLE" "$COPIES" "$@"\n'
        )
    try:
        os.makedirs(os.path.dirname(FOO2XQX_WRAPPER), exist_ok=True)
        with open(FOO2XQX_WRAPPER, "w") as f:
            f.write(script)
        os.chmod(FOO2XQX_WRAPPER, 0o755)
        C.log("printer", "INFO", f"已安装 foo2xqx 归一化包装: {FOO2XQX_WRAPPER}")
        # 清理 0.2.34 及更早误装到 /usr/local/bin 的旧 wrapper（AppArmor 拒执行、且无用）
        if FOO2XQX_WRAPPER_OLD != FOO2XQX_WRAPPER and os.path.exists(FOO2XQX_WRAPPER_OLD):
            try:
                os.remove(FOO2XQX_WRAPPER_OLD)
                C.log("printer", "INFO",
                      f"已清理旧版 foo2xqx 包装残留: {FOO2XQX_WRAPPER_OLD}")
            except Exception as e:
                C.log("printer", "WARN", f"清理旧版 foo2xqx 包装失败（可忽略）: {e}")
        return True
    except Exception as e:
        C.log("printer", "WARN", f"安装 foo2xqx 包装失败: {e}")
        return False


def _sanitize_ppd_filter_paths(ppd_dir="/etc/cups/ppd"):
    """升级/修复前对磁盘 PPD 做「离线净化」，打断「坏 PPD → cupsd 启动崩溃 → 队列丢失」死循环。

    根因（0.2.36/0.2.37 反复回潮）：历史版本曾把 foo2xqx 过滤器行指向
    /usr/local/bin/printbox-foo2xqx-pdf（AppArmor 拒执行、0.2.35 起已删除）。只要
    /etc/cups/ppd/*.ppd 里残留这条不存在的路径，cupsd 一加载就
    `File "..." not available` 并 `Scheduler shutting down due to program error` 崩溃循环；
    崩溃中 cupsd 还可能把 /etc/cups/printers.conf、PPD 写崩（截断为空），于是 upgrade/repair
    之后队列彻底消失、iOS 看到的是残留 mDNS 残影、打印必失败。

    本函数在「重启 cupsd 之前」纯文件级把残留旧路径改写成 AppArmor 放行的
    /usr/lib/cups/filter/printbox-foo2xqx-pdf，使 cupsd 能正常启动；随后 printers.main() 才会
    真正重建/补丁队列。纯文件操作、不依赖 cupsd，故可在 repair 重启前安全调用。

    ppd_dir 默认 /etc/cups/ppd；单元测试可传入临时目录以隔离真实系统。"""
    # 先确保正确 wrapper 在盘（缺失会让改写后的 PPD 仍指向不存在的文件）。
    try:
        _install_foo2xqx_wrapper()
    except Exception as e:
        C.log("printer", "WARN", f"离线净化前安装 wrapper 失败（可忽略）: {e}")
    if not os.path.isdir(ppd_dir):
        return
    fixed = 0
    for fn in sorted(os.listdir(ppd_dir)):
        if not fn.endswith(".ppd"):
            continue
        ppd = os.path.join(ppd_dir, fn)
        try:
            with open(ppd, errors="ignore") as f:
                text = f.read()
        except Exception:
            continue
        if FOO2XQX_WRAPPER_OLD not in text:
            continue
        new = text.replace(FOO2XQX_WRAPPER_OLD, FOO2XQX_WRAPPER)
        tmp = ppd + ".tmp"
        try:
            with open(tmp, "w") as f:
                f.write(new)
            os.chmod(tmp, 0o644)
        except Exception as e:
            C.log("printer", "WARN", f"离线净化写 PPD {fn} 失败: {e}")
            continue
        # 安全护栏：改写后必须 cupstestppd 通过，否则放弃替换、保留原文件，
        # 避免损坏的 PPD 被后续 lpadmin 加载后导致 cupsd 启动即崩溃。
        ct = shutil.which("cupstestppd") or "/usr/bin/cupstestppd"
        if os.path.exists(ct):
            rc_ct, out_ct, err_ct = C.run([ct, tmp])
            if rc_ct != 0:
                C.log("printer", "WARN",
                      f"离线净化后 PPD {fn} 未通过 cupstestppd，放弃替换以保留原 PPD: "
                      f"{(out_ct + err_ct).strip()[-300:]}")
                try:
                    os.remove(tmp)
                except Exception:
                    pass
                continue
        try:
            os.replace(tmp, ppd)
            fixed += 1
            C.log("printer", "INFO",
                  f"离线净化 PPD {fn}：已把旧 wrapper 路径改写为 {FOO2XQX_WRAPPER}")
        except Exception as e:
            C.log("printer", "WARN", f"离线净化替换 PPD {fn} 失败: {e}")
    if fixed:
        C.log("printer", "INFO",
              f"离线净化完成，共修正 {fixed} 个 PPD（已打断 cupsd 崩溃死循环）")


def _ppd_is_raw_or_missing(ppd_path):
    """离线（cupsd 未必在跑）判定某 PPD 是否「缺失 / 损坏 / raw」，返回原因字符串或 None。

    - 文件不存在 → "missing"
    - 无法读取 → "unreadable"
    - 无 *ModelName 或无 *OpenUI *PageSize → "raw"（CUPS 生成的极简 raw PPD 没有这些块）
    - cupstestppd 校验失败 → "invalid"
    否则返回 None（视为健康真驱动 PPD）。
    用于 repair 离线摘除坏队列、以及 add_printer 判断「已存在但需重建」。"""
    if not os.path.exists(ppd_path):
        return "missing"
    try:
        with open(ppd_path, errors="ignore") as f:
            p = f.read()
    except Exception:
        return "unreadable"
    if not re.search(r'^\*ModelName:', p, re.M):
        return "raw(no ModelName)"
    if not re.search(r'^\*OpenUI \*PageSize', p, re.M):
        return "raw(no PageSize UI)"
    ct = shutil.which("cupstestppd") or "/usr/bin/cupstestppd"
    if os.path.exists(ct):
        rc, _, _ = C.run([ct, ppd_path])
        if rc != 0:
            return "invalid(cupstestppd)"
    return None


def _remove_broken_queues_offline(printers_conf="/etc/cups/printers.conf",
                                  ppd_dir="/etc/cups/ppd"):
    """repair 在【停掉 cupsd】后离线调用：从 printers.conf 摘除 PPD 缺失/损坏/raw 的坏队列，
    使 cupsd 能以干净状态启动。否则 cupsd 一加载坏队列（尤其 PPD 被历史崩溃写崩的队列）就
    `Scheduler shutting down` 崩溃循环，main() 永远没机会重建队列。

    仅摘除坏队列、保留健康队列；直接改文件、不依赖 cupsd。返回摘除数量。"""
    if not os.path.exists(printers_conf):
        return 0
    try:
        with open(printers_conf, errors="ignore") as f:
            text = f.read()
    except Exception:
        return 0
    blocks = list(re.finditer(
        r'<(DefaultPrinter|Printer)\s+([^>]+)>(.*?)</Printer>', text, re.S))
    if not blocks:
        return 0
    bad = {}
    for m in blocks:
        nm = m.group(2).strip()
        reason = _ppd_is_raw_or_missing(os.path.join(ppd_dir, nm + ".ppd"))
        if reason:
            bad[nm] = reason
    if not bad:
        return 0
    out = []
    pos = 0
    for m in blocks:
        out.append(text[pos:m.start()])
        nm = m.group(2).strip()
        if nm in bad:
            out.append("# PrintBox: 已摘除坏队列 %s（%s）\n" % (nm, bad[nm]))
        else:
            out.append(m.group(0))
        pos = m.end()
    out.append(text[pos:])
    tmp = printers_conf + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write("".join(out))
        os.chmod(tmp, 0o600)
        os.replace(tmp, printers_conf)
    except Exception as e:
        C.log("printer", "WARN", f"离线摘除坏队列失败: {e}")
        return 0
    C.log("printer", "INFO",
          f"已离线摘除 {len(bad)} 个坏队列: " + ", ".join(f"{n}({r})" for n, r in bad.items()))
    return len(bad)


def _wipe_managed_queues_offline(printers_conf="/etc/cups/printers.conf",
                                 ppd_dir="/etc/cups/ppd"):
    """repair/init 在【停掉 cupsd 且确认其已退出】后离线调用：从 printers.conf 摘除两类队列、
    制造干净状态，以便 printers.main() 重新探测并重建为真实驱动 + A4：

      1) 本系统托管的全部 *_printbox 队列（连同其 PPD 文件）；
      2) **任何 PPD 缺失/损坏/raw 的坏队列，无论名字是否带 _printbox**。

    为何必须摘除非 _printbox 的坏队列（0.2.44 修复）：历史版本（0.2.23 及更早）或手工 lpadmin 可能在
    printers.conf 留下不带 _printbox 后缀的坏队列（如 `hp_laserjet_p1505n`），其 PPD 文件已被历史崩溃
    写崩/删除；cupsd 一加载就 `Raw queues are deprecated` + `Scheduler shutting down due to program
    error` 崩溃循环。旧逻辑只摘 _printbox 队列会漏掉它，清场不净 → cupsd 持续崩溃 → main() 永远没机会
    重建。故只要 PPD 缺失/raw 就一律摘除。
    健康（有真实 PPD、非 raw）的非 _printbox 队列仍保留，不误伤用户手动添加的打印机。
    正则容错：即便 printers.conf 被崩溃写崩、缺少 </Printer> 结束标签，也用 `(</Printer>|$)` 兜底切到
    行尾/文件尾，保证坏块一定被清掉。直接改文件、不依赖 cupsd。返回摘除的队列数。"""
    if not os.path.exists(printers_conf):
        return 0
    try:
        with open(printers_conf, errors="ignore") as f:
            text = f.read()
    except Exception:
        return 0
    # 匹配所有队列块（容忍缺少闭合标签），摘除坏队列
    blocks = list(re.finditer(
        r'<(DefaultPrinter|Printer)\s+([^>]+)>(.*?)(</Printer>|$)', text, re.S))
    if not blocks:
        return 0
    removed = []
    out = []
    pos = 0
    for m in blocks:
        nm = m.group(2).strip()
        out.append(text[pos:m.start()])
        reason = None
        if nm.endswith("_printbox"):
            reason = "managed(_printbox)"
        else:
            r = _ppd_is_raw_or_missing(os.path.join(ppd_dir, nm + ".ppd"))
            if r:
                reason = f"bad/raw/missing PPD({r})"
        if reason:
            removed.append((nm, reason))
            out.append(f"# PrintBox: 离线摘除队列 {nm}（{reason}）\n")
        else:
            out.append(m.group(0))
        pos = m.end()
    out.append(text[pos:])
    if not removed:
        return 0
    tmp = printers_conf + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write("".join(out))
        os.chmod(tmp, 0o600)
        os.replace(tmp, printers_conf)
    except Exception as e:
        C.log("printer", "WARN", f"离线清空托管队列失败: {e}")
        return 0
    # 一并删除其 PPD 文件，避免残留半残 PPD 在 cupsd 重启后被加载崩
    if os.path.isdir(ppd_dir):
        for nm, _ in removed:
            ppd = os.path.join(ppd_dir, nm + ".ppd")
            try:
                if os.path.exists(ppd):
                    os.remove(ppd)
            except Exception:
                pass
    C.log("printer", "INFO",
          f"已离线清空 {len(removed)} 个队列（清场）: " +
          ", ".join(f"{n}({r})" for n, r in removed))
    return len(removed)


def _wipe_all_printers_offline(printers_conf="/etc/cups/printers.conf",
                               ppd_dir="/etc/cups/ppd"):
    """紧急兜底（0.2.44 新增）：停掉 cupsd 且【正常清场后仍不稳定】时，离线摘除 printers.conf 中
    【全部】打印机块并删除全部 PPD，制造绝对干净状态。

    本机是单用途打印服务器、printers.main() 会确定性重建唯一 USB 打印机，故全量清场安全；
    仅在「常规清场后 cupsd 仍崩溃」的极端情况下由 init._stabilize_cups_for_rebuild 调用，
    作为打断崩溃循环的最后手段（确保 main() 有干净的起点可重建）。直接改文件、不依赖 cupsd。"""
    if os.path.exists(printers_conf):
        try:
            with open(printers_conf, errors="ignore") as f:
                text = f.read()
            out = []
            pos = 0
            for m in re.finditer(
                r'<(DefaultPrinter|Printer)\s+[^>]+>.*?(</Printer>|$)', text, re.S):
                out.append(text[pos:m.start()])
                out.append("# PrintBox: 紧急全量清场（main() 将重建）\n")
                pos = m.end()
            out.append(text[pos:])
            tmp = printers_conf + ".tmp"
            with open(tmp, "w") as f:
                f.write("".join(out))
            os.chmod(tmp, 0o600)
            os.replace(tmp, printers_conf)
        except Exception as e:
            C.log("printer", "WARN", f"紧急全量清场失败: {e}")
    if os.path.isdir(ppd_dir):
        for fn in os.listdir(ppd_dir):
            if fn.endswith(".ppd"):
                try:
                    os.remove(os.path.join(ppd_dir, fn))
                except Exception:
                    pass
    C.log("printer", "INFO", "已紧急全量清场：移除全部打印机块与 PPD 文件")


def _inject_ppd_a4(text):
    """确保 PPD 的 *PageSize/*PageRegion 选项列表里包含 A4（best-effort，绝不破坏结构）。

    早期版本在「PPD 选项列表无 A4」时直接放弃（只记 INFO），导致 iOS/鸿蒙/Win 的纸张选项里
    永远看不到 A4、默认停在 Letter（用户反复反馈「改了 N 次 A4 都没生效」的根因之一）。
    这里改为【强制保证 A4 存在】：
      1) 已有 A4 选项：原样返回；
      2) 有 *OpenUI *PageSize 块：在 *DefaultPageSize（或其 OpenUI 行）后插入 A4 选项；
      3) 无 OpenUI 块：在 *DefaultPageSize 后、或文件末尾追加一段最小 A4 OpenUI 块。
    随后 patch_ppd_defaults 会把 *DefaultPageSize/*DefaultPageRegion 设为 A4。任何异常由调用方兜底。"""
    if re.search(r'^\*PageSize\s+[Aa]4[/\s]', text, re.M):
        return text  # 已有 A4 选项，无需注入
    a4 = "595 842"  # A4 页面尺寸（点）
    ps_line = f'*PageSize A4/A4: "<</PageSize[{a4}]>>setpagedevice"\n'
    pr_line = f'*PageRegion A4/A4: "<</PageRegion[{a4}]>>setpagedevice"\n'
    lines = text.splitlines(keepends=True)
    # 情况 2：有 *OpenUI *PageSize 块——在 *DefaultPageSize 行（或 OpenUI 行）后插入 A4 选项
    if re.search(r'^\*OpenUI \*PageSize', text, re.M):
        out, done = [], False
        for ln in lines:
            out.append(ln)
            if not done and ln.strip().startswith("*DefaultPageSize"):
                out.append(ps_line)
                out.append(pr_line)
                done = True
        if not done:
            out, done = [], False
            for ln in lines:
                out.append(ln)
                if not done and ln.strip().startswith("*OpenUI *PageSize"):
                    out.append("*DefaultPageSize: A4\n")
                    out.append(ps_line)
                    out.append(pr_line)
                    done = True
        return "".join(out)
    # 情况 3：无 OpenUI 块——补一段最小 A4 OpenUI 块（插在 *DefaultPageSize 后或文件末尾）
    block = ("*OpenUI *PageSize: PickOne\n"
             "*OrderDependency: 10 AnySetup *PageSize\n"
             "*DefaultPageSize: A4\n" + ps_line + pr_line +
             "*CloseUI: *PageSize\n")
    if re.search(r'^\*DefaultPageSize', text, re.M):
        out = []
        for ln in lines:
            out.append(ln)
            if ln.strip().startswith("*DefaultPageSize"):
                out[-1] = "*DefaultPageSize: A4\n"
                out.append(block)
        return "".join(out)
    # 连 *DefaultPageSize 都没有：追加到末尾
    return text + "\n" + block


def patch_ppd_defaults(name):
    """为所有非 raw（带 PPD、能真正出纸）的队列注入默认纸张 A4，并确保 A4 出现在 *PageSize 选项列表。

    修复历史版本「PPD 无 A4 选项就静默放弃」导致默认永远停在 Letter、iOS/鸿蒙/Win 看不到 A4 的问题
    （用户反复反馈「改了 N 次 A4 都没生效」的根因）。流程：
      1) 先 _inject_ppd_a4 强制保证 PPD 选项含 A4（无 OpenUI 块也补最小块，绝不破坏结构）；
      2) 改写 *DefaultPageSize/*DefaultPageRegion 为 A4；
      3) cupstestppd 护栏校验，非法则放弃替换、保留原 PPD；
      4) lpadmin -P 重新加载，并显式 -o PageSize=A4 -o PageRegion=A4 设队列级默认。
    重复调用幂等。仅当注入后【仍】无 A4 选项（结构异常）才放弃并记录 WARN。"""
    if is_raw_printer(name):
        return
    ppd = _ppd_path(name)
    if not os.path.exists(ppd):
        return
    try:
        with open(ppd, errors="ignore") as f:
            text = f.read()
    except Exception:
        return
    text = _inject_ppd_a4(text)
    # 若注入后【仍】没有 A4 选项（极罕见、PPD 结构异常），记录并放弃，绝不写坏 PPD。
    if not re.search(r'^\*PageSize\s+[Aa]4[/\s]', text, re.M):
        C.log("printer", "WARN",
              f"队列 {name} 的 PPD 结构异常、无法注入 A4 选项（已尽力），跳过默认 A4")
        return
    lines = text.splitlines(keepends=True)
    out = []
    for ln in lines:
        s = ln.rstrip("\n")
        if s.startswith("*DefaultPageSize:"):
            out.append("*DefaultPageSize: A4\n" if s.strip() != "*DefaultPageSize: A4" else ln)
            continue
        if s.startswith("*DefaultPageRegion:"):
            out.append("*DefaultPageRegion: A4\n" if s.strip() != "*DefaultPageRegion: A4" else ln)
            continue
        out.append(ln)
    joined = "".join(out)
    changed = joined != text
    if "*DefaultPageSize: A4" not in joined:
        out.append("*DefaultPageSize: A4\n")
        changed = True
    if "*DefaultPageRegion: A4" not in joined:
        out.append("*DefaultPageRegion: A4\n")
        changed = True
    if not changed:
        return
    tmp = ppd + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.writelines(out)
        os.chmod(tmp, 0o644)
    except Exception as e:
        C.log("printer", "WARN", f"PPD 默认 A4 写入失败: {e}")
        return
    # 安全护栏：用 cupstestppd 校验改写后的 PPD，非法则放弃替换、保留原 PPD。
    # 避免损坏的 PPD 被 lpadmin 加载后导致 cupsd 启动即崩溃
    # （日志里 “Scheduler shutting down due to program error” 循环的常见根因之一）。
    ct = shutil.which("cupstestppd") or "/usr/bin/cupstestppd"
    if os.path.exists(ct):
        rc_ct, out_ct, err_ct = C.run([ct, tmp])
        if rc_ct != 0:
            C.log("printer", "WARN",
                  f"注入 A4 后的 PPD 未通过 cupstestppd 校验，放弃替换以保留原 PPD: "
                  f"{(out_ct + err_ct).strip()[-300:]}")
            try:
                os.remove(tmp)
            except Exception:
                pass
            return
    os.replace(tmp, ppd)
    rc, _, err = C.run(["lpadmin", "-p", name, "-P", ppd])
    if rc == 0:
        # 同时把队列级默认纸张显式设为 A4：仅注入 PPD 的 *DefaultPageSize 时，printers.conf
        # 里既存的 Option PageSize（常为 Letter）可能仍作为“当前默认”，lpoptions 反映不出 A4；
        # 显式 -o 设置队列级默认，确保 iOS/鸿蒙/Win 默认走 A4 而非 Letter。
        C.run(["lpadmin", "-p", name, "-o", "PageSize=A4", "-o", "PageRegion=A4"])
        C.log("printer", "INFO", f"已为打印机 {name} 强制注入默认 A4（修复 iOS/鸿蒙 无 A4 选项）")
    else:
        C.log("printer", "WARN", f"PPD 默认 A4 已写入但 lpadmin 重新加载失败: {err}")


def patch_foo2xqx_ppd(name):
    """为 foo2xqx（host-based）打印机打补丁，修复移动端空白页。

    - 把 PPD 中 application/pdf -> foomatic-rip 的 cupsFilter 改为先经归一化包装脚本；
    - 默认纸张 A4 由 patch_ppd_defaults 统一处理（对所有非 raw PPD 生效），此处不再重复；
    - 仅对含 foo2xqx/foomatic 的 PPD 生效，其它驱动（driverless/everywhere/raw）不动，
      避免误伤。
    改完用 lpadmin -P 重新加载 PPD 让 CUPS 立即生效。"""
    ppd = _ppd_path(name)
    if not os.path.exists(ppd):
        return
    try:
        # 读完整 PPD：foo2xqx 的 *cupsFilter("...foomatic-rip") 行常在 PPD 末尾（30KB+），
        # 若只读前 30000 字节会读不到，导致门控误判「非 foo2xqx」而直接 return，
        # wrapper 永不安装、PPD 永不打补丁（移动端白页/打印失败）。
        with open(ppd, errors="ignore") as f:
            head = f.read()
    except Exception:
        return
    low = head.lower()
    if "foo2xqx" not in low and "foomatic" not in low:
        return  # 非 foo2xqx/foomatic 驱动，不碰
    # 门控：PPD 的过滤器行可能仍是旧绝对路径（0.2.34 把 foomatic-rip 直接替换成了
    # /usr/local/bin/printbox-foo2xqx-pdf，0.2.35 起该路径已被 AppArmor 拒执行且已迁移到
    # /usr/lib/cups/filter/）。若只认 'foomatic-rip' 会匹配不到、提前 return，导致旧 PPD 永远
    # 不被修正、作业 0 字节不出纸。故旧路径或 foomatic-rip 任一存在即需要重打补丁。
    if FOO2XQX_WRAPPER_OLD not in head and "foomatic-rip" not in head:
        return
    if not _install_foo2xqx_wrapper() and not os.path.exists(FOO2XQX_WRAPPER):
        # _install_foo2xqx_wrapper 失败（如本次探测 pdftopdf/foomatic-rip 找不到）但
        # 目标 wrapper 已存在（上次成功安装留下的），仍允许改写 PPD——否则会陷入
        # “wrapper 在但 PPD 永远不指向它”的死局。仅当 wrapper 确实不存在时才放弃。
        return
    try:
        with open(ppd, errors="ignore") as f:
            lines = f.readlines()
        out = []
        changed = False
        for ln in lines:
            s = ln.strip()
            # 任何 cupsFilter / cupsFilter2 行，只要引用了 foomatic-rip 或旧绝对路径
            # /usr/local/bin/printbox-foo2xqx-pdf，就重定向到 AppArmor 放行的
            # /usr/lib/cups/filter/printbox-foo2xqx-pdf。
            #
            # ★ 关键修正（0.2.37）：不再限定 MIME 类型。0.2.36 只认
            #   application/pdf / application/vnd.cups-pdf / application/vnd.cups-postscript
            #   三种 MIME——若某 foo2zjs PPD 的 cupsFilter 行用了其它 MIME（如
            #   application/vnd.cups-raster、application/postscript、image/* 等），
            #   该过滤器行就【永远不被改写】，PPD 仍指向 0.2.34 删掉的
            #   /usr/local/bin/printbox-foo2xqx-pdf → 作业 0 字节、cupsd 崩溃循环、
            #   iOS/Win/鸿蒙 一律打不出。改为“行内出现 foomatic-rip 或旧绝对路径即改”，
            #   与 MIME 无关，覆盖所有 PPD 变体。
            if (s.startswith("*cupsFilter") or s.startswith("*cupsFilter2")) \
               and ("foomatic-rip" in s or FOO2XQX_WRAPPER_OLD in s):
                new = ln
                replaced = False
                if "foomatic-rip" in new:
                    new = new.replace("foomatic-rip", FOO2XQX_WRAPPER)
                    replaced = True
                if FOO2XQX_WRAPPER_OLD in new:
                    new = new.replace(FOO2XQX_WRAPPER_OLD, FOO2XQX_WRAPPER)
                    replaced = True
                if replaced:
                    out.append(new)
                    changed = True
                    continue
            out.append(ln)
        if not changed:
            return
        tmp = ppd + ".tmp"
        with open(tmp, "w") as f:
            f.writelines(out)
        os.chmod(tmp, 0o644)
        os.replace(tmp, ppd)
    except Exception as e:
        C.log("printer", "WARN", f"foo2xqx PPD 补丁写入失败: {e}")
        return
    rc, _, err = C.run(["lpadmin", "-p", name, "-P", ppd])
    if rc == 0:
        C.log("printer", "INFO",
              f"已为 foo2xqx 打印机 {name} 打补丁（PDF 归一化 + 默认 A4），修复移动端空白页")
    else:
        C.log("printer", "WARN", f"foo2xqx PPD 补丁已写入但 lpadmin 重新加载失败: {err}")


def _strip_drv_cost(drv):
    """去掉 lpinfo -m 模型 id 可能带的「代价」数字前缀（如 `foo2zjs:0/ppd/...` → `foo2zjs:/ppd/...`）。

    某些发行版该前缀会让 `lpadmin -m` 解析失败，进而回退 raw（raw 队列无真实 PPD、无 A4 选项、
    PDF 直传打印机会报错灯）。返回去前缀的 id；无此前缀时原样返回。"""
    return re.sub(r':\d+/', ':/', drv)


def _resolve_ppd_file_from_driver_id(drv, roots=None):
    """从 `lpinfo -m` 的驱动 id 解析出真实 PPD 文件路径，用于 `lpadmin -P` 兜底安装。

    驱动 id 形如 `foo2zjs:0/ppd/foo2zjs/HP-LaserJet_P1505n.ppd`。本机 CUPS 2.4 下
    `lpadmin -m <带 :N/ 代价前缀的 id>` 可能「rc=0 却生成半残队列（PPD 实际缺失）」，而
    `lpadmin -P <真实 PPD 文件>` 能稳定生成真实队列，故在 `-m` 兜底失败时用它绕开驱动库路径解析。

    解析策略：取驱动 id 末尾的文件名（如 `HP-LaserJet_P1505n.ppd`），在常见 PPD 根目录
    （/usr/share/ppd、/usr/share/cups/model、/usr/lib/cups/drivers 等）递归查找该文件名，
    【大小写不敏感、同时匹配 .ppd 与 .ppd.gz】。命中 .gz 时就地解压到临时文件并返回临时路径
    （lpadmin -P 不认 .gz）。找不到返回 None。0.2.47 重写为「按文件名递归查找（忽略大小写/.gz）」，
    因为实测 /usr/share/ppd/foo2zjs/... 并不存在（foo2zjs PPD 实际安装路径随发行版变化，且多为 .gz）。"""
    if not drv or drv in ("raw", "everywhere"):
        return None
    m = re.search(r'([^/\\]+\.ppd)(?:\.\w+)?$', drv, re.I)
    if not m:
        return None
    base = m.group(1)
    # 规范化搜索名：去 .ppd/.gz 后缀、小写、折叠非字母数字，用于跨大小写/分隔符比对实际文件名
    stem_n = _norm(re.sub(r'\.ppd.*$', '', base.lower()))
    if not stem_n:
        return None
    if roots is None:
        roots = []
        for r in ("/usr/share/ppd", "/usr/share/cups/model", "/usr/lib/cups/drivers",
                  "/usr/share/ppd/cups", "/usr/share/ppd/cupsfilters",
                  "/usr/share/cups/drv", "/opt"):
            if os.path.isdir(r):
                roots.append(r)
        if not roots:
            roots = ["/usr/share", "/usr/lib"]
    tmp = os.path.join(tempfile.gettempdir(), "printbox-" + base.lower())
    for r in roots:
        for path in glob.glob(os.path.join(r, "**", "*.ppd*"), recursive=True):
            bn = os.path.basename(path).lower()
            if stem_n not in _norm(re.sub(r'\.gz$', '', bn)):
                continue
            if path.lower().endswith(".gz"):
                try:
                    with gzip.open(path, "rb") as fin, open(tmp, "wb") as fout:
                        shutil.copyfileobj(fin, fout)
                    os.chmod(tmp, 0o644)
                    return tmp
                except Exception as e:
                    C.log("printer", "WARN", f"解压 PPD {path} 失败: {e}")
                    continue
            return path
    return None


def _ensure_error_policy_abort(name):
    """强制队列的 printer-error-policy=abort-job，从根因阻断『同一作业被 cupsd 反复重打』。

    根因（0.2.45 复盘，用户实测：1 页×5 份被打成 3 批×5 页=15 张、鸿蒙一直打印不停）：
    add_printer 新建分支经 `lpadmin -o printer-error-policy=abort-job` 设了该策略，但
    『队列已存在且健康』快速通道只做 URI 自愈 + PPD 补丁就 return，从未设置它。于是
    手工 lpadmin 建好队列后跑 `printbox republish` → 走健康分支 → 队列沿用 CUPS 默认策略
    （多为 retry-job）。后端每作业出纸后一旦报错，cupsd 便以**同一 job-ID** 重试整个作业
    → 表现为同一份被打 N 遍、中间有停顿（重试间隔）。这正是**服务端重打**，与客户端是否
    重复提交无关；重试复用同一 job-ID，故 guard_runaway_jobs（按 job-ID 去重）拦不住，
    只能从 ErrorPolicy 根因消除：任何后端/传输错误都让作业立即中止，绝不重打。"""
    if not printer_exists(name):
        return
    rc, _, err = C.run(["lpadmin", "-p", name, "-o", "printer-error-policy=abort-job"])
    if rc != 0:
        C.log("printer", "WARN", f"设置 printer-error-policy=abort-job 失败({name}): {err}")


def add_printer(uri, mdl, name=None, force=False):
    """添加（或升级）一台打印机。

    - 已存在但为 raw/无 PPD 队列时，force=True 会删掉重建为真实驱动（修复“收数据不出纸”）；
    - 调度器偶发不稳时 lpadmin 可能失败，这里会先确保 CUPS 在跑再重试；
    - 加完后校验 PPD：本应装真驱动却落成 raw 时明确报错并给出修复命令；
    - 默认 ErrorPolicy=abort-job：任何后端/传输错误都让作业立即中止，避免“无限重打同一作业”
      的打印洪流（这是通用止损策略，与具体硬件/系统无关）。
      注意：“单作业打印出多页”的真实根因通常是**后端 URI 与驱动不匹配**（如把 HPLIP 的
      `hp:/usb` 后端配给 foo2zjs/foo2xqx 驱动，后端会反复重发数据）；若观察到此现象，
      应优先校正设备 URI（见 _uri_priority：标准 usb:// 优先于 hp:/usb）。
    """
    # 同物理设备已存在队列则复用其名：避免 usb:// 与 hp:/usb 暴露同一打印机时被建成两个队列
    # （如 hp_laserjet_p1505n_printbox 与 laserjet_p1505n_printbox）。用 _uri_key 归一并比对，
    # 优先复用已存在的队列（不新增第二个名字），保证「一台物理机 = 一个 CUPS 队列」。
    if not name:
        for n in list_printers():
            if _uri_key(_current_uri(n)) == _uri_key(uri):
                name = n
                break
    base = _slug(mdl) or _slug(uri)
    name = name or (base + "_printbox" if base else "printer")
    exists = printer_exists(name)
    if exists and not force:
        ppd = _ppd_path(name)
        bad = _ppd_is_raw_or_missing(ppd)
        if bad:
            # 队列存在但 PPD 缺失/损坏/raw（历史崩溃把 PPD 写崩所致）：必须删除重建，否则 cupsd
            # 持续崩溃循环，且 patch 类函数因 PPD 不存在而静默跳过，队列永远修不好。此时 cupsd
            # 在跑，lpadmin -x 可用；删后落到下方新建分支重装真 foo2zjs 驱动 + A4 默认。
            C.log("printer", "WARN",
                  f"打印机 {name} 队列存在但 PPD {bad}，删除并重建为真实驱动")
            C.run(["lpadmin", "-x", name])
            exists = False
        else:
            # 已存在且驱动正常：URI 自愈 + 补打两份补丁（均幂等），确保 Live PPD 始终正确。
            _maybe_repoint_uri(name, uri, mdl)
            try:
                patch_ppd_defaults(name)
            except Exception as e:
                C.log("printer", "WARN", f"已存在队列 {name} 补打 A4 默认失败（可忽略）: {e}")
            try:
                patch_foo2xqx_ppd(name)
            except Exception as e:
                C.log("printer", "WARN", f"已存在队列 {name} 补打 foo2xqx 补丁失败（可忽略）: {e}")
            C.log("printer", "INFO",
                  f"打印机 {name} 已存在且驱动正常，已补正 PPD（过滤器路径 / 默认 A4）")
            # 0.2.45 复盘：健康快速通道此前漏设 ErrorPolicy，导致沿用 CUPS 默认 retry 策略 → 服务端重打。
            # 无论新建还是复用，都必须强制 abort-job。
            _ensure_error_policy_abort(name)
            return True
    if exists and force:
        C.run(["lpadmin", "-x", name])
    enable_job_archive()
    drv = pick_driver(mdl, uri)
    if not drv:
        # driverless/IPP 设备优先 everywhere；其余兜底 raw（先加进来，用户可后续在 WebUI 指定 PPD）
        drv = "everywhere" if (uri or "").lower().startswith(("ipp://", "ipps://")) else "raw"
    # 重试：CUPS 调度器偶发不稳定时 lpadmin 会失败，先确保调度器在跑再重试。
    # 某些发行版 lpinfo -m 返回的模型 id 带「代价」数字前缀（如 `foo2zjs:0/ppd/...` 中的 `0/`），
    # 直接传给 lpadmin -m 可能解析失败；这里把「原 id」与「去前缀 id(:0/ → :/)」都作为候选依次尝试。
    added = False
    last_err = ""
    drv_stripped = _strip_drv_cost(drv)
    # 候选顺序：去前缀的 scheme:/path 优先于带代价前缀的 scheme:N/path。
    # 根因（0.2.42 仍失败）：本机 CUPS 2.4 下，`lpadmin -m foo2zjs:0/ppd/...`（带 :N/ 代价）
    # 会「rc=0 却写出『引用了 PPD 路径但 /etc/cups/ppd/*.ppd 实际缺失』的半残队列」，cupsd 一加载就
    # `Scheduler shutting down due to program error` 崩溃循环；而手工验证可用的
    # `lpadmin -m foo2zjs:/ppd/...`（去前缀）能正常生成真实 PPD。故优先用去前缀形式。
    drv_candidates = []
    if drv_stripped != drv:
        drv_candidates.append(drv_stripped)
    drv_candidates.append(drv)
    is_real = (uri or "").lower().startswith(
        ("usb://", "hp:/", "ipp://", "ipps://", "socket://", "lpd://", "dnssd://", "mdns://"))
    for attempt in range(3):
        if not _ensure_cups():
            break
        for cand in drv_candidates:
            rc, _out, err = C.run(["lpadmin", "-p", name, "-E", "-v", uri, "-m", cand,
                                  "-o", "printer-error-policy=abort-job"])
            last_err = err
            if rc == 0:
                # 关键校验：lpadmin 可能 rc=0 却建成「无 PPD / raw」半残队列（尤其带 :N/ 代价的
                # 驱动 id）。必须确认 PPD 文件确实生成且非 raw，否则删掉重试用下一候选，
                # 绝不留半残队列（这正是 0.2.42 之前 iOS/鸿蒙/Win 全部打不出、cupsd 崩溃循环的根）。
                if is_raw_printer(name) or not os.path.exists(_ppd_path(name)):
                    C.log("printer", "WARN",
                          f"驱动 {cand} 添加返回成功但队列无真实 PPD（半残），删除并重试: {err}")
                    C.run(["lpadmin", "-x", name])
                    added = False
                    continue
                if cand != drv:
                    C.log("printer", "INFO", f"驱动 id 去前缀后添加成功: {cand}")
                added = True
                break
            if drv != "raw":
                C.log("printer", "WARN", f"驱动 {cand} 添加失败（第 {attempt + 1} 次）: {err}")
        # 兜底：lpadmin -m 的驱动 id（含 :N/ 代价前缀）在本机 CUPS 可能解析为半残队列
        # （rc=0 但 PPD 未生成）。若能从驱动 id 解析出真实 PPD 文件，改用 lpadmin -P 直接装，
        # 彻底绕开驱动数据库路径解析问题（0.2.46 新增）。
        if not added:
            ppd_file = _resolve_ppd_file_from_driver_id(drv)
            if ppd_file:
                rc, _o, err = C.run(["lpadmin", "-p", name, "-E", "-v", uri,
                                     "-P", ppd_file, "-o", "printer-error-policy=abort-job"])
                last_err = err
                if rc == 0 and not is_raw_printer(name) and os.path.exists(_ppd_path(name)):
                    C.log("printer", "INFO",
                          f"改用 lpadmin -P {ppd_file} 成功建立真实队列（绕开驱动库路径解析）")
                    added = True
                elif rc != 0:
                    C.log("printer", "WARN", f"lpadmin -P {ppd_file} 失败（第 {attempt + 1} 次）: {err}")
        if added:
            break
        time.sleep(1)
    if not added and drv != "raw":
        if is_real:
            # 真实打印机：绝不以 raw 静默兜底（raw 队列无真实 PPD、无 A4 选项，且 PDF 直传打印机会
            # 报错灯、不出纸，比「没建队列」更难排查）。明确报错，交由 repair/手动用正确驱动重建。
            C.log("printer", "ERROR",
                  f"真实打印机 {name} 的正确驱动 {drv} 添加失败（已尝试去前缀变体），不回退 raw。"
                  f"请检查 `lpinfo -m` 是否含该驱动且 CUPS 已稳定；CUPS 稳定后手动重建："
                  f"lpadmin -p {name} -E -v {uri} -m {drv}")
        else:
            C.log("printer", "WARN", f"驱动 {drv} 添加失败，回退 raw 以保证至少能进队列: {last_err}")
            drv = "raw"
            if _ensure_cups():
                rc, _out, err = C.run(["lpadmin", "-p", name, "-E", "-v", uri, "-m", drv,
                                      "-o", "printer-error-policy=abort-job"])
                added = rc == 0
                last_err = err
    if not added:
        C.log("printer", "ERROR", f"添加打印机失败: {last_err}")
        return False
    C.run(["lpadmin", "-p", name, "-o", "printer-is-shared=true"])
    C.run(["lpoptions", "-d", name])
    C.log("printer", "INFO", f"已添加/更新打印机 {name} (uri={uri}, driver={drv})")
    # 校验 PPD：本应装真驱动却落成 raw 时（含回退）明确报错，绝不静默
    if is_raw_printer(name):
        C.log("printer", "ERROR",
              f"打印机 {name} 被建成 raw 队列（无法出纸、无 A4 选项）。请手动重建为真实驱动："
              f"lpadmin -p {name} -E -v {uri} -m {drv}")
    write_airprint_service(name, mdl)
    # 所有非 raw 队列统一注入默认 A4（修复 iOS/鸿蒙 无 A4 选项；foo2zjs 等本机驱动此前未覆盖）
    patch_ppd_defaults(name)
    # foo2xqx(host-based) 打印机打补丁，修复移动端 JPEG(0 DPI) 空白页
    patch_foo2xqx_ppd(name)
    # 0.2.45 复盘：兜底强制一次，确保任何路径（新建/重建/健康复用）下队列都是 abort-job。
    _ensure_error_policy_abort(name)
    return True


def _ppd_make_model(name):
    """从 PPD 读取机型名（NickName/ModelName），用于 AirPrint 的 ty/product 字段。"""
    ppd = _ppd_path(name)
    try:
        with open(ppd, errors="ignore") as f:
            head = f.read(4000)
        for key in ("*NickName", "*ModelName"):
            m = re.search(key + r':\s*"([^"]*)"', head)
            if m:
                return m.group(1)
    except Exception:
        pass
    return ""


def ensure_avahi():
    """确保 avahi-daemon 运行并重新加载 /etc/avahi/services/*.service（AirPrint 发布依赖）。

    Debian trixie 下 avahi 由 avahi-daemon.socket 激活；仅 stop+start 服务可能留下旧的 socket
    状态，导致新写的 AirPrint 服务文件不被读取（表现为『文件存在但 avahi-browse 看不到』）。
    故先同时停止 socket+service 清除激活残留，再启动服务以干净重读服务目录。
    """
    C.run(["systemctl", "enable", "avahi-daemon"])
    # 同时停止 socket 与 service，清除 socket 激活残留状态，再启动服务以干净重读服务文件
    C.run(["systemctl", "stop", "avahi-daemon", "avahi-daemon.socket"])
    time.sleep(1)
    C.run(["systemctl", "start", "avahi-daemon"])
    time.sleep(1)
    rc, _, _ = C.run(["systemctl", "is-active", "--quiet", "avahi-daemon"])
    if rc == 0:
        C.log("printer", "INFO", "已确保 avahi-daemon 运行并重新加载服务文件（AirPrint 发布依赖）")
    else:
        C.log("printer", "WARN", "avahi-daemon 启动异常，AirPrint 发布可能失败\n" + _avahi_diag())
    # 别名服务依赖 avahi：avahi 重启后必须一并重启别名发布（printbox.local）
    C.run(["systemctl", "restart", "printbox-mdns-alias.service"])


def write_airprint_service(name, mdl=""):
    """生成显式 AirPrint 服务，确保 iOS/macOS/Android 通过 mDNS 发现打印机。

    mdl 优先取调用方传入的机型，缺失时回退读取 PPD 真实机型名。
    pdl 声明 application/pdf：CUPS 内置 cups-filters 会把手机发来的 PDF 转换成
    打印机原生格式（foo2xqx 等），无需打印机自身支持 PDF。
    """
    if not mdl:
        mdl = _ppd_make_model(name) or name
    # 用队列名作为展示名：已是 [a-z0-9_-] 的干净稳定字符串，iOS/鸿蒙/Win 均直接识别，
    # 不再拼机型名（含空格/斜杠）也不再用 avahi 的 %h 主机名通配符（本机主机名解析异常时
    # 会变成 “xxx @ /root” 这类怪名）。这是 0.2.24 修复“鸿蒙显示 @ /root / iOS 不显名”的根因。
    label = name
    svc = "/etc/avahi/services"
    try:
        os.makedirs(svc, exist_ok=True)
        path = os.path.join(svc, f"airprint-{name}.service")
        tmp = path + ".tmp"
        lines = []
        lines.append("<?xml version='1.0' standalone='no'?>\n")
        lines.append('<!DOCTYPE service-group SYSTEM "avahi-service.dtd">\n')
        lines.append('<service-group>\n')
        lines.append(f'  <name replace-wildcards="no">{_xml_esc(label)}</name>\n')
        lines.append('  <service>\n')
        # 只声明基础类型 _ipp._tcp，【不再】显式写 <subtype>_universal._sub._ipp._tcp</subtype>。
        # 原因：Avahi 对 TXT 含 image/urf 的 _ipp._tcp 服务会【自动合成】_universal._sub._ipp._tcp
        # 子类型（Avahi 的 AirPrint 支持特性）。若再显式写 <subtype>，同一服务会被注册两次子类型、
        # 名字相同 → iOS 按子类型浏览时看到「两台同名」（实测：鸿蒙/Win10 按基础类型只看到 1 台，
        # iOS 按子类型看到 2 台同名，正是 0.2.31 的现象）。去掉显式 <subtype> 后仅剩自动合成的那条，
        # iOS 也只看到 1 台；Mopria(Android/HarmonyOS)/Windows 按基础类型 _ipp._tcp 发现不受影响。
        # 备忘：子类型元素名必须是 <subtype> 而非 <sub>——曾误写 <sub> 被 avahi 判非法丢弃，
        #  现已无需显式声明，但保留此注释以免后人误加回 <sub> 或错误的 <subtype>。
        lines.append('    <type>_ipp._tcp</type>\n')
        lines.append('    <port>631</port>\n')
        for k, v in [
            ("txtvers", "1"), ("qtotal", "1"),
            ("Transparent", "T"),
            ("rp", f"printers/{name}"), ("ty", label),
            ("adminurl", "http://%h.local:631/printers/" + name),
            ("note", "PrintBox"),
            ("product", f"({label})"),
            # iOS/macOS 的 AirPrint 探测强制要求 pdl 含 image/urf，仅 application/pdf 时
            # iOS 会直接把设备从候选列表移除（Apple StackExchange + Debian CUPS AirPrint 补丁均确认）。
            ("pdl", "application/pdf,image/urf"),
            ("Color", "F"), ("Duplex", "F"), ("paper", "A4"),
            ("URF", "W8,SRGB24,CP1,RS300"),
        ]:
            lines.append(f'    <txt-record>{_xml_esc(k)}={_xml_esc(v)}</txt-record>\n')
        lines.append('  </service>\n')
        lines.append('</service-group>\n')
        content = "".join(lines)
        # 1) 写前自检：用 expat 确保内容合法，绝不把坏 XML 交给 avahi
        try:
            xml.parsers.expat.ParserCreate().Parse(content, True)
        except Exception as e:
            C.log("printer", "ERROR",
                  f"生成的 AirPrint 服务 XML 非法，已放弃写入 {path}: {e}")
            return
        # 2) 原子写：临时文件写满后再 rename，avahi 只会看到完整文件，不再有半截读取窗口
        with open(tmp, "w") as f:
            f.write(content)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
        C.log("printer", "INFO", f"已生成 AirPrint 服务: {path}")
    except Exception as e:
        C.log("printer", "WARN", f"写 AirPrint 服务失败: {e}")


def _clean_stale_airprint_services():
    """删除没有对应真实 CUPS 打印机的 AirPrint 服务文件，避免死队列（如幽灵 `print`）
    继续被广播，干扰手机/电脑发现。"""
    sdir = "/etc/avahi/services"
    if not os.path.isdir(sdir):
        return
    live = set(list_printers())
    for fn in os.listdir(sdir):
        if fn.startswith("airprint-") and fn.endswith(".service"):
            name = fn[len("airprint-"):-len(".service")]
            if name not in live:
                try:
                    os.remove(os.path.join(sdir, fn))
                    C.log("printer", "INFO",
                          f"已清理无对应打印机的 AirPrint 服务文件: {fn}")
                except Exception:
                    pass


def prune_phantom_printers():
    """删除 CUPS 中的幽灵打印机队列。三类：

    1) 通用名幽灵队列（print / printer / printers 等）：100% 是 PrintBox 早期版本在 mdl 为空时
       误建的兜底名，绝不是用户主动命名。其设备 URI 可能是网络后端（ipp/socket/dnssd），
       不出现于 lpinfo -v，旧逻辑只删本地后端故漏掉了它——这正是「实际只插了 1505 却冒出
       print 幽灵队列、iPhone 看到空名/打印白纸」的根因。
    2) 双后缀脏队列（xxx_printbox_printbox）：自愈线程曾把本机广播当新设备反复重建而成。
    3) 同物理设备暴露多个队列（早期误建短名 / 双后缀）：仅保留一个，详见 _dedupe_printers。

    网络打印机（用户主动添加的 ipp/socket/lpd）名称不会是上述通用名，故不误伤。"""
    _dedupe_printers()
    GENERIC_PHANTOM_PREFIX = {"print", "printer", "printers", "generic",
                              "local", "remote", "network", "usb", "unknown", ""}
    rc, out, _ = C.run(["lpinfo", "-v"])
    present = out or ""
    for name in list_printers():
        # 情形 1/2：通用兜底名（含 <通用词>_printbox）或双后缀脏队列——必为 PrintBox 误建，
        # 直接删除（无论其 URI 是什么后端）。注意队列名恒为 <slug>_printbox，故必须按前缀判定，
        # 否则 print_printbox 这种「通用词+_printbox」会被漏掉、继续被广播成幽灵打印机。
        prefix = name[:-len("_printbox")] if name.endswith("_printbox") else name
        if prefix in GENERIC_PHANTOM_PREFIX or name.endswith("_printbox_printbox"):
            rc2, _, err = C.run(["lpadmin", "-x", name])
            if rc2 == 0:
                C.log("printer", "INFO", f"已删除幽灵打印机队列: {name}")
            else:
                C.log("printer", "WARN", f"删除幽灵队列 {name} 失败: {err}")
            continue
        uri = _current_uri(name)
        u = (uri or "").lower()
        if not u:
            continue
        # 情形 3：本地后端里，伪设备（parallel:/dev/lp0、scsi:/...）即便出现在
        # lpinfo -v 中也是无真实打印机的假设备，必须删除（这正是 print_printbox 的根因）。
        # 真实设备（usb://、hp:/usb）且不在 lpinfo -v 中才删（设备已移除）。
        if u.startswith(("usb://", "hp:/usb", "hp://", "parallel:", "scsi:", "serial:")):
            if not _is_real_printer(uri):
                rc2, _, err = C.run(["lpadmin", "-x", name])
                if rc2 == 0:
                    C.log("printer", "INFO",
                          f"已删除伪设备队列（parallel/scsi/serial 假设备）: {name}（uri={uri}）")
                else:
                    C.log("printer", "WARN", f"删除伪设备队列 {name} 失败: {err}")
                continue
            if uri not in present:
                rc2, _, err = C.run(["lpadmin", "-x", name])
                if rc2 == 0:
                    C.log("printer", "INFO",
                          f"已删除幽灵打印机队列（设备已移除/不存在）: {name}")
                else:
                    C.log("printer", "WARN", f"删除幽灵队列 {name} 失败: {err}")
    # 情形 4：自引用 DNS-SD 幽灵队列 + 空 URI 死队列。
    # 本机发布 AirPrint 后，CUPS 的 dnssd 后端会把已存在队列当成新设备
    # ipp://<name>._ipp._tcp.local/ 重新列进 lpinfo -v；早期版本未跳过便据此建出
    # 名字不同的队列（如 laserjet_p1505n_printbox / print_printbox），其 URI 指向自身、
    # 无法出纸，且 _dedupe 按设备 URI 归一时与真实 usb:// 队列 key 不同不会被合并，必须专门清除。
    # 另外：CUPS 重启后个别队列 DeviceURI 可能短暂为空，若该 _printbox 队列已无 URI、
    # 且本机已有「有效」打印机（含真实 URI 且非自引用），则判定为死队列删除（detect/自愈会重建）。
    existing = set(list_printers())
    has_valid = any(
        (lambda u: bool(u) and not _is_self_dnssd(u, existing))(_current_uri(n))
        for n in existing
    )
    for name in list(list_printers()):
        uri = _current_uri(name)
        u = (uri or "").lower()
        if u and _is_self_dnssd(uri, existing):
            rc2, _, err = C.run(["lpadmin", "-x", name])
            if rc2 == 0:
                C.log("printer", "INFO", f"已删除自引用 DNS-SD 幽灵队列: {name}（uri={uri}）")
            else:
                C.log("printer", "WARN", f"删除自引用幽灵队列 {name} 失败: {err}")
            continue
        if (not u) and name.endswith("_printbox") and has_valid:
            rc2, _, err = C.run(["lpadmin", "-x", name])
            if rc2 == 0:
                C.log("printer", "INFO", f"已删除空 URI 死队列（自愈将重建）: {name}")
            else:
                C.log("printer", "WARN", f"删除空 URI 队列 {name} 失败: {err}")
            continue
    _clean_stale_airprint_services()


def _sh_q(s):
    """systemd ExecStart 按空白分词且支持引号；把含空格的参数用双引号包住，避免被拆开。"""
    s = str(s)
    if any(c in s for c in " \t\"'"):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _airprint_visible(timeout=6):
    """本机 AirPrint(_universal._sub._ipp._tcp) 是否在局域网可见。

    同时查询子类型与基础类型 _ipp._tcp（部分客户端按基础类型浏览），
    任一命中即视为可见。带 8 秒缓存，避免页面/定时任务频繁触发 avahi-browse。"""
    global _AV_CACHE
    now = time.time()
    if _AV_CACHE["val"] is not None and now - _AV_CACHE["ts"] < 8:
        return _AV_CACHE["val"]
    end = time.time() + timeout
    found = False
    while time.time() < end:
        try:
            rc, out, _ = C.run(["avahi-browse", "-rt", "_universal._sub._ipp._tcp"])
        except Exception:
            rc, out = 1, ""
        if rc == 0 and out and ("p1505" in out.lower() or "laserjet" in out.lower()
                                or "hp_laserjet" in out.lower()
                                or "ipp._tcp" in out or "universal" in out):
            found = True
            break
        try:
            rc2, out2, _ = C.run(["avahi-browse", "-rt", "_ipp._tcp"])
        except Exception:
            rc2, out2 = 1, ""
        if rc2 == 0 and out2 and ("p1505" in out2.lower() or "laserjet" in out2.lower()
                                  or "hp_laserjet" in out2.lower()):
            found = True
            break
        time.sleep(1)
    _AV_CACHE = {"ts": time.time(), "val": found}
    return found


def airprint_visible():
    """公开封装：供 WebUI / 自检调用。"""
    return _airprint_visible()


def _stop_dynamic_airprint():
    """停止并禁用动态 avahi-publish 兜底服务，确保「静态 .service 文件」成为唯一广播源。

    否则「静态文件（基础类型+子类型）」与「动态 avahi-publish（仅基础类型）」会同时广播
    同一实例，iOS 按基础类型与子类型各发现一次 → 同一打印机显示为两个。"""
    for svc in ("printbox-airprint-publish.service",):
        C.run(["systemctl", "stop", svc])
        C.run(["systemctl", "disable", svc])


def _uri_key(uri):
    """把设备 URI 归一化为「物理设备标识」用于去重。

    同物理打印机常经多个后端暴露（如 `usb://HP/LaserJet%20P1505n?serial=KQ12ZY6`
    与 `hp:/usb/HP_LaserJet_P1505n?serial=KQ12ZY6`），型号串还带有/不带厂商前缀、
    用 `/`、`_`、`%20`、空格等不同分隔符。旧逻辑只做浅归一化，这两种 URI 归不到一起，
    导致同一台打印机被建成 `hp_laserjet_p1505n_printbox` 与 `laserjet_p1505n_printbox`
    两个队列（手机端看到重复打印机）。

    新策略：优先用序列号（最可靠，同设备跨后端一致）；否则去掉协议头、厂商前缀，
    并把 `/`、`_`、`%20`、空格折叠为单空格后再去厂商词，只留型号。"""
    u = (uri or "").lower().strip()
    if not u:
        return ""
    # 1) 序列号优先：绝大多数同物理设备在不同后端下 serial 一致
    m = re.search(r"serial=([^&?\s\"']+)", u)
    if m:
        return "serial:" + m.group(1).strip("'\"")
    # 2) 主体：去协议头、去厂商前缀、折叠分隔符
    body = re.sub(r"^[a-z]+://", "", u)       # 去 usb:// / ipp:// ...
    body = re.sub(r"^[a-z]+:/", "", body)     # 去 hp:/usb -> usb/...
    body = re.sub(r"^usb/", "", body)         # 去 usb/ 前缀
    body = re.sub(r"^hp/", "", body)          # 去 HP/ 厂商前缀
    body = body.split("?")[0]                 # 忽略 ?query
    body = re.sub(r"[ /_%20]+", " ", body).strip()   # 折叠分隔符为单空格
    body = re.sub(r"\b(hp|hewlett|packard)\b", "", body).strip()  # 去厂商词
    return body


def _keeper_score(name):
    """同设备多队列时选保留者的评分（越高越优先）。"""
    score = 0
    if name.endswith("_printbox_printbox"):
        score -= 100      # 双后缀脏队列，最不愿保留
    if is_raw_printer(name):
        score -= 10
    low = name.lower()
    # 偏好带厂商关键字（如 hp）的规范名，避免把 hp_laserjet_p1505n_printbox 误删、
    # 反而留下 laserjet_p1505n_printbox 这类缺厂商前缀的短名
    if "hp" in low:
        score += 10
    # 名称前缀为通用兜底词（print/printer/...）的队列是误建幽灵，最不愿保留
    prefix = name[:-len("_printbox")] if name.endswith("_printbox") else name
    if prefix in {"print", "printer", "printers", "generic", "local",
                  "remote", "network", "usb", "unknown", ""}:
        score -= 50
    if name.endswith("_printbox"):
        score += 5        # 本系统管理的规范名
    return score


def _dedupe_printers():
    """同一物理设备（相同设备 URI）可能暴露多个 CUPS 队列（早期版本误建、自愈回退 raw、
    双后缀脏队列等）。每个物理设备只保留一个队列（评分：双后缀/raw 最低、规范名 _printbox 优先），
    其余删除，避免 AirPrint 把同一台打印机广播成多个、客户端重影 / 连接失败。"""
    try:
        rc, out, _ = C.run(["lpstat", "-v"])
        if rc != 0 or not out:
            return
        by_uri = {}
        for ln in out.splitlines():
            # 真实 `lpstat -v` 输出形如 "device for NAME: uri"（冒号分隔，没有 "is" 这个词）。
            # 早期误写成 "device for NAME is URI" 的正则永远匹配不到、整段 dedupe 不执行，
            # 导致重复队列（hp_laserjet_p1505n_printbox 与 laserjet_p1505n_printbox）删不掉、iOS 双显。
            # 用非贪婪 (\S+?) 在冒号前停下，正确取出不带冒号的队列名。
            m = re.search(r"device for\s+(\S+?):\s+(\S+)", ln)
            if not m:
                continue
            name, uri = m.group(1), m.group(2)
            key = _uri_key(uri)
            if not key:
                continue
            by_uri.setdefault(key, []).append(name)
        for uri, grp in by_uri.items():
            if len(grp) <= 1:
                continue
            keeper = max(grp, key=_keeper_score)
            for n in grp:
                if n != keeper:
                    rc2, _, err = C.run(["lpadmin", "-x", n])
                    if rc2 == 0:
                        C.log("printer", "INFO",
                              f"已删除重复队列（同设备 URI，保留 {keeper}）: {n}")
                    else:
                        C.log("printer", "WARN", f"删除重复队列 {n} 失败: {err}")
    except Exception as e:
        C.log("printer", "WARN", f"去重打印机队列失败: {e}")


def _write_airprint_services():
    """为所有可发布打印机写入 Avahi 服务文件并清理幽灵服务。

    先停掉动态兜底服务，保证静态文件是唯一广播源（避免 iOS 出现重复打印机）。"""
    _stop_dynamic_airprint()
    for name in list_printers():
        uri = _current_uri(name)
        if not uri:
            C.log("printer", "WARN",
                  f"跳过无设备 URI 的队列（无法出纸，不发布到 AirPrint）: {name}")
            continue
        if is_raw_printer(name):
            C.log("printer", "WARN",
                  f"跳过无 PPD 的队列（无法出纸，不发布到 AirPrint）: {name}")
            continue
        write_airprint_service(name, _ppd_make_model(name))
        # 重新发布时一并确保默认 A4（修复升级后 iOS/鸿蒙 无 A4 选项，无需手工改 PPD）
        patch_ppd_defaults(name)
        # 一并补打 foo2xqx 过滤器补丁：republish 路径历史上只打 A4、漏打过滤器路径，
        # 导致升级后 Live PPD 仍指向被删的 /usr/local/bin wrapper → 作业失败/cupsd 崩溃。
        # 两个补丁均幂等，重复调用安全。
        try:
            patch_foo2xqx_ppd(name)
        except Exception as e:
            C.log("printer", "WARN", f"重新发布时补打 foo2xqx 补丁失败（可忽略）: {e}")
    _clean_stale_airprint_services()


def avahi_publish_service_dynamic(name, mdl):
    """兜底：用 avahi-publish 动态持续发布打印机服务（Restart=always），
    不依赖静态 .service 文件被 avahi 重新加载。当静态文件多次重试仍不可见时使用，
    杜绝“文件已写却 avahi 不注册”导致 iOS/鸿蒙/Win 永远搜不到的死局。"""
    binp = shutil.which("avahi-publish") or "/usr/bin/avahi-publish"
    if not os.path.exists(binp):
        return False, "缺少 avahi-publish 工具（需安装 avahi-utils）"
    label = name
    # 移除静态文件，避免与动态发布重复广播
    svc = f"/etc/avahi/services/airprint-{name}.service"
    try:
        if os.path.exists(svc):
            os.remove(svc)
    except Exception:
        pass
    txt = [
        "txtvers=1", "qtotal=1", "Transparent=T",
        f"rp=printers/{name}", f"ty={label}",
        f"adminurl=http://%h.local:631/printers/{name}",
        "note=PrintBox", f"product=({label})",
        "pdl=application/pdf,image/urf",
        "Color=F", "Duplex=F", "paper=A4", "URF=W8,SRGB24,CP1,RS300",
    ]
    args = [binp, "-s", label, "_ipp._tcp", "631"] + txt
    unit = (
        "[Unit]\n"
        "Description=PrintBox AirPrint 动态发布（兜底）\n"
        "After=avahi-daemon.service\n"
        "Requires=avahi-daemon.service\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={' '.join(_sh_q(a) for a in args)}\n"
        "Restart=always\n"
        "RestartSec=2\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    path = "/etc/systemd/system/printbox-airprint-publish.service"
    try:
        with open(path, "w") as f:
            f.write(unit)
    except Exception as e:
        return False, f"写入动态发布单元失败: {e}"
    C.run(["systemctl", "daemon-reload"])
    C.run(["systemctl", "enable", "printbox-airprint-publish.service"])
    rc, _, err = C.run(["systemctl", "restart", "printbox-airprint-publish.service"])
    if rc == 0:
        return True, "已用动态发布兜底（avahi-publish -s）"
    return False, f"动态发布失败: {err}"


def ensure_airprint_published():
    """发布并自检：先静态文件多次重试，仍不可见则切换为动态发布兜底。

    返回最终是否可见。用于 repair / 定时器 / WebUI「重新发布」——尽量自动完成，
    不要求用户每次都手动上 WebUI 操作。"""
    for attempt in range(3):
        _write_airprint_services()
        ensure_avahi()
        if _airprint_visible(timeout=5):
            return True
        C.log("printer", "WARN", f"AirPrint 静态发布自检未通过，第 {attempt + 1}/3 次重试")
    # 静态文件多次重试仍不可见：切换动态发布兜底
    C.log("printer", "WARN", "静态 AirPrint 服务多次重试仍不可见，改用 avahi-publish 动态发布兜底")
    for name in list_printers():
        if is_raw_printer(name):
            continue
        ok, msg = avahi_publish_service_dynamic(name, _ppd_make_model(name))
        C.log("printer", "INFO" if ok else "WARN", f"动态发布 {name}: {msg}")
    return _airprint_visible(timeout=5)


def _heal_empty_uris():
    """CUPS 重启后个别队列的 DeviceURI 可能短暂为空（lpstat -v 暂为空）。若此时就去发布
    AirPrint，会因「无 URI」跳过，导致该打印机在局域网不可见、WebUI 概览误报「未共享/未发布」。
    对每个本系统管理的队列（名字含 _printbox），若当前 URI 为空，从 lpinfo -v 重新定位同机型
    真实设备（usb:// 优先，跳过自引用 DNS-SD 与裸后端）并写回，等价于 add_printer 的空 URI 自愈。
    必须在 prune 之后调用：先清掉幽灵队列，再把真实队列的空 URI 校正回来，绝不丢打印机。"""
    devs = []
    for _ in range(4):  # CUPS 重启后 USB 后端枚举可能滞后，最多重试 ~6s
        try:
            devs = get_devices()
        except Exception:
            devs = []
        if devs:
            break
        time.sleep(2)
    if not devs:
        return
    existing = set(list_printers())
    best = {}
    for uri, mdl in devs:
        if _is_self_dnssd(uri, existing) or not _is_real_printer(uri):
            continue
        key = _norm(mdl) or uri
        pri = _uri_priority(uri)
        if key not in best or pri < best[key][0]:
            best[key] = (pri, uri, mdl)
    if not best:
        return
    for name in list_printers():
        if _current_uri(name):
            continue
        base = name[:-len("_printbox")] if name.endswith("_printbox") else name
        key = _norm(base)
        if key in best:
            uri, mdl = best[key][1], best[key][2]
            C.log("printer", "INFO",
                  f"自愈：将 {name} 空设备 URI 校正为 {uri}（修复卡队列/出纸/AirPrint 不发布）")
            _maybe_repoint_uri(name, uri, mdl)


def republish_airprint():
    """为所有已配置打印机重新生成 Avahi 服务文件并刷新 avahi-daemon。

    用于「手动改了设备 URI / 重装驱动 / 手机找不到打印机」后，让局域网发布与新状态一致。
    无 PPD 的裸队列（无法出纸）不发布到 AirPrint，仅保留在 CUPS 中由用户后续配置。
    自动重试 + 自检，必要时切换动态发布兜底；失败会把证据写进日志便于远程诊断。"""
    try:
        prune_phantom_printers()
    except Exception:
        pass
    try:
        _heal_empty_uris()
    except Exception:
        pass
    # 自愈补全 URI 后，可能让原本因「URI 为空」未被合并的重复队列（同设备、不同名字）
    # 此刻有了有效 URI、可被 _uri_key 归并——但上面的 prune 在 heal 之前跑、当时 URI 还空，
    # 抓不到它们。这里再剪一次，保证 repair 永远收敛到「一台物理机 = 一个队列」，
    # 不再需要人工 lpadmin -x。
    try:
        prune_phantom_printers()
    except Exception:
        pass
    ok = ensure_airprint_published()
    if ok:
        C.log("printer", "INFO", "已重新发布全部打印机到局域网(AirPrint/mDNS)，本机自检可见")
    else:
        C.log("printer", "WARN",
              "AirPrint 自检仍未发现服务（静态+动态兜底均失败）。\n" + _avahi_diag())


def guard_runaway_jobs(max_copies=1):
    """安全网：取消同一 (打印机, 用户, 文档名) 下**多个不同 job-ID** 的重复在途作业。

    ⚠️ 适用范围澄清（0.2.45 复盘）：本函数只能拦住「客户端真的提交了 N 个独立作业」的情况
    （此时会有 N 个不同 job-ID）。它**无法**拦住「cupsd 对同一 job-ID 反复重试」——因为重试
    复用同一个 job-ID，本函数看到的是「1 个作业」，不会取消。而用户实测的『1 页×5 份打出 3 批×5 页』
    与『鸿蒙一直打印不停』正是 cupsd 重试同一 job-ID 的服务端重打，其根因与正确修复是
    `_ensure_error_policy_abort()`（printer-error-policy=abort-job），见该函数说明。本函数仅作为
    针对「客户端确实多提交了不同 job-ID」的二级安全网，不应被当作上述症状的主修复。

    策略（保守、绝不误伤不同文档的正常作业）：
      - 解析 `lpstat -l -o` 拿到每份在途作业的 打印机/用户/文档名/状态；
      - 仅对「同名同用户同打印机、且状态为在途(pending/queued/processing/held)」的作业分组；
      - 组内**不同 job-ID 数量** > max_copies 时，按作业号升序保留前 max_copies 份，**取消其余重复项**；
      - 文档名为空（无法判定重复）的作业一律不取消，避免误杀；
      - cupsd 未运行或 lpstat 解析失败则安全跳过。
    由 systemd 定时器 printbox-guard.timer 每 ~10s 调用一次。"""
    try:
        rc, out, _ = C.run(["lpstat", "-l", "-o"])
    except Exception:
        return 0
    if rc != 0 or not out:
        return 0
    # 解析 lpstat -l -o：每块以 "<printer>-<id> <user> <size> <date> <time>" 开头，
    # 其下缩进行含 job-name: / job-state:。
    jobs = []
    cur = None
    for ln in out.splitlines():
        s = ln.strip()
        if not s:
            continue
        if not ln.startswith(" ") and not ln.startswith("\t"):
            parts = s.split()
            if len(parts) >= 2 and "-" in parts[0]:
                printer, jobid = parts[0].rsplit("-", 1)
                user = parts[1] if len(parts) > 1 else ""
                cur = {"printer": printer, "jobid": jobid, "user": user,
                       "name": "", "state": ""}
                jobs.append(cur)
        else:
            if cur is None:
                continue
            low = s.lower()
            if low.startswith("job-name:"):
                cur["name"] = s.split(":", 1)[1].strip()
            elif low.startswith("job-state:"):
                cur["state"] = s.split(":", 1)[1].strip()
    active_states = ("pending", "queued", "processing", "held")
    active = [j for j in jobs
              if (j["state"] == "" or any(st in j["state"].lower() for st in active_states))]
    # 仅在文档名非空时按 (printer, user, name) 分组；空名不判定重复，避免误杀不同作业
    from collections import defaultdict
    groups = defaultdict(list)
    for j in active:
        if not j["name"]:
            continue
        groups[(j["printer"], j["user"], j["name"])].append(j)
    cancelled = 0
    for key, grp in groups.items():
        if len(grp) <= max_copies:
            continue
        grp_sorted = sorted(grp, key=lambda x: int(x["jobid"]) if x["jobid"].isdigit() else 0)
        for j in grp_sorted[max_copies:]:
            full = f'{j["printer"]}-{j["jobid"]}'
            rc2, _, err = C.run(["cancel", full])
            if rc2 == 0:
                cancelled += 1
                C.log("printer", "WARN",
                      f"已取消重复作业 {full}（同文档重复提交，防护打印洪流）")
            else:
                C.log("printer", "WARN", f"取消重复作业 {full} 失败: {err}")
    if cancelled:
        C.log("printer", "INFO", f"打印洪流防护：共取消 {cancelled} 个重复在途作业")
    return cancelled


def self_heal_publish():
    """供定时任务 / 自动维护调用：若 AirPrint 在局域网不可见则自动重新发布。

    无需人工干预——用户不必每次都登上 WebUI 手动点「重新发布」。"""
    if not list_printers():
        return "无打印机，跳过 AirPrint 自检"
    if _airprint_visible(timeout=5):
        C.log("printer", "INFO", "AirPrint 自检已可见，无需操作")
        return "AirPrint 已可见"
    C.log("printer", "WARN", "AirPrint 自检不可见，触发自动重新发布")
    ensure_airprint_published()
    return "已自动重新发布 AirPrint"


def _avahi_diag():
    parts = []
    rc, jl, _ = C.run(["journalctl", "-u", "avahi-daemon", "-n", "50", "--no-pager"])
    parts.append("[journalctl avahi]\n" + (jl or "(无)"))
    sdir = "/etc/avahi/services"
    if os.path.isdir(sdir):
        for fn in os.listdir(sdir):
            if fn.endswith(".service"):
                try:
                    with open(os.path.join(sdir, fn)) as f:
                        parts.append(f"\n--- {fn} ---\n" + f.read())
                except Exception:
                    pass
    return "\n".join(parts)


def list_printers():
    rc, out, _ = C.run(["lpstat", "-a"])
    names = []
    for ln in (out or "").splitlines():
        parts = ln.split()
        if parts:
            names.append(parts[0])
    return names


def _cups_running():
    """CUPS 调度器是否在运行（`lpinfo` 枚举设备依赖它）。

    必须精确匹配 "scheduler is running"：`lpstat -r` 在 cupsd 未运行时输出
    "scheduler is not running"，其中也含 "running" 子串，若用 `in` 粗匹配会误判为「在跑」，
    导致 _ensure_cups 以为调度器已就绪而不去启动它（真实潜在 bug，已在此修正）。"""
    rc, out, _ = C.run(["lpstat", "-r"])
    return "scheduler is running" in (out or "").lower()


def _ensure_cups():
    """探测前确保 CUPS 调度器已运行；否则 `lpinfo` 返回空、detect 静默无果。

    返回 True 表示可用；False 表示无法启动（包未装/启动失败），调用方应给出明确报错，
    而不是让用户以为“没插打印机”。"""
    if _cups_running():
        return True
    C.log("printer", "WARN", "CUPS 调度器未运行，尝试启动...")
    C.run(["systemctl", "enable", "cups"])
    rc, _, err = C.run(["systemctl", "start", "cups"])
    if rc != 0:
        C.log("printer", "ERROR", f"CUPS 启动失败: {err}")
        return False
    for _ in range(10):
        if _cups_running():
            return True
        time.sleep(0.5)
    return _cups_running()


def main():
    C.log("printer", "INFO", "开始探测打印机...")
    if not _ensure_cups():
        C.log("printer", "ERROR",
              "CUPS 调度器未运行且无法启动，无法探测打印机。请检查 "
              "`systemctl status cups` 与 /var/log/cups/error_log；"
              "若未安装请先 `apt-get install cups cups-client` 后重试。")
        return 1
    # 刚启动/重启的 cupsd 需要几秒让 usb 后端枚举出设备；先短暂稳定再探测，
    # 避免首轮 lpinfo 空窗漏建队列（与 repair 路径中「先稳定 CUPS 再建队列」同源）。
    time.sleep(2)
    # 重试枚举：cupsd 刚重启后 USB 后端枚举可能滞后数秒，单次 lpinfo 易返回空 →
    # 误判「未发现打印机」→ 不建队列 → 盒子仍空。最多重试 ~16s 给后端枚举时间。
    devs = []
    for _ in range(8):
        devs = get_devices()
        if devs:
            break
        time.sleep(2)
    if not devs:
        C.log("printer", "WARN",
              "未发现打印机设备（lpinfo -v 为空）；请确认打印机已开机并连接、CUPS 后端正常")
        return 0
    # 按机型去重：同一物理打印机可能暴露多个 URI（如 hp:/usb 与 usb://），只加一个，且优先最优 URI
    best = {}  # key -> (priority, uri, mdl)
    for uri, mdl in devs:
        # 用 _uri_key（优先序列号）归一并去重：同一物理打印机经 usb:// 与 hp:/usb 多后端
        # 暴露时（型号串还带/不带厂商前缀），旧逻辑按 _norm(mdl) 归一会建出两个队列
        # （如 hp_laserjet_p1505n_printbox 与 laserjet_p1505n_printbox）。改用 _uri_key
        # 让两者归到同一 key，从探测源头只添一个队列，杜绝重复。
        key = _uri_key(uri) or _norm(mdl) or uri
        pri = _uri_priority(uri)
        if key not in best or pri < best[key][0]:
            best[key] = (pri, uri, mdl)
    added = False
    existing = set(list_printers())
    seen_keys = set()  # 物理设备去重：同一台打印机经 usb:// 与 hp:/usb 暴露时只加一个队列
    for _, uri, mdl in best.values():
        # 过滤本机已发布队列经 DNS-SD 被 CUPS 自身发现回来的「自引用」设备（详见 _is_self_dnssd）。
        # 不跳过会导致 detect/自愈定时线程反复把它当新打印机重建为 raw，覆盖正确队列、出不了纸。
        if _is_self_dnssd(uri, existing):
            C.log("printer", "INFO", f"跳过自引用 DNS-SD 设备（即本机已发布队列）: {uri}")
            continue
        if not _is_real_printer(uri):
            # parallel:/dev/lp0、scsi:/... 这类 CUPS 即便无真实设备也会列出的伪后端，直接跳过，
            # 避免建成 print_printbox 这类通用名幽灵队列、手机端看到空名/白纸。
            C.log("printer", "INFO", f"跳过伪设备（非真实打印机）: {uri}")
            continue
        key = _uri_key(uri)
        if key in seen_keys:
            C.log("printer", "INFO", f"跳过同物理设备的重复 URI（已处理）: {uri}")
            continue
        seen_keys.add(key)
        drivers.ensure_drivers(mdl)
        # 已存在但为 raw 队列时强制升级为真实驱动
        base = _slug(mdl) or _slug(uri)
        new_name = (base + "_printbox") if base else "printer"
        old_name = base  # 0.2.23 及更早版本生成的队列名（无 _printbox 后缀）
        # 已带 _printbox 后缀的队列是本系统自己管理的，绝不再追加一次后缀（避免 ..._printbox_printbox 双后缀脏队列）
        if old_name and old_name != new_name and not old_name.endswith("_printbox") and printer_exists(old_name):
            C.log("printer", "INFO", f"迁移旧队列名 {old_name} -> {new_name}")
            C.run(["lpadmin", "-x", old_name])
        name = new_name
        force = printer_exists(name) and is_raw_printer(name)
        if add_printer(uri, mdl, force=force):
            added = True
    if added:
        # 注意：不要重启 cups-browsed —— 它会重新发起订阅风暴拖垮 CUPS。
        # 打印机发布已由 write_airprint_service 写入的 Avahi 服务文件负责。
        C.log("printer", "INFO", "打印机探测完成（未触碰 cups-browsed）")
    # 收敛重复 / 幽灵队列（根因 B）：同物理设备经 usb:// 与 hp:/usb 多后端暴露、或因重启后
    # CUPS 重新枚举而建出第二个队列（如 hp_laserjet_p1505n_printbox 与 laserjet_p1505n_printbox）
    # 时，由 _dedupe_printers（按 _uri_key 含序列号归并）保留规范名、删除短名/双后缀/自引用队列，
    # 确保「一台物理打印机 = 一个 CUPS 队列 = iOS 只看到一台」。必须在写 AirPrint 服务文件之前收敛，
    # 否则错误的双队列会被一并广播出去，手机端又看到两台同名打印机。
    try:
        prune_phantom_printers()
    except Exception as e:
        C.log("printer", "WARN", f"收敛幽灵/重复队列失败（可忽略，下次探测会再收敛）: {e}")
    # 清理无对应队列的残留 AirPrint 服务文件（如曾手动 `lpadmin -x` 删除队列但未清文件，
    # 否则旧 .service 仍广播、手机端看到幽灵打印机）。republish/repair 路径也会调，这里再兜底一次。
    _clean_stale_airprint_services()
    # 所有打印机写盘后统一刷新一次 avahi（原子写已避免半截文件，这里再做一次干净重启兜底）
    ensure_avahi()
    return 0


if __name__ == "__main__":
    sys.exit(main())
