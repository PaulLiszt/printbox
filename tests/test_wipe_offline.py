"""验证 0.2.44 修复：离线清场必须摘除「非 _printbox 后缀、但 PPD 缺失/raw 的坏队列」。

回归背景：0.2.43 的 _wipe_managed_queues_offline 只摘 _printbox 托管队列，漏掉不带 _printbox
后缀的坏队列（历史/手建，如 hp_laserjet_p1505n），其 PPD 被历史崩溃写崩/删除 → cupsd 加载即
`Raw queues are deprecated` + `Scheduler shutting down due to program error` 崩溃循环，而 main()
因 cupsd 崩着连不上、永远建不出新队列。本测试用临时 printers.conf + 临时 PPD 目录确定性验证：
  - 不带 _printbox 且 PPD 缺失的坏队列 → 必须被摘除；
  - 健康（有真实 PPD、非 raw）的非 _printbox 队列 → 必须保留，不误伤用户手动打印机。
"""
import os
import sys
import tempfile
import shutil

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, os.path.abspath(SRC))

import printbox.printers as P

GOOD_PPD = """*PPD-Adobe: "4.3"
*Manufacturer: "HP"
*ModelName: "HP LaserJet P1505n"
*Product: "(HP LaserJet P1505n)"
*OpenUI *PageSize: PickOne
*DefaultPageSize: A4
*PageSize A4/A4: "<</PageSize[595 842]>>setpagedevice"
*CloseUI: *PageSize
"""


def _write_conf(path, body):
    with open(path, "w") as f:
        f.write(body)


def test_wipe_removes_non_printbox_bad_queue(tmp_path=None):
    d = tmp_path or tempfile.mkdtemp()
    try:
        pc = os.path.join(d, "printers.conf")
        ppd_dir = os.path.join(d, "ppd")
        os.makedirs(ppd_dir, exist_ok=True)
        # 两个队列：
        #  - hp_laserjet_p1505n            —— 不带 _printbox，且 PPD 缺失（坏队列，必须摘除）
        #  - office_hp_mfp                 —— 不带 _printbox，但有真实 PPD（健康，必须保留）
        #  - hp_laserjet_p1505n_printbox    —— 托管队列（必须摘除）
        _write_conf(pc, """\
# This is the cupsd printer configuration file.
<Printer hp_laserjet_p1505n>
PrinterId 1
DeviceURI usb://HP/LaserJet%20P1505n?serial=KQ12ZY6
State Idle
</Printer>
<Printer office_hp_mfp>
PrinterId 2
DeviceURI socket://192.168.1.50:9100
State Idle
</Printer>
<DefaultPrinter hp_laserjet_p1505n_printbox>
PrinterId 3
DeviceURI usb://HP/LaserJet%20P1505n?serial=KQ12ZY6
State Idle
</DefaultPrinter>
""")
        # 仅 office_hp_mfp 有真实 PPD（健康）
        with open(os.path.join(ppd_dir, "office_hp_mfp.ppd"), "w") as f:
            f.write(GOOD_PPD)
        # hp_laserjet_p1505n 与 hp_laserjet_p1505n_printbox 的 PPD 不存在（缺失 → 坏队列）

        removed = P._wipe_managed_queues_offline(pc, ppd_dir)

        # 必须摘除：hp_laserjet_p1505n（非 _printbox + PPD 缺失）、hp_laserjet_p1505n_printbox（托管）
        assert removed == 2, f"应摘除 2 个坏队列，实际 {removed}"

        with open(pc) as f:
            txt = f.read()
        # 坏队列块都应被注释掉
        assert "hp_laserjet_p1505n>" not in txt.replace("# ", ""), \
            "非 _printbox 的坏队列未被摘除"
        assert "hp_laserjet_p1505n_printbox>" not in txt.replace("# ", ""), \
            "托管队列未被摘除"
        # 健康队列必须保留
        assert "<Printer office_hp_mfp>" in txt, "健康非 _printbox 队列被误删"
        # 健康队列 PPD 不被删
        assert os.path.exists(os.path.join(ppd_dir, "office_hp_mfp.ppd")), "健康队列 PPD 被误删"
        print("PASS: 非 _printbox 坏队列被摘除，健康队列被保留")
    finally:
        if not tmp_path:
            shutil.rmtree(d, ignore_errors=True)


def test_wipe_all_printers_offline(tmp_path=None):
    """_wipe_all_printers_offline 必须摘除全部队列块 + 删除全部 PPD。"""
    d = tmp_path or tempfile.mkdtemp()
    try:
        pc = os.path.join(d, "printers.conf")
        ppd_dir = os.path.join(d, "ppd")
        os.makedirs(ppd_dir, exist_ok=True)
        _write_conf(pc, """\
<Printer a>
DeviceURI usb://x
</Printer>
<Printer b_printbox>
DeviceURI usb://y
</Printer>
""")
        with open(os.path.join(ppd_dir, "a.ppd"), "w") as f:
            f.write(GOOD_PPD)
        with open(os.path.join(ppd_dir, "b_printbox.ppd"), "w") as f:
            f.write(GOOD_PPD)

        P._wipe_all_printers_offline(pc, ppd_dir)

        with open(pc) as f:
            txt = f.read()
        assert "<Printer a>" not in txt, "全量清场未摘除队列 a"
        assert "<Printer b_printbox>" not in txt, "全量清场未摘除队列 b_printbox"
        assert not os.path.exists(os.path.join(ppd_dir, "a.ppd")), "PPD 未删除"
        assert not os.path.exists(os.path.join(ppd_dir, "b_printbox.ppd")), "PPD 未删除"
        print("PASS: 紧急全量清场摘除全部队列块与 PPD")
    finally:
        if not tmp_path:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    test_wipe_removes_non_printbox_bad_queue()
    test_wipe_all_printers_offline()
    print("ALL WIPE-OFFLINE TESTS PASSED")
