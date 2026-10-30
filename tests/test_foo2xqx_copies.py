"""回归测试：foo2xqx 包装脚本在「份数」上只应计一次。

曾经缺陷（0.2.48 及之前）：包装脚本把 COPIES 同时传给 pdftopdf（已在页数里翻倍）
和 foomatic（foo2xqx 再乘一次），导致 实际份数 = 请求份数²。
本测试用假的 pdftopdf/foomatic-rip 记录各自收到的第 4 个参数（COPIES），断言：
  - 正常路径：pdftopdf 收到请求份数，foomatic 收到 1（不二次计份）。
  - 回退路径（pdftopdf 未产出合法 PDF）：foomatic 保留原请求份数。

注意：本测试在 Git Bash（Windows）下运行，故假二进制一律放在 /tmp 下、使用正斜杠路径，
避免 Windows 反斜杠路径被 bash 拒绝（真实 Debian 盒上 _resolve_filter 返回的是
/usr/lib/cups/filter/... 合法 Linux 路径，不存在此问题）。
"""
import os
import sys
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import printbox.printers as P

BASE = "/tmp/printbox_copiestest"


def _ensure_base():
    os.makedirs(BASE, exist_ok=True)


def _make_fake(name, log_path):
    _ensure_base()
    path = os.path.join(BASE, name)
    if name == "pdftopdf":
        script = (
            '#!/bin/bash\n'
            'echo "pdftopdf_copies=$4" >> "%s"\n'
            'printf "%%PDF-1.4\\npage\\n"\n'
        ) % log_path
    else:  # foomatic-rip
        script = (
            '#!/bin/bash\n'
            'echo "foomatic_copies=$4" >> "%s"\n'
            'cat\n'
        ) % log_path
    with open(path, "w") as f:
        f.write(script)
    os.chmod(path, 0o755)
    return path


def _make_bad_pdftopdf(log_path):
    _ensure_base()
    path = os.path.join(BASE, "pdftopdf_bad")
    script = (
        '#!/bin/bash\n'
        'echo "pdftopdf_copies=$4" >> "%s"\n'
        ': > /dev/null\n'
    ) % log_path
    with open(path, "w") as f:
        f.write(script)
    os.chmod(path, 0o755)
    return path


def _install_with_fakes(pdf_path, foom_path):
    saved_wrapper = P.FOO2XQX_WRAPPER
    saved_old = P.FOO2XQX_WRAPPER_OLD
    saved_resolve = P._resolve_filter
    _ensure_base()
    tmp_wrapper = os.path.join(BASE, "printbox-foo2xqx-pdf")
    P.FOO2XQX_WRAPPER = tmp_wrapper
    P.FOO2XQX_WRAPPER_OLD = "/nonexistent/old-wrapper"
    P._resolve_filter = lambda n: pdf_path if n == "pdftopdf" else foom_path
    try:
        ok = P._install_foo2xqx_wrapper()
    finally:
        P.FOO2XQX_WRAPPER = saved_wrapper
        P.FOO2XQX_WRAPPER_OLD = saved_old
        P._resolve_filter = saved_resolve
    return ok, tmp_wrapper


def _run_wrapper(wrapper, copies):
    infile = os.path.join(BASE, "in.pdf")
    with open(infile, "w") as f:
        f.write("%PDF-1.4\n")
    return subprocess.run(
        ["bash", wrapper, "7", "user", "title", str(copies), "fit-to-page", infile],
        capture_output=True, text=True,
    )


def test_wrapper_passes_copies_once():
    log = os.path.join(BASE, "copies.log")
    if os.path.exists(log):
        os.remove(log)
    pdf = _make_fake("pdftopdf", log)
    foom = _make_fake("foomatic-rip", log)
    ok, wrapper = _install_with_fakes(pdf, foom)
    assert ok is True, "wrapper 安装失败"
    r = _run_wrapper(wrapper, 2)
    assert r.returncode == 0, r.stderr
    content = open(log).read()
    assert "pdftopdf_copies=2" in content, content
    assert "foomatic_copies=1" in content, content   # 关键：foomatic 收到 1
    assert "foomatic_copies=2" not in content, content
    print("  [ok] 成功路径：pdftopdf 收 2，foomatic 收 1（不再 2²）")


def test_wrapper_fallback_keeps_copies():
    log = os.path.join(BASE, "copies_fb.log")
    if os.path.exists(log):
        os.remove(log)
    pdf = _make_bad_pdftopdf(log)
    foom = _make_fake("foomatic-rip", log)
    ok, wrapper = _install_with_fakes(pdf, foom)
    assert ok is True
    r = _run_wrapper(wrapper, 3)
    assert r.returncode == 0, r.stderr
    content = open(log).read()
    assert "foomatic_copies=3" in content, content  # 回退：保留原份数
    print("  [ok] 回退路径：pdftopdf 失效时 foomatic 保留请求份数 3")


if __name__ == "__main__":
    test_wrapper_passes_copies_once()
    test_wrapper_fallback_keeps_copies()
    print("ALL PASS ✅")
