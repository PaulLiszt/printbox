"""printers.get_devices 解析单测。

用用户真实环境 `lpinfo -l -v` 的输出片段验证：
  - URI 前缀 `uri = ` 必须被剥掉；
  - 型号(mdl)从 make-and-model / device-id 正确提取；
  - beh / cups-brf / 裸 serial: 等伪后端被过滤。
不依赖真实 lpinfo（通过打桩 C.run 注入样本）。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from printbox import printers as P
from printbox import printopts as PO


# 用户真实设备（HP LaserJet P1505n）的 verbose 输出片段，外加若干伪后端
SAMPLE = """
Device: uri = beh
        class = network
        info = Backend Error Logger
        make-and-model = Backend Error Logger
        device-id =
        location =
Device: uri = cups-brf:/
        class = file
        info = CUPS-BRF
        make-and-model = CUPS-BRF
        device-id =
        location =
Device: uri = http
        class = network
        info = Internet Printing Protocol (http)
        make-and-model = Internet Printing Protocol (http)
        device-id =
        location =
Device: uri = serial:/dev/ttyS0?baud=115200
        class = serial
        info = Serial Port #1
        make-and-model = Serial Port #1
        device-id =
        location =
Device: uri = hp:/usb/HP_LaserJet_P1505n?serial=KQ12ZY6
        class = direct
        info = HP LaserJet P1505n
        make-and-model = HP LaserJet P1505n
        device-id = MFG:HP;MDL:LaserJet P1505n;CMD:PJL,PCL,POSTSCRIPT;
        location =
Device: uri = usb://HP/LaserJet%20P1505n?serial=KQ12ZY6
        class = direct
        info = HP LaserJet P1505n
        make-and-model = HP LaserJet P1505n
        device-id = MFG:HP;MDL:LaserJet P1505n;CMD:PJL,PCL,POSTSCRIPT;
        location =
Device: uri = "hpfax"
        class = direct
        info = HP Fax
        make-and-model = HP Fax
        device-id =
        location =
"""


def fake_run(cmd, **kw):
    if cmd == ["lpinfo", "-l", "-v"]:
        return (0, SAMPLE, "")
    if cmd == ["lpinfo", "-v"]:
        return (0, "", "")
    if cmd == ["lpinfo", "-m"]:
        # 命中 hpcups 驱动
        return (0, "drv:///hp/hpcups.drv/hp-laserjet_p1505n.ppd hpcups HP LaserJet P1505n\n", "")
    return (0, "", "")


def test_parse_verbose_strips_prefix_and_extracts_mdl():
    P.C.run = fake_run
    devs = P.get_devices()
    real = [(u, m) for u, m in devs if P._is_real_printer(u)]
    # 两个真实 URI 都在，且无 uri = 前缀
    assert ("hp:/usb/HP_LaserJet_P1505n?serial=KQ12ZY6", "HP LaserJet P1505n") in real, real
    assert ("usb://HP/LaserJet%20P1505n?serial=KQ12ZY6", "HP LaserJet P1505n") in real, real
    for u, m in real:
        assert not u.startswith("uri ="), f"URI 仍带前缀: {u}"
        assert m == "HP LaserJet P1505n", f"型号提取错误: {m}"


def test_fake_backends_filtered():
    P.C.run = fake_run
    devs = P.get_devices()
    # 与 main() 一致：先按 _is_real_printer 过滤，再判定
    real = [(u, m) for u, m in devs if P._is_real_printer(u)]
    uris = [u for u, _ in real]
    for bad in ("beh", "cups-brf:/", "http", "serial:/dev/ttyS0?baud=115200", "hpfax"):
        assert bad not in uris, f"伪后端未过滤: {bad}"


def test_text_injection():
    # 直接注入文本，验证 _parse_verbose 独立可用
    devs = P._parse_verbose(SAMPLE)
    real = [(u, m) for u, m in devs if P._is_real_printer(u)]
    assert ("hp:/usb/HP_LaserJet_P1505n?serial=KQ12ZY6", "HP LaserJet P1505n") in real
    assert ("usb://HP/LaserJet%20P1505n?serial=KQ12ZY6", "HP LaserJet P1505n") in real


# 一份贴近真实的 lpinfo -m 输出：hpcups 与 foo2zjs 都能驱动 P1505n，
# 且 foo2zjs 被厂商标注为 (recommended)
LPINFO_M = """\
drv:///hp/hpcups.drv/hp-laserjet_p1505n.ppd HP LaserJet P1505n hpcups
foo2zjs:0/ppd/foo2zjs/HP-LaserJet_P1505n.ppd HP LaserJet P1505n Foomatic/foo2zjs (recommended)
drv:///foomatic/foomatic-rip.ppd Generic PostScript
raw raw
"""


def fake_run_lpinfo_m(cmd, **kw):
    if cmd == ["lpinfo", "-l", "-v"]:
        return (0, SAMPLE, "")
    if cmd == ["lpinfo", "-v"]:
        return (0, "", "")
    if cmd == ["lpinfo", "-m"]:
        return (0, LPINFO_M, "")
    return (0, "", "")


def test_pick_driver_returns_real_driver_not_raw():
    P.C.run = fake_run_lpinfo_m
    P._ensure_cups = lambda: True      # 单测只验证选驱动逻辑，cups 就绪状态打桩
    P.time.sleep = lambda *a, **k: None
    drv = P.pick_driver("HP LaserJet P1505n", "hp:/usb/HP_LaserJet_P1505n?serial=KQ12ZY6")
    assert drv is not None, "应为 P1505n 选到真实驱动，而不是 None/raw"
    assert "raw" not in drv.lower(), f"不应选 raw 驱动: {drv}"
    # 必须是能驱动 P1505n 的候选
    assert "p1505n" in drv.lower() or "foo2zjs" in drv.lower(), drv


def test_pick_driver_recommended_preferred_over_other_keywords():
    # 场景：候选里既有 hpcups（命中关键字）又有 (recommended) 的 foo2zjs，
    # 期望优先返回厂商标注的 recommended 项
    P.C.run = fake_run_lpinfo_m
    P._ensure_cups = lambda: True
    P.time.sleep = lambda *a, **k: None
    drv = P.pick_driver("HP LaserJet P1505n", "hp:/usb/HP_LaserJet_P1505n?serial=KQ12ZY6")
    assert drv is not None
    assert "foo2zjs" in drv.lower(), f"应优先 recommended 的 foo2zjs，实际: {drv}"


def test_pick_driver_retries_empty_lpinfo_m():
    """0.2.47 根因回归：lpinfo -m 在 cupsd 刚就绪、驱动后端未枚举完时偶发返回空。
    此时 pick_driver 必须重试（最多 5 次、间隔 1s），而不是空列表直接返回 None → 误判 raw。"""
    calls = {"m": 0}

    def fake_run(cmd, **kw):
        if cmd == ["lpinfo", "-l", "-v"]:
            return (0, SAMPLE, "")
        if cmd == ["lpinfo", "-v"]:
            return (0, "", "")
        if cmd == ["lpinfo", "-m"]:
            calls["m"] += 1
            if calls["m"] <= 2:
                return (0, "", "")          # 前两次空：驱动后端尚未就绪
            return (0, LPINFO_M, "")          # 第三次起恢复
        return (0, "", "")

    P.C.run = fake_run
    P._ensure_cups = lambda: True
    P.time.sleep = lambda *a, **k: None      # 加速单测，跳过重试间隔
    drv = P.pick_driver("HP LaserJet P1505n", "hp:/usb/HP_LaserJet_P1505n?serial=KQ12ZY6")
    assert drv is not None, "lpinfo -m 偶发空时 pick_driver 应重试并选到真实驱动，而非返回 None"
    assert "raw" not in drv.lower(), f"不应选 raw: {drv}"
    assert calls["m"] >= 3, f"应至少重试 3 次 lpinfo -m（前两次空），实际 {calls['m']} 次"
    print("PASS: lpinfo -m 偶发空时 pick_driver 重试后仍能选到真实驱动（不再误判 raw）")


# 一份贴近真实的 `lpoptions -l -p <name>` 输出（不同驱动暴露的选项不同）
LPOPTIONS_SAMPLE = """\
PageSize/Media Size: *A4 Letter Legal Executive
Resolution/Resolution: 300x300dpi *600x600dpi 1200x1200dpi
Duplex/2-Sided Printing: *None DuplexNoTumble DuplexTumble
ColorModel/Color Model: *Gray Color
InputSlot/Media Source: *Auto Tray1 Manual
"""


def test_parse_lpoptions_basic():
    opts = PO.parse_lpoptions(LPOPTIONS_SAMPLE)
    names = [o["name"] for o in opts]
    assert "PageSize" in names
    assert "Duplex" in names
    assert "ColorModel" in names
    # 描述被正确剥离
    assert opts[0]["desc"] == "Media Size"
    # 每个选项应有 choice 列表
    for o in opts:
        assert len(o["choices"]) >= 2


def test_parse_lpoptions_current_marked():
    opts = PO.parse_lpoptions(LPOPTIONS_SAMPLE)
    by = {o["name"]: o for o in opts}
    # 当前生效值应以 * 标记
    assert by["PageSize"]["choices"][0]["current"] is True
    assert by["PageSize"]["choices"][0]["value"] == "A4"
    assert by["Resolution"]["choices"][1]["current"] is True
    assert by["Resolution"]["choices"][1]["value"] == "600x600dpi"
    # 非当前项不应被标记
    assert by["ColorModel"]["choices"][0]["current"] is True  # Gray=*
    assert by["ColorModel"]["choices"][1]["current"] is False  # Color


if __name__ == "__main__":
    test_parse_verbose_strips_prefix_and_extracts_mdl()
    test_fake_backends_filtered()
    test_text_injection()
    test_pick_driver_returns_real_driver_not_raw()
    test_pick_driver_recommended_preferred_over_other_keywords()
    test_pick_driver_retries_empty_lpinfo_m()
    test_parse_lpoptions_basic()
    test_parse_lpoptions_current_marked()
    print("ALL PRINTERS PARSE TESTS PASSED")

