"""验证 0.2.43 修复：add_printer 必须在 lpadmin -m 返回 rc=0 后校验 PPD 已真实生成，
且仅“去前缀”的 scheme:/path 驱动 id 能在本机 CUPS 2.4 上生成真实 PPD；带 :N/ 代价的 id
会『rc=0 却写不出 PPD 文件』（半残队列），必须被检测并改用正确 id 重建。

本测试用 fake_run 模拟这种行为，无需真实 CUPS / 盒子即可确定性验证修复。
"""
import os
import sys
import tempfile

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, os.path.abspath(SRC))

import printbox.printers as P

NAME = "hp_laserjet_p1505n_printbox"
URI = "usb://HP/LaserJet%20P1505n?serial=KQ12ZY6"
MDL = "HP LaserJet P1505n"
COSTED = "foo2zjs:0/ppd/foo2zjs/HP-LaserJet_P1505n.ppd"   # lpinfo -m 原始输出（带 :N/ 代价）
STRIPPED = "foo2zjs:/ppd/foo2zjs/HP-LaserJet_P1505n.ppd"  # 去前缀（手工验证可用）

REAL_PPD = """*PPD-Adobe: "4.3"
*FormatVersion: "4.3"
*Manufacturer: "HP"
*ModelName: "HP LaserJet P1505n"
*Product: "(HP LaserJet P1505n)"
*OpenUI *PageSize: PickOne
*DefaultPageSize: A4
*PageSize A4/A4: "<</PageSize[595 842]>>setpagedevice"
*CloseUI: *PageSize
"""


def make_state(ppd_path):
    """构造一个可变的执行环境状态。"""
    # 记录『盘上是否存在 PPD 文件』：用于 is_raw_printer / 校验
    state = {"ppd_exists": False}

    def fake_run(cmd, **kw):
        c = list(cmd)
        if c[:2] == ["lpinfo", "-m"]:
            # 返回带代价前缀的驱动 id（模拟本机 lpinfo -m 输出）
            return (0, COSTED + " " + MDL + " Foomatic/foo2xqx (recommended)\n", "")
        if c[:2] == ["lpstat", "-r"]:
            return (0, "scheduler is running\n", "")
        if c[:2] == ["lpstat", "-p", NAME]:
            return (1, "", "")  # 队列不存在
        # lpadmin 添加：根据 -m 的候选判定
        if c[:3] == ["lpadmin", "-p", NAME] and "-m" in c:
            cand = c[c.index("-m") + 1]
            if cand == COSTED:
                # 带 :N/ 代价的 id：rc=0 但【不写 PPD 文件】（半残队列 → 模拟本机行为）
                state["ppd_exists"] = False
                return (0, "", "")
            if cand == STRIPPED:
                # 去前缀 id：rc=0 且写出真实 PPD
                with open(ppd_path, "w") as f:
                    f.write(REAL_PPD)
                state["ppd_exists"] = True
                return (0, "", "")
            return (1, "", f"unknown driver {cand}")
        if c[:3] == ["lpadmin", "-x", NAME]:
            if os.path.exists(ppd_path):
                os.remove(ppd_path)
            state["ppd_exists"] = False
            return (0, "", "")
        if c[:1] == ["lpadmin"]:
            return (0, "", "")  # -o / -P 等
        if c[:1] == ["lpoptions"]:
            return (0, "", "")
        if c[:1] == ["cupsctl"]:
            return (0, "", "")
        if c and "cupstestppd" in c[0]:
            return (0, "", "")
        return (0, "", "")

    return state, fake_run


def run_once():
    tmp = tempfile.mkdtemp()
    ppd_path = os.path.join(tmp, NAME + ".ppd")
    state, fake_run = make_state(ppd_path)

    # 打桩：用临时 PPD 路径、禁用真实系统调用
    orig_ppd_path = P._ppd_path
    P._ppd_path = lambda n: ppd_path
    P.C.run = fake_run
    P._ensure_cups = lambda: True
    P._cups_running = lambda: True
    P.printer_exists = lambda n: False
    P.list_printers = lambda: []
    P.write_airprint_service = lambda *a, **k: None

    try:
        ok = P.add_printer(URI, MDL, name=NAME)
    finally:
        P._ppd_path = orig_ppd_path

    return ok, os.path.exists(ppd_path), state["ppd_exists"], ppd_path


def test_add_printer_builds_real_ppd_not_half_broken():
    ok, exists, state_exists, ppd_path = run_once()
    assert ok is True, f"add_printer 应返回 True，实际 {ok}"
    assert exists is True, "修复后必须生成真实 PPD 文件（/etc/cups/ppd 缺失是 0.2.42 崩溃根因）"
    assert state_exists is True
    # PPD 必须非 raw（含 *ModelName）
    with open(ppd_path) as f:
        txt = f.read()
    assert "*ModelName:" in txt, "生成的是 raw/空 PPD"
    print("PASS: add_printer 优先用去前缀驱动 id 生成真实 PPD，不再落成半残队列")


def run_once_ppd_fallback():
    """两种 -m 候选（带 :N/ 代价 与 去前缀）都「rc=0 但不写 PPD（半残）」时，
    应回退 lpadmin -P <真实 PPD 文件> 建队。"""
    tmp = tempfile.mkdtemp()
    ppd_path = os.path.join(tmp, NAME + ".ppd")
    state = {"ppd_exists": False}

    def fake_run(cmd, **kw):
        c = list(cmd)
        if c[:2] == ["lpinfo", "-m"]:
            return (0, COSTED + " " + MDL + " Foomatic/foo2xqx (recommended)\n", "")
        if c[:2] == ["lpstat", "-r"]:
            return (0, "scheduler is running\n", "")
        if c[:2] == ["lpstat", "-p", NAME]:
            return (1, "", "")
        if c[:3] == ["lpadmin", "-p", NAME] and "-m" in c:
            # 两种 -m 候选都「rc=0 但不写 PPD」（半残队列）
            state["ppd_exists"] = False
            return (0, "", "")
        if c[:3] == ["lpadmin", "-p", NAME] and "-P" in c:
            # -P 兜底：写出真实 PPD
            with open(ppd_path, "w") as f:
                f.write(REAL_PPD)
            state["ppd_exists"] = True
            return (0, "", "")
        if c[:3] == ["lpadmin", "-p", NAME] and "-x" in c:
            if os.path.exists(ppd_path):
                os.remove(ppd_path)
            state["ppd_exists"] = False
            return (0, "", "")
        if c[:1] == ["lpadmin"]:
            return (0, "", "")
        if c[:1] == ["lpoptions"]:
            return (0, "", "")
        if c[:1] == ["cupsctl"]:
            return (0, "", "")
        if c and "cupstestppd" in c[0]:
            return (0, "", "")
        return (0, "", "")

    orig_ppd_path = P._ppd_path
    orig_resolve = P._resolve_ppd_file_from_driver_id
    P._ppd_path = lambda n: ppd_path
    P.C.run = fake_run
    P._ensure_cups = lambda: True
    P._cups_running = lambda: True
    P.printer_exists = lambda n: False
    P.list_printers = lambda: []
    P.write_airprint_service = lambda *a, **k: None
    P._resolve_ppd_file_from_driver_id = lambda drv: ppd_path  # 模拟解析出真实 PPD 文件
    try:
        ok = P.add_printer(URI, MDL, name=NAME)
    finally:
        P._ppd_path = orig_ppd_path
        P._resolve_ppd_file_from_driver_id = orig_resolve
    return ok, os.path.exists(ppd_path), state["ppd_exists"], ppd_path


def test_add_printer_ppd_fallback_when_m_both_half_broken():
    ok, exists, state_exists, ppd_path = run_once_ppd_fallback()
    assert ok is True, f"两种 -m 候选都半残时应靠 -P 兜底建队，实际 {ok}"
    assert exists is True, "-P 兜底必须生成真实 PPD 文件"
    assert state_exists is True
    with open(ppd_path) as f:
        txt = f.read()
    assert "*ModelName:" in txt, "生成的是 raw/空 PPD"
    print("PASS: lpadmin -m 两种候选均半残时，回退 lpadmin -P 真实 PPD 成功建队（绕开驱动库路径解析）")


def test_resolve_ppd_gz_fallback():
    """0.2.47 根因回归：_resolve_ppd_file_from_driver_id 必须能递归查到 .gz 形式的真实 PPD
    并解压到临时文件返回（lpadmin -P 不认 .gz）。实测 /usr/share/ppd/foo2zjs/...ppd 不存在，
    但 foo2zjs PPD 多以 .gz 安装，旧的「按精确小写 basename 拼路径」方式完全查不到。"""
    import gzip as _gz
    tmp = tempfile.mkdtemp()
    real_ppd = ('*PPD-Adobe: "4.3"\n'
                '*ModelName: "HP LaserJet P1505n Foomatic/foo2zqx"\n')
    gz_dir = os.path.join(tmp, "usr", "share", "ppd", "foo2zjs")
    os.makedirs(gz_dir, exist_ok=True)
    gz_path = os.path.join(gz_dir, "HP-LaserJet_P1505n.ppd.gz")  # 大小写 + .gz 均不同于驱动 id 的 basename
    with _gz.open(gz_path, "wt") as f:
        f.write(real_ppd)
    resolved = P._resolve_ppd_file_from_driver_id(COSTED, roots=[tmp])
    assert resolved is not None, "应递归查到 .gz 并返回解压后的临时 PPD 路径"
    assert not resolved.endswith(".gz"), f"返回路径不应是原 .gz（lpadmin -P 不认）: {resolved}"
    assert os.path.isfile(resolved), "返回路径应存在且可读"
    with open(resolved) as f:
        content = f.read()
    assert "*ModelName:" in content, "解压后的 PPD 内容应保留"
    print("PASS: _resolve_ppd_file_from_driver_id 能递归查找并解压 .gz PPD 兜底")


if __name__ == "__main__":
    test_add_printer_builds_real_ppd_not_half_broken()
    test_add_printer_ppd_fallback_when_m_both_half_broken()
    test_resolve_ppd_gz_fallback()
    print("ALL ADD-PRINTER PPD TESTS PASSED")
