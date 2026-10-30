"""运行级 mock 实跑：验证 init._stabilize_cups_for_rebuild 的编排不崩、调用顺序正确，
且「杯前已崩溃」时直接核弹级重置（整体移走 printers.conf）、「启动后才崩溃」时走软清场+紧急清场兜底。

0.2.45 复盘：崩溃循环里 cupsd 每次重启后会有瞬间 "running"，_wait_cups_ready 易误判为已就绪，
从而跳过紧急全量清场，导致损坏/raw 的 printers.conf 原样留下、继续崩。故修复为「停服前先判定
_cupsd_recently_crashed(120)，一旦命中崩溃循环直接核弹级重置并 early-return」。

模拟 _run / printers.* 等系统调用，无需真实盒子即可确定性验证修复链路。
"""
import os
import sys
import subprocess

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, os.path.abspath(SRC))

import printbox.init as I
import printbox.printers as P


class FakeCP():
    """模仿 subprocess.CompletedProcess，支持 .returncode/.stdout/.stderr 取用。"""
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def make_env(crash_before_stop=False, crash_after_start=False, cups_running=False,
             recent_crash=False):
    calls = []
    wiped_managed = {"n": 0}
    wiped_all = {"n": 0}
    sanitized = {"n": 0}
    nuclear = {"n": 0}
    post_start_count = {"n": 0}  # 统计 window=30 的 journalctl 调用次数
    state = {"cups_running": cups_running}

    def fake_run(cmd, **kw):
        c = list(cmd)
        calls.append(c)
        if c[:3] == ["systemctl", "is-active", "cups"]:
            return FakeCP(0 if state["cups_running"] else 3)
        if c[:2] == ["systemctl", "start", "cups"]:
            state["cups_running"] = True
            return FakeCP(0)
        if c[:2] == ["lpstat", "-r"]:
            return FakeCP(0, "scheduler is running\n" if state["cups_running"]
                          else "scheduler is not running\n")
        if c and c[0] == "journalctl":
            # 停服前 window=120 看 crash_before_stop；stable 分支 window=15 看 recent_crash；
            # 之后 window=30 的调用：第 1 次是 _stabilize 启动后判定，第 2 次是 _emergency 内部升级判定
            arg = " ".join(c)
            if "120 seconds ago" in arg:
                return FakeCP(0, "status=1/FAILURE\n" if crash_before_stop else "")
            if "15 seconds ago" in arg:
                return FakeCP(0, "status=1/FAILURE\n" if recent_crash else "")
            post_start_count["n"] += 1
            if post_start_count["n"] == 1:
                return FakeCP(0, "status=1/FAILURE\n" if crash_after_start else "")
            # 紧急清场内部二次判定：认为已稳定，不再升级到核弹级（否则掩盖主路径断言）
            return FakeCP(0, "")
        return FakeCP(0)

    def fake_wipe_managed(printers_conf="/etc/cups/printers.conf", ppd_dir="/etc/cups/ppd"):
        wiped_managed["n"] += 1
        return 1

    def fake_sanitize(ppd_dir="/etc/cups/ppd"):
        sanitized["n"] += 1

    def fake_wipe_all(printers_conf="/etc/cups/printers.conf", ppd_dir="/etc/cups/ppd"):
        wiped_all["n"] += 1

    def fake_nuclear():
        nuclear["n"] += 1

    # 打桩
    I._run = fake_run
    I._nuclear_reset_printers = fake_nuclear
    I._ensure_cups_stopped = lambda timeout=20: True
    I._wait_cups_ready = lambda timeout=30: True
    P._wipe_managed_queues_offline = fake_wipe_managed
    P._sanitize_ppd_filter_paths = fake_sanitize
    P._wipe_all_printers_offline = fake_wipe_all

    return calls, wiped_managed, wiped_all, sanitized, nuclear


def test_stabilize_happy_path():
    calls, wm, wa, sa, nu = make_env()
    I._stabilize_cups_for_rebuild()
    assert wm["n"] == 1, f"离线清空托管队列应被调用 1 次，实际 {wm['n']}"
    assert sa["n"] == 1, f"离线 PPD 净化应被调用 1 次，实际 {sa['n']}"
    assert wa["n"] == 0, f"稳定路径不应触发紧急全量清场，实际 {wa['n']}"
    assert nu["n"] == 0, f"稳定路径不应触发核弹级重置，实际 {nu['n']}"
    seq = [" ".join(c[:3]) for c in calls]
    assert any(s == "systemctl mask cups" for s in seq), "应先 mask cups"
    assert any(s == "systemctl unmask cups" for s in seq), "应最后 unmask cups"
    assert any(s == "systemctl start cups" for s in seq), "应 start cups"
    print("PASS: 稳定路径编排正确（mask→stop→清场→净化→unmask→start，无紧急/核弹级清场）")


def test_stabilize_nuclear_when_crashing_before_stop():
    """0.2.45 复盘核心：停服前就检测到崩溃循环 → 直接核弹级重置并 early-return。"""
    calls, wm, wa, sa, nu = make_env(crash_before_stop=True)
    I._stabilize_cups_for_rebuild()
    assert nu["n"] == 1, f"崩溃循环应触发核弹级重置 1 次，实际 {nu['n']}"
    # 关键：核弹级路径不应再走软清场（正则清场在损坏文件上不可靠，正是此前漏清的根因）
    assert wm["n"] == 0, f"核弹级路径不应再走软清场，实际 {wm['n']}"
    assert wa["n"] == 0, f"核弹级路径不应再走紧急全量清场，实际 {wa['n']}"
    print("PASS: 停服前检测到崩溃循环 → 直接核弹级重置（整体移走 printers.conf），跳过不可靠的正则软清场")


def test_stabilize_emergency_fallback():
    """停服前未崩溃、但启动后 cupsd 仍崩 → 软清场 + 紧急全量清场兜底（保留原行为）。"""
    calls, wm, wa, sa, nu = make_env(crash_after_start=True)
    I._stabilize_cups_for_rebuild()
    assert wm["n"] == 1, f"常规清场应跑 1 次，实际 {wm['n']}"
    assert wa["n"] == 1, f"应急全量清场应被触发 1 次，实际 {wa['n']}"
    assert nu["n"] == 0, f"此路径不应走核弹级重置（由紧急清场覆盖），实际 {nu['n']}"
    print("PASS: 启动后 cupsd 仍崩溃时正确触发紧急全量清场兜底")


def test_stabilize_skip_when_already_stable():
    """0.2.46 新增：cupsd 当前已稳定运行（在跑且近 15s 无崩溃）→ 不拆除它，直接返回，
    由后续 main() 在稳定 cupsd 上重建队列。避免「核弹级重置重启 cupsd」制造 USB 枚举空窗、
    导致 main() 探测拿到空而漏建队列。"""
    calls, wm, wa, sa, nu = make_env(cups_running=True, recent_crash=False)
    I._stabilize_cups_for_rebuild()
    # 在线 PPD 净化应执行一次（轻量、不改 printers.conf、不重启，安全）
    assert sa["n"] == 1, f"在线 PPD 净化应执行 1 次，实际 {sa['n']}"
    # 绝不应走离线清场 / 紧急清场 / 核弹级重置
    assert wm["n"] == 0, f"已稳定路径不应离线清空托管队列，实际 {wm['n']}"
    assert wa["n"] == 0, f"已稳定路径不应触发紧急全量清场，实际 {wa['n']}"
    assert nu["n"] == 0, f"已稳定路径不应触发核弹级重置，实际 {nu['n']}"
    seq = [" ".join(c[:3]) for c in calls]
    assert not any(s == "systemctl mask cups" for s in seq), "已稳定路径不应 mask cups"
    assert not any(s == "systemctl stop cups" for s in seq), "已稳定路径不应 stop cups"
    assert not any(s == "systemctl start cups" for s in seq), "已稳定路径不应 start cups"
    print("PASS: cupsd 已稳定时跳过离线清场与重启（避免重建竞赛 USB 枚举空窗漏建队列）")


def test_fix_cups_spool_and_backends():
    """0.2.46 固化：修复 spool 目录权限 + 停用 HP 私有后端，防止杯崩循环复发。"""
    created = []
    renamed = []
    exists_map = {}

    def fake_makedirs(d, exist_ok=False):
        created.append(d)

    def fake_run(cmd, **kw):
        return FakeCP(0)

    def fake_exists(p):
        if p.endswith(".disabled"):
            return exists_map.get(p, False)
        # 初始：hpfax / hp 私有后端存在，且尚未 disabled
        if p in ("/usr/lib/cups/backend/hpfax", "/usr/lib/cups/backend/hp"):
            return True
        return False

    def fake_rename(src, dst):
        renamed.append((src, dst))
        exists_map[dst] = True

    orig = (I.os.makedirs, I.os.chmod, I.os.path.exists, I.os.rename, I._run)
    I.os.makedirs = fake_makedirs
    I.os.chmod = lambda d, m: None
    I.os.path.exists = fake_exists
    I.os.rename = fake_rename
    I._run = fake_run
    try:
        I._fix_cups_spool_and_backends()
    finally:
        (I.os.makedirs, I.os.chmod, I.os.path.exists, I.os.rename, I._run) = orig
    assert "/var/spool/cups/tmp" in created, "应创建 spool tmp 目录"
    assert any(r[0] == "/usr/lib/cups/backend/hpfax" for r in renamed), "应停用 hpfax 后端"
    assert any(r[0] == "/usr/lib/cups/backend/hp" for r in renamed), "应停用 hp 后端"
    print("PASS: _fix_cups_spool_and_backends 修目录权限并停用 HP 私有后端")


if __name__ == "__main__":
    test_stabilize_happy_path()
    test_stabilize_nuclear_when_crashing_before_stop()
    test_stabilize_emergency_fallback()
    test_stabilize_skip_when_already_stable()
    test_fix_cups_spool_and_backends()
    print("ALL STABILIZE-CUPS TESTS PASSED")
