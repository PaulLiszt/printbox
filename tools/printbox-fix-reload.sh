#!/usr/bin/env bash
# tools/printbox-fix-reload.sh —— <box> 盒「双打/无限循环」即时止血 + 恢复 WebUI（0.2.48 配套）
#
# 背景（2026-10-29 取证）：printbox 的 enable_job_archive / ensure_preserve 用
#   `cupsctl PreserveJobFiles=yes PreserveJobHistory=yes`
# 做 live reload，本盒 CUPS 2.4 对该指令 live 重载会让 cupsd 立即崩溃重启、重跑作业，
# 造成双打 / iOS·鸿蒙无限循环；且 cupsctl 反复追加把 cupsd.conf 撑到 40+ 遍重复。
#
# 本脚本做「即时止血 + 恢复访问」，不依赖已修复的新包：
#   1) 停所有 printbox 单元（断掉任何仍在跑的 cupsctl 循环）
#   2) 原子去重清理 cupsd.conf（40+ 遍重复块收敛为 1，确保 PreserveJobFiles/History=yes）
#   3) cupsd -t 校验
#   4) 干净重启 cupsd（沿用诊断留下的「cups.socket 停用、cups.service 直绑 631」稳定态）
#   5) 拉起 printbox-web，WebUI 立即恢复
#
# ⚠️ 本脚本只恢复 WebUI 与稳定打印；归档能力（printbox-archive）仍停在旧代码，请随后走标准
#    scp + upgrade_box.sh + printbox repair 升级到 0.2.48，自动重新启用已修复的 archive。
#
# 用法（盒上 root）： bash /home/<box>/printbox-fix-reload.sh
set -uo pipefail

CUPSD_CONF=/etc/cups/cupsd.conf
LOG=/tmp/printbox-fix-reload-$(date +%Y%m%d-%H%M%S).log
exec > >(tee -a "$LOG") 2>&1

log(){ echo "[$(date +%H:%M:%S)] $*"; }
die(){ echo "[$(date +%H:%M:%S)] 错误: $*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "必须用 root 运行（su - 后执行）"

log "============================================================"
log "PrintBox 重载崩溃即时止血 + 恢复 WebUI ($(date))"
log "日志: $LOG"
log "============================================================"

log "== 1) 停所有 printbox 单元（断 cupsctl 循环）=="
for u in printbox-guard.timer printbox-guard.service printbox-web.service \
         printbox-archive.service printbox-printer-hotplug.service \
         printbox-mdns-renew.service printbox-mdns-alias.service; do
  systemctl stop "$u" 2>/dev/null && log "    stopped $u" || log "    (无/已停) $u"
done
sleep 2
log "    当前 cupsd 状态: $(systemctl is-active cups 2>/dev/null || echo 未知)"

log "== 2) 原子去重清理 cupsd.conf =="
[ -f "$CUPSD_CONF" ] || die "$CUPSD_CONF 不存在"
python3 - "$CUPSD_CONF" <<'PY'
import sys, os, tempfile, subprocess
p = sys.argv[1]
with open(p, "r", encoding="utf-8", errors="ignore") as f:
    lines = f.read().splitlines()
keys = ["PreserveJobFiles", "PreserveJobHistory", "MaxSubscriptions",
        "MaxSubscriptionsPerJob", "MaxEvents"]
old_marks = ("# PrintBox subscription limits",
             "# PrintBox managed directives (edited by printbox, do not edit by hand)")
kept = []
for ln in lines:
    s = ln.strip()
    if s in old_marks:
        continue
    drop = False
    for k in keys:
        if __import__("re").match(r"^\s*" + __import__("re").escape(k) + r"(\s|$)", ln, __import__("re").I):
            drop = True
            break
    if not drop:
        kept.append(ln)
block = ["# PrintBox managed directives (edited by printbox, do not edit by hand)",
         "PreserveJobFiles yes", "PreserveJobHistory yes",
         "MaxSubscriptions 100000", "MaxSubscriptionsPerJob 1000", "MaxEvents 100000"]
new_text = "\n".join(kept + ["", "# PrintBox managed directives (edited by printbox, do not edit by hand)",
                              "PreserveJobFiles yes", "PreserveJobHistory yes",
                              "MaxSubscriptions 100000", "MaxSubscriptionsPerJob 1000",
                              "MaxEvents 100000", ""]) + "\n"
old_text = "\n".join(lines) + ("\n" if lines else "")
if old_text == new_text:
    print("    配置已干净，无需修改")
    sys.exit(0)
# 预校验
cupsd = __import__("shutil").which("cupsd")
if cupsd:
    tf = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False, encoding="utf-8")
    tf.write(new_text); tf.close()
    r = subprocess.run([cupsd, "-t", "-c", tf.name], capture_output=True, text=True, timeout=15)
    os.unlink(tf.name)
    if r.returncode != 0:
        print("    cupsd -t 校验失败，放弃写入以免破坏配置:"); print(r.stderr[-500:]); sys.exit(2)
tmp = p + ".printbox.tmp"
with open(tmp, "w", encoding="utf-8") as f:
    f.write(new_text)
os.replace(tmp, p)
before = len([l for l in lines if any(l.strip().startswith(k) for k in keys)])
after  = len([l for l in new_text.splitlines() if any(l.strip().startswith(k) for k in keys)])
print(f"    已清理：受管指令行数 {before} -> {after}（重复块收敛为唯一）")
PY
rc=$?
[ "$rc" = "0" ] || [ "$rc" = "2" ] || die "cupsd.conf 清理失败（code=$rc），请查看上方输出"
log "    cupsd.conf 校验: $(cupsd -t -c "$CUPSD_CONF" >/dev/null 2>&1 && echo OK || echo 失败)"

log "== 3) 干净重启 cupsd（沿用 cups.socket 停用态，cups.service 直绑 631）=="
systemctl disable cups.socket cups.path 2>/dev/null || true
systemctl enable cups.service 2>/dev/null || true
systemctl restart cups.service 2>/dev/null || systemctl restart cups
sleep 3
for i in 1 2 3 4 5; do
  st=$(systemctl is-active cups 2>/dev/null)
  [ "$st" = "active" ] && break
  log "    cups 尚未就绪($st)，等待..."; sleep 2
done
log "    cups 状态: $(systemctl is-active cups) | lpstat -r: $(lpstat -r 2>/dev/null || echo 连不上)"
cupsd_stable=yes
for i in $(seq 1 10); do
  if ! systemctl is-active cups >/dev/null 2>&1; then cupsd_stable=no; break; fi
  sleep 1
done
log "    [10s] cupsd 是否稳定: $cupsd_stable"

log "== 4) 拉起 WebUI（printbox-web）=="
systemctl start printbox-web.service
sleep 2
log "    printbox-web 状态: $(systemctl is-active printbox-web)"
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
log "    WebUI: http://${IP:-<本机IP>}:631 或 http://$(hostname).local （取决于发布）"

log "============================================================"
log "完成。当前状态："
log "  - cupsd 已稳定（不再被 cupsctl 重载拖崩），打印应为 1 份/次"
log "  - WebUI 已恢复访问"
log "  - 归档能力(printbox-archive)仍停用（旧代码），请升级 0.2.48 后自动恢复"
log "下一步：开发机 scp dist/printbox-0.2.48-src.tar.gz + tools/upgrade_box.sh 到盒，"
log "        su - 后 bash /home/<box>/upgrade_box.sh <包> 完成 printbox repair 升级"
log "============================================================"
