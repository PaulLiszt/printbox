"""0.2.45 运行级 mock 单测：打印洪流防护 + 核弹级重置兜底。

不依赖真实盒子：通过打桩 config.C.run / os.* / init._run 等系统调用，确定性验证
「客户端重复提交同一文档时服务端能拦住（取消重复在途作业）」，以及「崩溃循环里
printers.conf 被写崩时，核弹级整体移走文件（而非正则编辑）能正确执行」。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from printbox import printers as P
from printbox import init as I

# 贴近真实的 `lpstat -l -o` 输出：
#  - 同一用户 root、同一文档名 "Test Page" 提交了 3 次（鸿蒙/Win10 重发典型现场）；
#  - alice 提交了不同文档 "Report.pdf"（不应被误杀）；
#  - root 还提交了一个文档名为空的作业（无法判定重复，必须保留，不能误杀）。
LPSTAT_L_O = """\
hp_laserjet_p1505n_printbox-101 root 1024 Mon 10 12:00
\tjob-name: Test Page
\tjob-state: pending
hp_laserjet_p1505n_printbox-102 root 1024 Mon 10 12:00
\tjob-name: Test Page
\tjob-state: pending
hp_laserjet_p1505n_printbox-103 root 1024 Mon 10 12:00
\tjob-name: Test Page
\tjob-state: processing
hp_laserjet_p1505n_printbox-104 alice 2048 Mon 10 12:01
\tjob-name: Report.pdf
\tjob-state: pending
hp_laserjet_p1505n_printbox-105 root 0 Mon 10 12:02
\tjob-name:
\tjob-state: pending
"""


def make_guard_env(lpstat_out=LPSTAT_L_O, cancel_rc=0):
    cancels = []
    logged = []

    def fake_run(cmd, **kw):
        if cmd == ["lpstat", "-l", "-o"]:
            return (0, lpstat_out, "")
        if cmd[0] == "cancel":
            cancels.append(cmd[1])
            return (cancel_rc, "", "")
        return (0, "", "")

    def fake_log(*a, **kw):
        logged.append(a)

    saved_run = P.C.run
    saved_log = P.C.log
    P.C.run = fake_run
    P.C.log = fake_log
    return cancels, logged, saved_run, saved_log


def restore(saved_run, saved_log):
    P.C.run = saved_run
    P.C.log = saved_log


def test_guard_cancels_duplicate_same_doc_user():
    cancels, logged, sr, sl = make_guard_env()
    try:
        n = P.guard_runaway_jobs(max_copies=1)
    finally:
        restore(sr, sl)
    # 同 (printer, user, "Test Page") 有 3 份 -> 保留最小 jobid(101)，取消 102、103
    assert n == 2, f"应取消 2 个重复作业，实际 {n}"
    assert "hp_laserjet_p1505n_printbox-101" not in cancels, "不应取消保留的那一份"
    assert "hp_laserjet_p1505n_printbox-102" in cancels
    assert "hp_laserjet_p1505n_printbox-103" in cancels
    print("PASS: 同文档重复提交被正确取消（保留1、取消2）")


def test_guard_keeps_different_user_and_empty_name():
    cancels, logged, sr, sl = make_guard_env()
    try:
        P.guard_runaway_jobs(max_copies=1)
    finally:
        restore(sr, sl)
    # alice 的 Report.pdf 是不同用户 -> 不取消
    assert "hp_laserjet_p1505n_printbox-104" not in cancels, "不同用户作业被误杀"
    # 文档名为空的 105 -> 无法判定重复，必须保留
    assert "hp_laserjet_p1505n_printbox-105" not in cancels, "空文档名作业被误杀"
    print("PASS: 不同用户 / 空文档名作业被保留（无误杀）")


def test_guard_safe_skip_on_lpstat_failure():
    cancels, logged, sr, sl = make_guard_env(lpstat_out="")
    # lpstat 返回 rc!=0 或空 -> 安全跳过，取消 0
    P.C.run = lambda cmd, **kw: (1, "", "boom") if cmd == ["lpstat", "-l", "-o"] else (0, "", "")
    try:
        n = P.guard_runaway_jobs()
    finally:
        restore(sr, sl)
    assert n == 0, f"lpstat 失败时应为 0，实际 {n}"
    assert cancels == [], "lpstat 失败时不应有任何 cancel 调用"
    print("PASS: lpstat 失败时安全跳过（取消 0）")


def test_nuclear_reset_sequence():
    """核弹级重置：必须 mask→stop→等退出→整体移走 printers.conf→删 PPD→unmask→enable→start。"""
    calls = []
    renamed = []
    removed = []
    waited = {"start": 0, "rebuild": 0}

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return __import__("subprocess").CompletedProcess(cmd, 0, "", "")

    def fake_ensure_stopped(timeout=20):
        calls.append(["_ensure_cups_stopped"])

    def fake_wait_ready(timeout=60):
        waited["start"] += 1
        return True

    # os 桩：printers.conf 存在、ppd 目录有 2 个文件、rename/remove 记录
    fake_listdir = {"/etc/cups/ppd": ["a.ppd", "b.ppd", "notppd.txt"]}

    import subprocess as _sp
    saved_run = I._run
    saved_es = I._ensure_cups_stopped
    saved_wr = I._wait_cups_ready
    I._run = fake_run
    I._ensure_cups_stopped = fake_ensure_stopped
    I._wait_cups_ready = fake_wait_ready

    orig_exists = os.path.exists
    orig_isdir = os.path.isdir
    orig_rename = os.rename
    orig_remove = os.remove
    orig_listdir = os.listdir

    def fake_exists(p):
        if p in ("/etc/cups/printers.conf", "/etc/cups/ppd"):
            return True
        return orig_exists(p)

    def fake_isdir(p):
        if p == "/etc/cups/ppd":
            return True
        return orig_isdir(p)

    def fake_rename(src, dst):
        renamed.append((src, dst))

    def fake_remove(p):
        removed.append(p)

    def fake_listdir2(d):
        # 注意：不能用 fake_listdir.get(d, orig_listdir(d)) —— Python 会先求值默认参数
        # orig_listdir(d)，在 Windows 上真实 os.listdir 不存在的路径会抛 FileNotFoundError，
        # 被被测函数的 except 吞掉，导致 PPD 循环被跳过。改用惰性回退。
        if d in fake_listdir:
            return fake_listdir[d]
        return orig_listdir(d)

    os.path.exists = fake_exists
    os.path.isdir = fake_isdir
    os.rename = fake_rename
    os.remove = fake_remove
    os.listdir = fake_listdir2
    try:
        I._nuclear_reset_printers()
    finally:
        I._run = saved_run
        I._ensure_cups_stopped = saved_es
        I._wait_cups_ready = saved_wr
        os.path.exists = orig_exists
        os.path.isdir = orig_isdir
        os.rename = orig_rename
        os.remove = orig_remove
        os.listdir = orig_listdir

    seq = [" ".join(c[:3]) for c in calls]
    assert "systemctl mask cups" in seq, "应先 mask cups"
    assert "_ensure_cups_stopped" in seq, "应等 cupsd 彻底退出"
    assert "systemctl unmask cups" in seq, "应最后 unmask cups"
    assert "systemctl start cups" in seq, "应 start cups"
    # 关键：整体移走 printers.conf（rename），不是正则编辑
    assert any(r[0] == "/etc/cups/printers.conf" and r[1].startswith("/etc/cups/printers.conf.bak") for r in renamed), \
        f"应整体 rename printers.conf，实际: {renamed}"
    # 关键：仅 .ppd 被删，非 ppd 文件(notppd.txt)不动
    # 注意：用 os.path.join 生成期望路径，规避 Windows 下分隔符为反斜杠的差异
    ppd_a = os.path.join("/etc/cups/ppd", "a.ppd")
    ppd_b = os.path.join("/etc/cups/ppd", "b.ppd")
    assert ppd_a in removed and ppd_b in removed, f"PPD 应被删，实际: {removed}"
    assert not any("notppd.txt" in r for r in removed), "非 PPD 文件不应被删"
    print("PASS: 核弹级重置序列正确（mask→stop→整体移走.conf→删PPD→unmask→start）")


def test_nuclear_resets_when_no_conf_present():
    """即使 printers.conf 不存在也不应报错（幂等）。"""
    saved_run = I._run
    saved_es = I._ensure_cups_stopped
    saved_wr = I._wait_cups_ready
    I._run = lambda cmd, **kw: __import__("subprocess").CompletedProcess(cmd, 0, "", "")
    I._ensure_cups_stopped = lambda timeout=20: None
    I._wait_cups_ready = lambda timeout=60: True
    orig_exists = os.path.exists
    orig_listdir = os.listdir
    os.path.exists = lambda p: False if p == "/etc/cups/printers.conf" else orig_exists(p)
    os.listdir = lambda d: [] if d == "/etc/cups/ppd" else orig_listdir(d)
    try:
        I._nuclear_reset_printers()  # 不应抛异常
    finally:
        I._run = saved_run
        I._ensure_cups_stopped = saved_es
        I._wait_cups_ready = saved_wr
        os.path.exists = orig_exists
        os.listdir = orig_listdir
    print("PASS: 无 printers.conf 时核弹级重置仍幂等不报错")


def test_ensure_error_policy_abort_sets_policy():
    """0.2.45 复盘关键：add_printer 健康快速通道此前漏设 ErrorPolicy，导致服务端重打。
    验证 _ensure_error_policy_abort 确实向 lpadmin 写入 printer-error-policy=abort-job。"""
    saved_exists = P.printer_exists
    saved_run = P.C.run
    calls = []
    P.printer_exists = lambda n: True
    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return (0, "", "")
    P.C.run = fake_run
    try:
        P._ensure_error_policy_abort("hp_laserjet_p1505n_printbox")
    finally:
        P.printer_exists = saved_exists
        P.C.run = saved_run
    expected = ["lpadmin", "-p", "hp_laserjet_p1505n_printbox", "-o", "printer-error-policy=abort-job"]
    assert expected in calls, f"应调用 {expected}，实际: {calls}"
    print("PASS: _ensure_error_policy_abort 确实写入 printer-error-policy=abort-job")


def test_ensure_error_policy_abort_skips_missing_queue():
    """队列不存在时不该发任何 lpadmin（避免对幽灵队列报错/误建）。"""
    saved_exists = P.printer_exists
    saved_run = P.C.run
    calls = []
    P.printer_exists = lambda n: False
    P.C.run = lambda cmd, **kw: calls.append(list(cmd)) or (0, "", "")
    try:
        P._ensure_error_policy_abort("ghost_printer")
    finally:
        P.printer_exists = saved_exists
        P.C.run = saved_run
    assert calls == [], f"队列不存在时不应调用 lpadmin，实际: {calls}"
    print("PASS: 队列不存在时 _ensure_error_policy_abort 不调用 lpadmin")


if __name__ == "__main__":
    test_guard_cancels_duplicate_same_doc_user()
    test_guard_keeps_different_user_and_empty_name()
    test_guard_safe_skip_on_lpstat_failure()
    test_nuclear_reset_sequence()
    test_nuclear_resets_when_no_conf_present()
    test_ensure_error_policy_abort_sets_policy()
    test_ensure_error_policy_abort_skips_missing_queue()
    print("ALL 0.2.45 GUARD/NUCLEAR TESTS PASSED")
