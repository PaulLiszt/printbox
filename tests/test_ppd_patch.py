"""printers.patch_foo2xqx_ppd / patch_ppd_defaults / _current_uri 单测。

针对 0.2.36 暴露的真问题编写：
  - 0.2.36 的 patch_foo2xqx_ppd 仅在 cupsFilter 行的 MIME 类型属于 3 项白名单时才改写，
    若 PPD 用了其它 MIME（如 application/vnd.cups-raster / application/postscript / image/*），
    旧 /usr/local/bin/printbox-foo2xqx-pdf 路径永远不被替换 → 作业 0 字节、cupsd 崩溃。
    本测试用多种 MIME 的 cupsFilter 行验证 0.2.37 的“含 foomatic-rip 或旧绝对路径即改”逻辑。
  - patch_ppd_defaults 必须把默认纸张设为 A4（修复 iOS/鸿蒙 无 A4 / 默认 Letter）。
  - _current_uri 必须能解析真实 `device for X: uri`（冒号）格式，否则 URI 自愈拿到空串。

不依赖真实 lpadmin/cups：通过打桩 P.C.run 与 P.C.log、临时 PPD 文件验证。
"""
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from printbox import printers as P


# ---- 桩：C.run / C.log 全部静默 ----
def fake_run(cmd, **kw):
    # 支持 _current_uri 测试：返回真实冒号格式 lpstat -v 输出
    if cmd[:2] == ["lpstat", "-v"] and len(cmd) >= 3:
        return (0, "device for %s: usb://HP/LaserJet%%20P1505n?serial=KQ12ZY6" % cmd[2], "")
    return (0, "", "")


def fake_log(*a, **k):
    return None


# 准备假的 pdftopdf / foomatic-rip 放到 PATH，让 _resolve_filter 能找到
_BIN = tempfile.mkdtemp(prefix="printbox_test_bin_")
for _n in ("pdftopdf.exe", "foomatic-rip.exe"):
    with open(os.path.join(_BIN, _n), "w") as _f:
        _f.write("# fake\n")
os.environ["PATH"] = _BIN + os.pathsep + os.environ.get("PATH", "")


def _setup():
    P.C.run = fake_run
    P.C.log = fake_log
    P._ppd_path = lambda name: os.path.join(_TMP, name + ".ppd")


_TMP = tempfile.mkdtemp(prefix="printbox_test_ppd_")


def _write_ppd(name, content):
    path = os.path.join(_TMP, name + ".ppd")
    with open(path, "w") as f:
        f.write(content)
    return path


SAMPLE_PPD = """\
*PPD-Adobe: "4.3"
*FormatVersion: "4.3"
*FileVersion: "1.0"
*NickName: "HP LaserJet P1505n Foomatic/foo2xqx"
*Manufacturer: "HP"
*ModelName: "HP LaserJet P1505n"
*Product: "(HP LaserJet P1505n)"
*LanguageLevel: "3"
*CupsVersion: "1.1"
*PageSize A4/A4: "<</PageSize[595 842]>>setpagedevice"
*PageSize Letter/Letter: "<</PageSize[612 792]>>setpagedevice"
*OpenUI *PageSize: PickOne
*DefaultPageSize: Letter
*PageRegion A4/A4: "<</PageSize[595 842]>>setpagedevice"
*PageRegion Letter/Letter: "<</PageSize[612 792]>>setpagedevice"
*OpenUI *PageRegion: PickOne
*DefaultPageRegion: Letter
*% Foomatic/foo2xqx PPD
*cupsFilter: "application/vnd.cups-raster 0 /usr/local/bin/printbox-foo2xqx-pdf"
*cupsFilter2: "application/postscript 0 foomatic-rip"
*FoomaticRIPCommandLine: "foo2xqx ..."
"""


# 只有 Letter、完全没有 A4 选项的 PPD（模拟用户盒上「driver 装错 / PPD 被写残」导致的真实情形）
NO_A4_PPD = """\
*PPD-Adobe: "4.3"
*NickName: "HP LaserJet P1505n Foomatic/foo2xqx"
*ModelName: "HP LaserJet P1505n"
*OpenUI *PageSize: PickOne
*OrderDependency: 10 AnySetup *PageSize
*DefaultPageSize: Letter
*PageSize Letter/Letter: "<</PageSize[612 792]>>setpagedevice"
*CloseUI: *PageSize
*cupsFilter: "application/vnd.cups-raster 0 /usr/local/bin/printbox-foo2xqx-pdf"
"""

# raw 队列 PPD（无真实驱动）：patch_ppd_defaults 应直接跳过、不改动
RAW_PPD = """\
*PPD-Adobe: "4.3"
*NickName: "raw"
*ModelName: "raw"
"""


def test_patch_ppd_defaults_injects_a4_when_missing():
    """PPD 选项列表只有 Letter、无 A4 时，必须强制注入 A4 选项并把默认设为 A4（用户真实场景）。
    历史上 patch_ppd_defaults 在此情况静默放弃 → 默认永远停在 Letter、iOS/鸿蒙看不到 A4。"""
    _setup()
    path = _write_ppd("hp_laserjet_p1505n_printbox", NO_A4_PPD)
    P.patch_ppd_defaults("hp_laserjet_p1505n_printbox")
    with open(path) as f:
        text = f.read()
    assert "*PageSize A4/A4:" in text, "A4 选项未被注入到 *PageSize 选项列表:\n" + text
    assert "*DefaultPageSize: A4" in text, "默认 PageSize 未设为 A4:\n" + text
    assert "*DefaultPageRegion: A4" in text, "默认 PageRegion 未设为 A4:\n" + text
    # 注入后 A4 不应破坏原有 Letter 选项（选择列表应同时含 A4 与 Letter）
    assert "Letter" in text, "注入 A4 时误删了 Letter 选项:\n" + text


def test_patch_ppd_defaults_skips_raw():
    """raw 队列（无真实 PPD）应直接跳过，不写坏任何东西。"""
    _setup()
    path = _write_ppd("raw_printer", RAW_PPD)
    P.patch_ppd_defaults("raw_printer")
    with open(path) as f:
        text = f.read()
    assert "*DefaultPageSize: A4" not in text, "raw PPD 被误改:\n" + text


def test_strip_drv_cost():
    """lpinfo -m 的驱动 id 可能带代价前缀 `:N/`，必须去掉（否则 lpadmin -m 失败 → 回退 raw）。"""
    assert P._strip_drv_cost(
        "foo2zjs:0/ppd/foo2zjs/HP-LaserJet_P1505n.ppd") == \
        "foo2zjs:/ppd/foo2zjs/HP-LaserJet_P1505n.ppd"
    assert P._strip_drv_cost(
        "hpcups:3/ppd/hpcups/hp-laserjet_p1505n.ppd") == \
        "hpcups:/ppd/hpcups/hp-laserjet_p1505n.ppd"
    # 无前缀时原样返回
    assert P._strip_drv_cost(
        "drv:///hpcups.drv/hp-laserjet_p1505n.ppd") == \
        "drv:///hpcups.drv/hp-laserjet_p1505n.ppd"


def test_patch_foo2xqx_redirects_all_mime_types():
    """多种 MIME 的 cupsFilter 行都应被重定向到新 wrapper，不再引用旧路径/foomatic-rip。"""
    _setup()
    # 让 wrapper 落到临时目录（避免触碰 /usr/lib/cups/filter）
    saved_wrapper = P.FOO2XQX_WRAPPER
    P.FOO2XQX_WRAPPER = os.path.join(_TMP, "filter", "printbox-foo2xqx-pdf")
    try:
        path = _write_ppd("hp_laserjet_p1505n_printbox", SAMPLE_PPD)
        P.patch_foo2xqx_ppd("hp_laserjet_p1505n_printbox")
        with open(path) as f:
            text = f.read()
        # 旧路径必须彻底消失
        assert "/usr/local/bin/printbox-foo2xqx-pdf" not in text, \
            "PPD 仍引用已删除的旧 wrapper 路径:\n" + text
        # foomatic-rip 不应再出现在 cupsFilter 行（已被新 wrapper 替换）
        assert "foomatic-rip" not in text, "PPD cupsFilter 仍引用 foomatic-rip:\n" + text
        # 必须指向新 wrapper
        assert P.FOO2XQX_WRAPPER in text, "PPD 未指向新 wrapper:\n" + text
        # 两个过滤器行都被改（raster + postscript）
        assert text.count(P.FOO2XQX_WRAPPER) >= 2, "不是所有 cupsFilter 行都被重定向:\n" + text
    finally:
        P.FOO2XQX_WRAPPER = saved_wrapper


def test_patch_foo2xqx_idempotent_on_already_fixed():
    """已修好的 PPD 再打补丁不应引入变化（幂等）。"""
    _setup()
    saved_wrapper = P.FOO2XQX_WRAPPER
    P.FOO2XQX_WRAPPER = os.path.join(_TMP, "filter", "printbox-foo2xqx-pdf")
    try:
        # 先打一次（此时 PPD 还是旧路径）
        path = _write_ppd("hp_laserjet_p1505n_printbox", SAMPLE_PPD)
        P.patch_foo2xqx_ppd("hp_laserjet_p1505n_printbox")
        with open(path) as f:
            after_first = f.read()
        # 再打一次
        P.patch_foo2xqx_ppd("hp_laserjet_p1505n_printbox")
        with open(path) as f:
            after_second = f.read()
        assert after_first == after_second, "重复打补丁产生了差异（非幂等）"
    finally:
        P.FOO2XQX_WRAPPER = saved_wrapper


def test_patch_ppd_defaults_sets_a4():
    """默认纸张必须被设为 A4，且不能破坏 PPD 结构。"""
    _setup()
    path = _write_ppd("hp_laserjet_p1505n_printbox", SAMPLE_PPD)
    P.patch_ppd_defaults("hp_laserjet_p1505n_printbox")
    with open(path) as f:
        text = f.read()
    assert "*DefaultPageSize: A4" in text, "默认 PageSize 未设为 A4:\n" + text
    assert "*DefaultPageRegion: A4" in text, "默认 PageRegion 未设为 A4:\n" + text
    assert "Letter" not in text.split("*DefaultPageSize: A4")[1].split("\n")[0], \
        "DefaultPageSize 行仍残留 Letter"


def test_current_uri_parses_colon_format():
    """_current_uri 必须解析真实 `device for X: uri`（冒号）格式。"""
    _setup()
    uri = P._current_uri("hp_laserjet_p1505n_printbox")
    assert uri == "usb://HP/LaserJet%20P1505n?serial=KQ12ZY6", f"解析失败，得到: {uri!r}"


def test_current_uri_handles_is_format_too():
    """兼容早期 `device for X is uri` 写法。"""
    def fake_run_is(cmd, **kw):
        if cmd[:2] == ["lpstat", "-v"] and len(cmd) >= 3:
            return (0, "device for %s is usb://HP/P1505n" % cmd[2], "")
        return (0, "", "")
    P.C.run = fake_run_is
    P.C.log = fake_log
    P._ppd_path = lambda name: os.path.join(_TMP, name + ".ppd")
    uri = P._current_uri("hp_laserjet_p1505n_printbox")
    assert uri == "usb://HP/P1505n", f"is 格式解析失败，得到: {uri!r}"


def test_sanitize_rewrites_dead_wrapper_path_offline():
    """_sanitize_ppd_filter_paths 必须把残留的旧 wrapper 路径【离线】改写为新路径，
    且不依赖 cupsd（纯文件操作），否则 repair 重启 cupsd 时会因坏 PPD 崩溃、队列丢失。"""
    _setup()
    P._install_foo2xqx_wrapper = lambda: True  # 隔离：不碰真实 /usr/lib/cups/filter
    tmpdir = tempfile.mkdtemp(prefix="printbox_sanitize_")
    saved_old = P.FOO2XQX_WRAPPER_OLD
    saved_new = P.FOO2XQX_WRAPPER
    P.FOO2XQX_WRAPPER_OLD = "/usr/local/bin/printbox-foo2xqx-pdf"
    # 任意新路径，仅用于验证“被引用”即可
    P.FOO2XQX_WRAPPER = os.path.join(tmpdir, "printbox-foo2xqx-pdf")
    try:
        bad = os.path.join(tmpdir, "bad.ppd")
        good = os.path.join(tmpdir, "good.ppd")
        with open(bad, "w") as f:
            f.write('*cupsFilter: "application/pdf 0 /usr/local/bin/printbox-foo2xqx-pdf"\n')
        with open(good, "w") as f:
            f.write('*cupsFilter: "application/pdf 0 /usr/lib/cups/filter/gs"\n')
        P._sanitize_ppd_filter_paths(ppd_dir=tmpdir)
        with open(bad) as f:
            btext = f.read()
        with open(good) as f:
            gtext = f.read()
        assert "/usr/local/bin/printbox-foo2xqx-pdf" not in btext, \
            "残留旧 wrapper 路径未被清除:\n" + btext
        assert P.FOO2XQX_WRAPPER in btext, "旧 PPD 未指向新 wrapper:\n" + btext
        assert "/usr/local/bin/printbox-foo2xqx-pdf" not in gtext, "不含旧路径的 PPD 被误改"
        assert P.FOO2XQX_WRAPPER not in gtext, "不含旧路径的 PPD 被误改"
    finally:
        P.FOO2XQX_WRAPPER_OLD = saved_old
        P.FOO2XQX_WRAPPER = saved_new
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_ppd_is_raw_or_missing_detects_missing():
    """_ppd_is_raw_or_missing 对不存在的文件应返回 'missing'。"""
    _setup()
    reason = P._ppd_is_raw_or_missing(os.path.join(_TMP, "no_such_printer.ppd"))
    assert reason == "missing", f"缺失 PPD 未被识别，得到: {reason!r}"


def test_ppd_is_raw_or_missing_detects_raw():
    """_ppd_is_raw_or_missing 对极简 raw PPD（无 *ModelName）应返回原因字符串。"""
    _setup()
    raw = os.path.join(_TMP, "raw.ppd")
    # 真实 CUPS raw PPD 没有 *ModelName / *OpenUI *PageSize 块
    with open(raw, "w") as f:
        f.write("*PPD-Adobe: \"4.3\"\n*PCFileName: \"RAW.PPD\"\n")
    reason = P._ppd_is_raw_or_missing(raw)
    assert reason and reason.startswith("raw"), f"raw PPD 未被识别，得到: {reason!r}"


def test_ppd_is_raw_or_missing_ok_for_valid():
    """_ppd_is_raw_or_missing 对含 *ModelName + *OpenUI *PageSize 的真驱动 PPD 应返回 None。"""
    _setup()
    valid = os.path.join(_TMP, "valid.ppd")
    with open(valid, "w") as f:
        f.write('*PPD-Adobe: "4.3"\n'
                '*ModelName: "HP LaserJet P1505n"\n'
                '*NickName: "HP LaserJet P1505n Foomatic/foo2xqx"\n'
                '*OpenUI *PageSize: PickOne\n'
                '*DefaultPageSize: A4\n'
                '*PageSize A4/A4: "<</PageSize[595 842]>>setpagedevice"\n'
                '*CloseUI: *PageSize\n')
    assert P._ppd_is_raw_or_missing(valid) is None, "合法真驱动 PPD 被误判为坏"


def test_remove_broken_queues_offline_strips_missing_ppd():
    """_remove_broken_queues_offline 必须【离线】摘除 PPD 缺失的坏队列、保留健康队列，
    使 cupsd 能干净启动（这是打断“坏 PPD→cupsd 崩溃循环”的决定性步骤）。"""
    _setup()
    tmpd = tempfile.mkdtemp(prefix="printbox_offline_q_")
    try:
        ppd_dir = os.path.join(tmpd, "ppd")
        os.makedirs(ppd_dir)
        # 健康队列的 PPD（含 ModelName + PageSize UI）
        good = os.path.join(ppd_dir, "hp_laserjet_p1505n_printbox.ppd")
        with open(good, "w") as f:
            f.write('*PPD-Adobe: "4.3"\n*ModelName: "HP LaserJet P1505n"\n'
                    '*OpenUI *PageSize: PickOne\n*CloseUI: *PageSize\n')
        # 坏队列的 PPD 故意不创建（缺失）
        pc = os.path.join(tmpd, "printers.conf")
        with open(pc, "w") as f:
            f.write(
                "<DefaultPrinter hp_laserjet_p1505n_printbox>\n"
                "DeviceURI usb://HP/LaserJet%20P1505n?serial=KQ12ZY6\n"
                "State Idle\n"
                "</Printer>\n"
                "<Printer broken_phantom>\n"
                "DeviceURI usb://HP/old\n"
                "State Idle\n"
                "</Printer>\n")
        removed = P._remove_broken_queues_offline(printers_conf=pc, ppd_dir=ppd_dir)
        assert removed == 1, f"应摘除 1 个坏队列，实际: {removed}"
        with open(pc) as f:
            txt = f.read()
        assert "broken_phantom" in txt and "已摘除" in txt, \
            "坏队列未被摘除:\n" + txt
        assert "<Printer broken_phantom>" not in txt, "坏队列活跃块未被摘除:\n" + txt
        assert "hp_laserjet_p1505n_printbox" in txt, "健康队列被误删:\n" + txt
        assert "DeviceURI usb://HP/LaserJet" in txt, "健康队列 DeviceURI 被破坏:\n" + txt
        assert "</Printer>" in txt, "健康队列结束标签被破坏:\n" + txt
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def test_wipe_managed_queues_offline_strips_printbox_and_removes_ppd():
    """_wipe_managed_queues_offline：离线清空全部 _printbox 托管队列（含删 PPD 文件），
    非 _printbox 队列（用户手动添加的）保留；且能容忍 printers.conf 被崩溃截断（缺 </Printer>）。"""
    import shutil as _sh
    tmpd = tempfile.mkdtemp(prefix="printbox_test_wipe_")
    try:
        ppd_dir = os.path.join(tmpd, "ppd")
        os.makedirs(ppd_dir, exist_ok=True)
        # 一个本系统托管的 _printbox 队列（带 PPD）+ 一个用户自加的网络队列（非 _printbox，须保留）
        pc = os.path.join(tmpd, "printers.conf")
        with open(pc, "w") as f:
            f.write(
                "<DefaultPrinter hp_laserjet_p1505n_printbox>\n"
                "DeviceURI usb://HP/LaserJet%20P1505n?serial=KQ12ZY6\n"
                "State Idle\n"
                "</Printer>\n"
                "<Printer user_added_network>\n"
                "DeviceURI ipp://192.168.1.50/ipp/print\n"
                "State Idle\n"
                "</Printer>\n")
        good_ppd = os.path.join(ppd_dir, "hp_laserjet_p1505n_printbox.ppd")
        with open(good_ppd, "w") as f:
            f.write("*PPD-Adobe: \"4.3\"\n*ModelName: \"HP LaserJet P1505n\"\n")
        # 用户自加的网络队列应是一台「健康」打印机（有真实 PPD），故保留；
        # 缺失/raw PPD 的非 _printbox 队列会被新逻辑一并摘除（见 test_wipe_offline.py）。
        user_ppd = os.path.join(ppd_dir, "user_added_network.ppd")
        with open(user_ppd, "w") as f:
            f.write("*PPD-Adobe: \"4.3\"\n*ModelName: \"Network Printer\"\n"
                    "*OpenUI *PageSize: PickOne\n*DefaultPageSize: A4\n"
                    "*PageSize A4/A4: \"<</PageSize[595 842]>>setpagedevice\"\n"
                    "*CloseUI: *PageSize\n")
        removed = P._wipe_managed_queues_offline(printers_conf=pc, ppd_dir=ppd_dir)
        assert removed == 1, f"应摘除 1 个托管队列（健康用户队列须保留），实际: {removed}"
        with open(pc) as f:
            txt = f.read()
        assert "hp_laserjet_p1505n_printbox" in txt and "离线摘除队列" in txt, \
            "托管队列未被摘除:\n" + txt
        assert "<DefaultPrinter hp_laserjet_p1505n_printbox>" not in txt, \
            "托管队列活跃块未被摘除:\n" + txt
        assert "<Printer user_added_network>" in txt, "用户自加队列被误删:\n" + txt
        assert not os.path.exists(good_ppd), "托管队列的 PPD 文件未被删除"

        # 截断场景：printers.conf 被崩溃写崩、缺少 </Printer> 闭合标签，仍能清掉 _printbox 块
        pc2 = os.path.join(tmpd, "printers2.conf")
        with open(pc2, "w") as f:
            f.write("<Printer hp_laserjet_p1505n_printbox>\nDeviceURI usb://HP/x\n"
                    "State Idle\n")  # 故意无 </Printer>
        removed2 = P._wipe_managed_queues_offline(printers_conf=pc2, ppd_dir=ppd_dir)
        assert removed2 == 1, f"截断配置应仍摘除 1 个托管队列，实际: {removed2}"
        with open(pc2) as f:
            assert "离线摘除队列" in f.read()
    finally:
        _sh.rmtree(tmpd, ignore_errors=True)


if __name__ == "__main__":
    test_patch_foo2xqx_redirects_all_mime_types()
    test_patch_foo2xqx_idempotent_on_already_fixed()
    test_patch_ppd_defaults_sets_a4()
    test_patch_ppd_defaults_injects_a4_when_missing()
    test_patch_ppd_defaults_skips_raw()
    test_strip_drv_cost()
    test_current_uri_parses_colon_format()
    test_current_uri_handles_is_format_too()
    test_sanitize_rewrites_dead_wrapper_path_offline()
    test_ppd_is_raw_or_missing_detects_missing()
    test_ppd_is_raw_or_missing_detects_raw()
    test_ppd_is_raw_or_missing_ok_for_valid()
    test_remove_broken_queues_offline_strips_missing_ppd()
    test_wipe_managed_queues_offline_strips_printbox_and_removes_ppd()
    print("ALL PPD PATCH TESTS PASSED")
