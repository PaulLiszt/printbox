"""PrintBox 驱动自动安装。

探测到打印机后，按厂商自动安装对应的 Debian/Ubuntu 驱动包（apt）。
基础驱动集由 init 阶段预装，这里负责「按需补齐」——
例如新插上一台未预装驱动的 Brother/Samsung 打印机时自动装好。
"""
import subprocess

from . import config as C

# 厂商关键字 -> apt 驱动包
DRIVER_PKGS = {
    "hp": ["printer-driver-hpcups", "hplip"],
    "hewlett": ["printer-driver-hpcups", "hplip"],
    "brother": ["printer-driver-brlaser", "printer-driver-foo2zjs"],
    "samsung": ["printer-driver-splix"],
    "xerox": ["printer-driver-splix"],
    "kyocera": ["printer-driver-splix"],
    "lexmark": ["printer-driver-splix"],
    "canon": ["printer-driver-gutenprint"],
    "epson": ["printer-driver-gutenprint", "printer-driver-escpr"],
}

# init 阶段预装的基础驱动集（覆盖绝大多数常见机型）
BASE_DRIVER_PKGS = [
    "printer-driver-gutenprint",
    "printer-driver-hpcups",
    "hplip",
    "printer-driver-foo2zjs",
    "printer-driver-splix",
    "printer-driver-brlaser",
    "printer-driver-postscript-hp",
    "foomatic-db",
    "foomatic-db-engine",
]


def packages_for(mdl):
    m = (mdl or "").lower()
    for key, pkgs in DRIVER_PKGS.items():
        if key in m:
            return pkgs
    return []


def _install(pkgs):
    C.log("drivers", "INFO", f"安装驱动包: {pkgs}")
    r = subprocess.run(["apt-get", "install", "-y"] + pkgs,
                       capture_output=True, text=True)
    if r.returncode != 0:
        C.log("drivers", "WARN", f"驱动安装失败: {r.stderr[-400:]}")
        return False
    C.log("drivers", "INFO", "驱动安装完成")
    return True


def ensure_drivers(mdl):
    """为给定机型确保驱动已安装（按需安装，已装则跳过）。返回是否成功安装/已具备。"""
    pkgs = packages_for(mdl)
    if not pkgs:
        return False
    return _install(pkgs)


def install_base():
    """init 阶段预装基础驱动集。"""
    return _install(BASE_DRIVER_PKGS)
