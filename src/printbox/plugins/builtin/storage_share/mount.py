# -*- coding: utf-8 -*-
"""USB 存储自动挂载（storage-share 插件用）。

能力 / Capabilities
-------------------
a) 自动挂载 USB 接口的存储（udev 热插拔触发 `printbox usb-mount`，或 WebUI 一键挂载）
b) 区分系统盘：绝不挂载系统自身所在的硬盘（/ /boot /efi 所在磁盘），即使插在 USB 上也跳过
c) 用系统已建立的账户/组访问磁盘：挂载目录归 pb-disk 组（可写），owner 取共享归属账户

纯函数（parse_lsblk / is_system_device / build_mount_opts / device_disk）与系统调用分离，便于单测。
"""
import os
import json
import subprocess

try:
    from printbox import config as C
except Exception:  # 独立单元测试时 printbox 可能不在 path
    C = None

MOUNT_BASE = "/mnt/printbox"
DISK_GROUP = "pb-disk"


# ====================== 纯函数（可单测） ======================

def device_disk(name):
    """纯函数：从设备名推导磁盘名 / Derive the disk a partition belongs to.

    sda1 -> sda ; nvme0n1p2 -> nvme0n1 ; mmcblk0p2 -> mmcblk0 ; sda -> sda
    """
    import re
    return re.sub(r"p?\d+$", "", name)


def _flatten(devs, parent_disk=None, parent_tran="", parent_rm=False):
    """纯函数：把 lsblk 的树形结构展平成叶子设备列表，并把磁盘的 TRAN/RM 透传给分区。"""
    rows = []
    name = devs.get("name", "")
    tran = devs.get("tran") or parent_tran
    rm = bool(devs.get("rm")) or parent_rm
    disk = parent_disk or name
    rows.append({
        "name": name,
        "path": devs.get("path") or ("/dev/" + name if name else ""),
        "model": devs.get("model") or "",
        "size": devs.get("size") or "",
        "fstype": devs.get("fstype") or "",
        "mountpoint": devs.get("mountpoint") or "",
        "tran": tran,
        "rm": rm,
        "uuid": devs.get("uuid") or "",
        "label": devs.get("label") or "",
        "disk": disk,
    })
    for c in devs.get("children", []) or []:
        rows.extend(_flatten(c, disk, tran, rm))
    return rows


def parse_lsblk(obj):
    """纯函数：把 lsblk -J 的 dict 解析为设备行列表。"""
    if not isinstance(obj, dict):
        return []
    rows = []
    for dev in obj.get("blockdevices", []) or []:
        rows.extend(_flatten(dev))
    return rows


def _system_disks(mounts_text):
    """纯函数：从 /proc/mounts 文本推导『承载 / /boot /efi 的磁盘集合』。"""
    disks = set()
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        src, mp = parts[0], parts[1]
        if mp in ("/", "/boot", "/efi", "/boot/efi") and src.startswith("/dev/"):
            disks.add(device_disk(src[len("/dev/"):]))
    return disks


def is_system_device(name, mounts_text=None):
    """纯函数：该设备是否位于系统盘（承载 / /boot /efi 的磁盘）。

    mounts_text 缺省时读 /proc/mounts。返回 True 表示『是系统设备，禁止自动挂载』。
    """
    if mounts_text is None:
        try:
            with open("/proc/mounts", "r", encoding="utf-8") as f:
                mounts_text = f.read()
        except Exception:
            mounts_text = ""
    disk = device_disk(name)
    return disk in _system_disks(mounts_text)


def _safe_name(dev, label=""):
    """纯函数：把设备名/卷标清洗成可用的挂载子目录名（去路径分隔与高危字符）。"""
    import re
    base = (label or dev or "usb").strip()
    base = re.sub(r"[^A-Za-z0-9_\-\.]", "_", base)
    if not base:
        base = dev or "usb"
    return base[:40]


def build_mount_opts(fstype, owner="", group=DISK_GROUP):
    """纯函数：根据文件系统类型生成 mount -o 选项。

    - vfat/exfat/ntfs：无 POSIX 权限，用 uid/gid + umask 控制归属与可写性；
    - ext*/xfs/btrfs：有真实权限，只把根目录组设为 pb-disk、umask 放宽；
    - 其它：只读尝试。
    """
    if fstype in ("vfat", "exfat", "ntfs", "ntfs-3g"):
        opts = ["rw", "gid=%s" % group, "umask=002", "nofail"]
        if owner:
            opts.insert(1, "uid=%s" % owner)
        if fstype == "ntfs":
            opts.append("windows_names")
        return ",".join(opts)
    if fstype in ("ext2", "ext3", "ext4", "xfs", "btrfs"):
        return "rw,gid=%s,umask=002,nofail" % group
    return "rw,nofail"


def pick_usb_candidates(rows, mounts_text=None):
    """纯函数：从 lsblk 行列表中筛出『可自动挂载的 USB 存储分区』。

    条件：有文件系统 / 未挂载 / 传输为 usb 或可移动 / 非系统盘。
    """
    out = []
    for d in rows:
        if not d.get("fstype"):
            continue
        if d.get("mountpoint"):
            continue
        usb_or_removable = (d.get("tran") == "usb") or bool(d.get("rm"))
        if not usb_or_removable:
            continue
        if is_system_device(d["name"], mounts_text):
            continue
        out.append(d)
    return out


# ====================== 实时查询（调 subprocess） ======================

def list_block_devices():
    """调用 lsblk -J 返回展平后的设备列表（含系统盘）。无 lsblk 时返回 []。"""
    r = _run(["lsblk", "-J", "-o",
              "NAME,MODEL,SIZE,FSTYPE,MOUNTPOINT,PATH,TRAN,RM,UUID,LABEL"])
    if r.returncode != 0:
        return []
    try:
        return parse_lsblk(json.loads(r.stdout))
    except Exception:
        return []


def list_usb_drives():
    """返回当前『未挂载、可自动挂载的 USB 存储分区』列表。"""
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as f:
            mounts_text = f.read()
    except Exception:
        mounts_text = ""
    return pick_usb_candidates(list_block_devices(), mounts_text)


def list_mounted_shares():
    """返回已挂载在 MOUNT_BASE 下的 USB 共享（供 WebUI 展示）。"""
    out = []
    if not os.path.isdir(MOUNT_BASE):
        return out
    for name in os.listdir(MOUNT_BASE):
        mp = os.path.join(MOUNT_BASE, name)
        if not os.path.ismount(mp):
            continue
        out.append({"name": name, "mountpoint": mp})
    return out


# ====================== 挂载/卸载 ======================

def mount_usb(dev_name, owner="", group=DISK_GROUP, mountpoint=None, fstype=None,
              share_root=None, label=None):
    """挂载一个 USB 分区。

    挂载点优先级：
      1) 显式 mountpoint；
      2) share_root 非空时挂到 <share_root>/<清洗后的卷标或设备名>（这样 U 盘会出现在 SMB 共享里）；
      3) 否则挂到 MOUNT_BASE/<dev>（/mnt/printbox/<dev>）。
    返回 (ok, msg)。会先排除系统盘（is_system_device）。
    """
    if dev_name.startswith("/dev/"):
        dev = dev_name
        short = dev_name[len("/dev/"):]
    else:
        dev = "/dev/" + dev_name
        short = dev_name
    if is_system_device(short):
        return False, "拒绝挂载系统盘设备: %s" % short
    if mountpoint is None:
        if share_root:
            os.makedirs(share_root, exist_ok=True)
            mountpoint = os.path.join(share_root, _safe_name(short, label))
        else:
            mountpoint = os.path.join(MOUNT_BASE, short)
    os.makedirs(mountpoint, exist_ok=True)
    if not fstype:
        fstype = _detect_fstype(dev)
    if not fstype:
        return False, "无法识别文件系统类型: %s" % dev
    opts = build_mount_opts(fstype, owner, group)
    r = _run(["mount", "-o", opts, dev, mountpoint])
    if r.returncode != 0:
        return False, (r.stderr.strip() or "mount 失败")
    # 确保 pb-disk 组可写（vfat/exfat 已靠 umask 处理；ext 再补一次 chmod 兜底）
    _run(["chown", "%s:%s" % (owner or "root", group), mountpoint])
    _run(["chmod", "775", mountpoint])
    if C:
        C.log("usb-mount", "INFO", "已挂载 %s -> %s (fstype=%s)" % (dev, mountpoint, fstype))
    return True, mountpoint


def umount_usb(dev_name_or_mountpoint):
    """卸载一个 USB 共享（按设备名或挂载点）。返回 (ok, msg)。"""
    if dev_name_or_mountpoint.startswith("/"):
        target = dev_name_or_mountpoint
    else:
        target = os.path.join(MOUNT_BASE, dev_name_or_mountpoint)
    r = _run(["umount", target])
    if r.returncode != 0:
        return False, (r.stderr.strip() or "umount 失败")
    if C:
        C.log("usb-mount", "INFO", "已卸载 %s" % target)
    return True, "已卸载 %s" % target


def auto_mount_all(owner="", group=DISK_GROUP, share_root=None):
    """挂载当前所有符合资格的 USB 存储分区。返回 [(ok, msg), ...]。

    share_root 非空时把 U 盘挂进共享目录（使其出现在 SMB 共享里）。
    """
    results = []
    for d in list_usb_drives():
        results.append(mount_usb(d["name"], owner=owner, group=group,
                                 share_root=share_root, label=d.get("label")))
    return results


# ====================== CLI 入口（给 udev / 命令行用） ======================

def _owner_group_from_config():
    """从 storage-share 插件配置取共享归属账户与组；取不到则默认空 owner + pb-disk。"""
    owner = ""
    group = DISK_GROUP
    share_root = ""
    if C is None:
        return owner, group, share_root
    try:
        from printbox.plugins import manager as MG
        if MG:
            cfg = MG.get_config("storage-share")
            if cfg:
                owner = cfg.get("share_owner") or ""
                share_root = cfg.get("share_path") or ""
    except Exception:
        pass
    return owner, group, share_root


def usb_mount_main(dev_name, force=False, share_root=None):
    """udev / 命令行入口：`printbox usb-mount <dev>`。

    - force=True（手动）：无条件挂载（仍排除系统盘）。
    - force=False（udev 自动触发）：仅当 storage-share 插件开启 auto_mount 才挂载，否则跳过。
    返回 (ok, msg)。
    """
    short = dev_name[len("/dev/"):] if dev_name.startswith("/dev/") else dev_name
    if not force:
        auto = False
        try:
            from printbox.plugins import manager as MG
            if MG:
                cfg = MG.get_config("storage-share")
                auto = bool(cfg.get("auto_mount")) if cfg else False
        except Exception:
            auto = False
        if not auto:
            if C:
                C.log("usb-mount", "INFO",
                      "auto_mount 未开启，跳过自动挂载 %s" % short)
            return False, "auto_mount 未开启，已跳过"
    owner, group, cfg_root = _owner_group_from_config()
    if share_root is None:
        share_root = cfg_root
    return mount_usb(short, owner=owner, group=group, share_root=share_root)


def usb_umount_main(dev_name):
    """udev / 命令行入口：`printbox usb-umount <dev>`。"""
    return umount_usb(dev_name)


# ====================== 工具 ======================

def _detect_fstype(dev):
    r = _run(["blkid", "-o", "value", "-s", "TYPE", dev])
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().splitlines()[0]
    return ""


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except Exception as e:
        class _R:
            returncode = -1
            stdout = ""
            stderr = str(e)
        return _R()
