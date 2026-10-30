"""PrintBox 命令行入口（console_scripts: printbox）。

子命令：
  init      把本机配置为打印服务器（安装系统依赖 + 写入并启用 systemd 服务）；可加 --no-apt 跳过安装
  repair    修复模式：不装依赖，仅重启服务并重新发布打印机到局域网（适合 init 中断后补救）
  start     启动全部服务（含开机自启已由 init 设置）
  stop      停止全部服务
  restart   重启全部服务
  status    查看服务状态
  detect    探测并添加打印机（自动安装驱动）
  web       仅在前台运行 Web 管理界面（调试用）
  archive   执行一次/守护归档扫描（--daemon 持续运行）
  prune     删除设备已移除的幽灵打印机队列（如裸名 print）
  version   显示版本
"""
import argparse
import os
import subprocess
import sys

from . import __version__
from . import config as C

SERVICES = ["printbox-web", "printbox-archive", "printbox-printer-hotplug"]


def _need_root():
    if os.geteuid() != 0:
        sys.exit("错误：该命令需要 root 权限，请使用 sudo 运行（例如：sudo printbox init）。")


def _systemctl(action, services=None):
    cmd = ["systemctl", action]
    if services:
        cmd += services
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.stdout:
        print(r.stdout.rstrip())
    if r.stderr:
        print(r.stderr.rstrip(), file=sys.stderr)
    return r.returncode


def cmd_init(args):
    _need_root()
    from . import init
    init.provision(skip_apt=getattr(args, "no_apt", False))


def cmd_repair(args):
    _need_root()
    from . import init
    init.repair()


def cmd_start(args):
    _need_root()
    _systemctl("start", SERVICES)
    print("已启动 PrintBox 服务。")


def cmd_stop(args):
    _need_root()
    _systemctl("stop", SERVICES)
    print("已停止 PrintBox 服务。")


def cmd_restart(args):
    _need_root()
    _systemctl("restart", SERVICES)
    print("已重启 PrintBox 服务。")


def cmd_status(args):
    _need_root()
    for s in SERVICES:
        r = subprocess.run(["systemctl", "is-active", "--quiet", s])
        state = "运行中" if r.returncode == 0 else "未运行"
        print(f"  {s:28s} {state}")


def cmd_detect(args):
    _need_root()
    from . import printers
    printers.main()


def cmd_web(args):
    from . import webapp
    webapp.main()


def cmd_republish(args):
    _need_root()
    from . import printers
    printers.republish_airprint()
    print("已重新发布打印机到局域网（AirPrint/mDNS）。手机/电脑应能在数秒内发现。")


def cmd_mdns_check(args):
    _need_root()
    from . import printers
    msg = printers.self_heal_publish()
    print(msg)


def cmd_prune(args):
    _need_root()
    from . import printers
    removed = printers.prune_phantom_printers()
    if removed:
        print(f"已删除幽灵打印机队列: {', '.join(removed)}")
    else:
        print("未发现需要清理的幽灵打印机队列。")


def cmd_guard(args):
    _need_root()
    from . import printers
    n = printers.guard_runaway_jobs()
    if n:
        print(f"已取消 {n} 个重复在途作业（打印洪流防护）。")
    else:
        print("未检测到重复在途作业。")


def cmd_archive(args):
    from . import archive
    if args.daemon:
        archive.main()
    else:
        archive.run_once()
        print("归档扫描完成。")


def cmd_version(args):
    print(f"printbox {__version__}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="printbox", description="PrintBox 无头打印服务器")
    sub = p.add_subparsers(dest="cmd")

    p_init = sub.add_parser("init", help="配置本机为打印服务器（安装依赖 + 启用服务）")
    p_init.add_argument("--no-apt", action="store_true",
                        help="跳过 apt 安装，仅做配置/服务/发布（适合 install 中断后补救）")
    sub.add_parser("repair", help="修复模式：不装依赖，仅重启服务并重新发布打印机到局域网")
    sub.add_parser("start", help="启动全部服务")
    sub.add_parser("stop", help="停止全部服务")
    sub.add_parser("restart", help="重启全部服务")
    sub.add_parser("status", help="查看服务状态")
    sub.add_parser("detect", help="探测并添加打印机（自动安装驱动）")
    sub.add_parser("web", help="前台运行 Web 管理界面（调试）")
    sub.add_parser("republish", help="重新发布打印机到局域网（AirPrint/mDNS）")
    sub.add_parser("mdns-check", help="自检 AirPrint 是否在局域网可见，不可见则自动重新发布（供定时器调用）")
    sub.add_parser("prune", help="删除设备已移除的幽灵打印机队列（如裸名 print）并清理其服务文件")
    sub.add_parser("guard", help="打印洪流防护：取消同文档重复提交的在途作业（由定时器定期调用）")
    pa = sub.add_parser("archive", help="执行归档扫描")
    pa.add_argument("--daemon", action="store_true", help="持续守护运行")
    sub.add_parser("version", help="显示版本")

    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return

    {
        "init": cmd_init,
        "repair": cmd_repair,
        "start": cmd_start,
        "stop": cmd_stop,
        "restart": cmd_restart,
        "status": cmd_status,
        "detect": cmd_detect,
        "web": cmd_web,
        "archive": cmd_archive,
        "republish": cmd_republish,
        "mdns-check": cmd_mdns_check,
        "prune": cmd_prune,
        "guard": cmd_guard,
        "version": cmd_version,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
