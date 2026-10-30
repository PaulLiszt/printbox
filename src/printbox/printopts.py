"""PrintBox 打印选项（通用，跨机型）。

通过 `lpoptions -l -p <打印机>` 读取该打印机 PPD 支持的、可由管理员设置的选项，
解析成结构化数据供 WebUI 渲染表单；保存时写入对应 `-default` 选项（CUPS 默认值）。
不绑定任何特定打印机型号——foo2zjs/gutenprint/hpcups/brlaser 等都只是不同的 PPD，
底层机制完全一致，因此本模块对所有 CUPS 打印机通用。
"""
import re

from . import config as C


def list_printers():
    from . import printers as PS
    return PS.list_printers()


def get_default_printer():
    return C.get_default_printer()


def parse_lpoptions(text):
    """解析 `lpoptions -l` 输出。

    每行形如：  OptionName/Human Description: choiceA *choiceB choiceC
    `*` 标记当前生效值。返回 [{name, desc, choices:[{value,current}]}]。
    选项值可能含空格，按空白切分；当前值以 `*` 开头。
    """
    opts = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line or ":" not in line:
            continue
        head, rest = line.split(":", 1)
        if "/" in head:
            name, desc = head.split("/", 1)
        else:
            name, desc = head, ""
        name = name.strip()
        if not name:
            continue
        choices = []
        for tok in rest.split():
            cur = tok.startswith("*")
            val = tok[1:] if cur else tok
            if val:
                choices.append({"value": val, "current": cur})
        if choices:
            opts.append({"name": name, "desc": desc.strip(), "choices": choices})
    return opts


def get_printer_options(name):
    """返回 (options_list, err)。options_list 为空且 err 为空表示驱动无可选设置。"""
    rc, out, err = C.run(["lpoptions", "-l", "-p", name])
    if rc != 0:
        return [], err
    return parse_lpoptions(out), ""


def _printer_device_uri(name):
    """读取打印机设备 URI，优先直接解析 /etc/cups/printers.conf（最可靠、不依赖子进程输出），
    失败再回退 lpstat -v。任一成功即返回 URI 字符串。"""
    # 1) printers.conf：每个 <Printer name> 块里有 DeviceURI 行
    try:
        with open("/etc/cups/printers.conf", errors="ignore") as f:
            txt = f.read()
        m = re.search(r"<Printer\s+" + re.escape(name) + r">.*?DeviceURI\s+(\S+)",
                      txt, re.S)
        if m:
            return m.group(1)
    except Exception:
        pass
    # 2) 回退：lpstat -v
    try:
        rc, vout, _ = C.run(["lpstat", "-v", name])
        if rc == 0:
            m = re.search(r"device for\s+\S+\s+is\s+(\S+)", vout)
            if m:
                return m.group(1)
    except Exception:
        pass
    return ""


def _printer_shared(name):
    """读取打印机是否「共享」（发布到局域网的前提）。

    优先解析 /etc/cups/printers.conf 的 <Printer name> 块里的 `Shared Yes/No`（最可靠，不依赖
    子进程输出），失败再回退 `lpstat -l -p`（长格式含 `Shared: Yes`）。
    注意：短格式 `lpstat -p` 不含 shared 关键字，旧逻辑据此恒判为未共享，导致 WebUI 概览对
    已正确共享（printer-is-shared=true）的队列也误报「就绪 · 未共享」。"""
    try:
        with open("/etc/cups/printers.conf", errors="ignore") as f:
            txt = f.read()
        m = re.search(r"<Printer\s+" + re.escape(name) + r">.*?Shared\s+(Yes|No)",
                      txt, re.S)
        if m:
            return m.group(1).lower() == "yes"
    except Exception:
        pass
    try:
        rc, pout, _ = C.run(["lpstat", "-l", "-p", name])
        if rc == 0 and re.search(r"Shared:\s*yes", pout, re.I):
            return True
    except Exception:
        pass
    return False


def get_printer_meta(name):
    """返回该打印机的简要信息：URI、驱动 NickName、是否共享、是否默认。

    各子步骤独立 try，任一失败只让该字段为空，绝不让整函数抛异常（否则 WebUI 概览会把
    明明已配置 URI 的队列误判为「未配置 URI（无法打印）」）。"""
    meta = {"uri": "", "nickname": "", "accepting": False,
            "shared": False, "is_default": False}
    meta["uri"] = _printer_device_uri(name)
    ppd = f"/etc/cups/ppd/{name}.ppd"
    try:
        with open(ppd, errors="ignore") as f:
            head = f.read(2000)
        mm = re.search(r'\*NickName:\s*"([^"]*)"', head)
        if mm:
            meta["nickname"] = mm.group(1)
    except Exception:
        pass
    try:
        rc, pout, _ = C.run(["lpstat", "-p", name])
        meta["accepting"] = "accepting" in (pout or "")
    except Exception:
        pass
    # shared 必须从 printers.conf / lpstat -l -p 读取（见 _printer_shared 说明）
    meta["shared"] = _printer_shared(name)
    try:
        meta["is_default"] = (get_default_printer() == name)
    except Exception:
        pass
    return meta


def set_default_printer(name):
    rc, _, err = C.run(["lpadmin", "-d", name])
    return rc == 0, err


def apply_options(name, options):
    """options: dict {OptionName: value}。写入对应 -default。

    返回结果字符串列表（供 WebUI 展示）。
    """
    results = []
    for opt, val in options.items():
        if not opt or not val:
            continue
        rc, _, err = C.run(["lpadmin", "-p", name, "-o", f"{opt}-default={val}"])
        if rc == 0:
            results.append(f"已设置 {opt}={val}")
        else:
            results.append(f"设置 {opt} 失败: {err}")
    return results
