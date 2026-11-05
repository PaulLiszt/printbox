"""PrintBox USB 自动挂载 + 账户管理 纯函数验证（开发机，不实际挂载/改系统）。

覆盖：
- mount.device_disk：分区名 -> 磁盘名
- mount.parse_lsblk：lsblk 树形 -> 展平列表（TRAN/RM 透传）
- mount.is_system_device：排除 / /boot /efi 所在系统盘
- mount.build_mount_opts：按 fstype 生成挂载选项
- mount.pick_usb_candidates：筛出可自动挂载的 USB 分区
- storage_share.generate_smb_conf：force user / force group / valid users / guest account
- users.render_sshd_policy / parse_sshd_policy：幂等 + 解析
- users.validate_username：拒绝 root / 非法字符
"""
import sys
sys.path.insert(0, "src")
from printbox.plugins.builtin.storage_share import mount as MNT
from printbox.plugins.builtin.storage_share import module as SS
import printbox.users as US

PASS = []


def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)


# ---------- mount.device_disk ----------
check("device_disk sda1->sda", MNT.device_disk("sda1") == "sda")
check("device_disk nvme0n1p2->nvme0n1", MNT.device_disk("nvme0n1p2") == "nvme0n1")
check("device_disk mmcblk0p2->mmcblk0", MNT.device_disk("mmcblk0p2") == "mmcblk0")
check("device_disk sda->sda", MNT.device_disk("sda") == "sda")

# ---------- mount.parse_lsblk ----------
lsblk_json = {
    "blockdevices": [
        {"name": "sda", "tran": "sata", "children": [
            {"name": "sda1", "fstype": "ext4", "mountpoint": "/", "rm": False},
        ]},
        {"name": "sdb", "tran": "usb", "rm": True, "children": [
            {"name": "sdb1", "fstype": "vfat", "mountpoint": "", "rm": True},
        ]},
    ]
}
rows = MNT.parse_lsblk(lsblk_json)
by_name = {r["name"]: r for r in rows}
check("parse_lsblk 展平含分区", "sdb1" in by_name and "sda1" in by_name)
check("parse_lsblk 透传 TRAN 到分区", by_name["sdb1"]["tran"] == "usb")
check("parse_lsblk 透传 RM 到分区", by_name["sdb1"]["rm"] is True)

# ---------- mount.is_system_device ----------
mounts = "/dev/sda2 / ext4 rw 0 0\n/dev/sdb1 /mnt/usb vfat rw 0 0\n"
check("is_system_device 系统盘 sda1=True", MNT.is_system_device("sda1", mounts) is True)
check("is_system_device 系统盘 nvme 同盘=True",
      MNT.is_system_device("nvme0n1p3", "/dev/nvme0n1p2 / ext4 rw 0 0\n") is True)
check("is_system_device 非系统盘 sdb1=False", MNT.is_system_device("sdb1", mounts) is False)
check("is_system_device 非系统盘 sdc1=False", MNT.is_system_device("sdc1", mounts) is False)

# ---------- mount.build_mount_opts ----------
o1 = MNT.build_mount_opts("vfat", owner="alice", group="pb-disk")
check("build_mount_opts vfat 含 uid/gid/umask",
      "uid=alice" in o1 and "gid=pb-disk" in o1 and "umask=002" in o1)
o2 = MNT.build_mount_opts("ntfs", owner="alice")
check("build_mount_opts ntfs 含 windows_names", "windows_names" in o2)
o3 = MNT.build_mount_opts("ext4", owner="", group="pb-disk")
check("build_mount_opts ext4 含 gid", "gid=pb-disk" in o3 and "rw" in o3)
o4 = MNT.build_mount_opts("vfat", owner="", group="pb-disk")
check("build_mount_opts vfat 空 owner 无 uid", "uid=" not in o4)

# ---------- mount.pick_usb_candidates ----------
rows = [
    {"name": "sda", "fstype": "", "mountpoint": "", "tran": "sata", "rm": False},
    {"name": "sda1", "fstype": "ext4", "mountpoint": "/", "tran": "sata", "rm": False},
    {"name": "sdb", "fstype": "", "mountpoint": "", "tran": "usb", "rm": True},
    {"name": "sdb1", "fstype": "vfat", "mountpoint": "", "tran": "usb", "rm": True},
    {"name": "sdc1", "fstype": "ntfs", "mountpoint": "", "tran": "", "rm": True},
    {"name": "sdd1", "fstype": "ext4", "mountpoint": "/mnt/x", "tran": "usb", "rm": True},
    {"name": "nvme0n1p1", "fstype": "vfat", "mountpoint": "", "tran": "", "rm": False},
]
cands = MNT.pick_usb_candidates(rows, mounts)
cand_names = {c["name"] for c in cands}
check("pick_usb_candidates 命中 sdb1/sdc1", cand_names == {"sdb1", "sdc1"})
check("pick_usb_candidates 排除已挂载 sdd1", "sdd1" not in cand_names)
check("pick_usb_candidates 排除非可移动 nvme", "nvme0n1p1" not in cand_names)

# ---------- storage_share.generate_smb_conf 升级字段 ----------
conf_owner = SS.generate_smb_conf(
    {"share_path": "/mnt/usb", "timemachine": True, "guest_ok": False, "share_owner": "bob"})
check("smb force user = bob", "force user = bob" in conf_owner)
check("smb force group = pb-disk", "force group = pb-disk" in conf_owner)
check("smb 非访客含 valid users @pb-disk", "valid users = @pb-disk" in conf_owner)
check("smb 非访客不含 guest account", "guest account" not in conf_owner)
check("smb TM 开启仍含 fruit", "fruit:time machine = yes" in conf_owner)

conf_guest = SS.generate_smb_conf(
    {"share_path": "/mnt/usb", "timemachine": False, "guest_ok": True, "share_owner": "bob"})
check("smb 访客含 guest ok = yes", "guest ok = yes" in conf_guest)
check("smb 访客含 guest account = bob", "guest account = bob" in conf_guest)
check("smb 访客不含 valid users", "valid users" not in conf_guest)

conf_empty = SS.generate_smb_conf(
    {"share_path": "/mnt/usb", "timemachine": False, "guest_ok": True, "share_owner": ""})
check("smb owner 留空无 force user", "force user" not in conf_empty)

# ---------- users.render_sshd_policy / parse ----------
base = "Port 22\n# user config\nX11Forwarding no\n"
new1 = US.render_sshd_policy(base, restrict=True)
check("render_sshd_policy 含 AllowGroups pb-ssh", "AllowGroups pb-ssh" in new1)
check("render_sshd_policy 含 PermitRootLogin no", "PermitRootLogin no" in new1)
check("render_sshd_policy 保留用户配置", "Port 22" in new1 and "X11Forwarding no" in new1)
# 幂等：再套一次不应产生重复块
new2 = US.render_sshd_policy(new1, restrict=True)
check("render_sshd_policy 幂等（无重复块）", new1 == new2)
# 关闭限制：只剩 PermitRootLogin
off = US.render_sshd_policy(base, restrict=False)
check("render_sshd_policy 关闭限制无 AllowGroups", "AllowGroups" not in off)
p = US.parse_sshd_policy(new1)
check("parse_sshd_policy restrict=True", p["restrict"] is True and "pb-ssh" in p["allow_groups"])
check("parse_sshd_policy permit_root=no", p["permit_root_login"] == "no")

# ---------- users.validate_username ----------
check("validate root 拒绝", US.validate_username("root") is False)
check("validate 合法小写通过", US.validate_username("alice") is True)
check("validate 含大写拒绝", US.validate_username("Alice") is False)
check("validate 含空格拒绝", US.validate_username("a b") is False)
check("validate 空拒绝", US.validate_username("") is False)

# ---------- 0.2.58：挂载进共享目录 ----------
import tempfile as _tf
check("_safe_name 卷标空格->下划线", MNT._safe_name("sdb1", "My Disk") == "My_Disk")
check("_safe_name 去路径分隔", MNT._safe_name("sdb1", "weird/name\\x") == "weird_name_x")
check("_safe_name 空卷标回退设备名", MNT._safe_name("sdb1", "") == "sdb1")
check("_safe_name 长度<=40", len(MNT._safe_name("sdb1", "a" * 100)) <= 40)

# mount_usb 挂进共享目录（mock _run，不真挂载）
_call = {}
class _FakeRes:
    returncode = 0
    stdout = ""
    stderr = ""
def _fake_run(cmd):
    _call["cmd"] = cmd
    return _FakeRes()
_orig_run = MNT._run
_orig_sys = MNT.is_system_device
MNT._run = _fake_run
MNT.is_system_device = lambda n, mt=None: False
_share = _tf.mkdtemp()
ok, mp = MNT.mount_usb("sdb1", owner="tempest32", group="pb-disk",
                       share_root=_share, label="MyUSB", fstype="exfat")
MNT._run = _orig_run
MNT.is_system_device = _orig_sys
check("mount_usb 挂进共享目录(MyUSB)", ok and mp.startswith(_share) and mp.endswith("MyUSB"))
check("mount_usb 挂载命令含共享根", ok and _call["cmd"][-1] == mp and mp.startswith(_share))

# ---------- 汇总 ----------
n_fail = sum(1 for _, ok in PASS if not ok)
print(f"\n共 {len(PASS)} 项，失败 {n_fail} 项。")
sys.exit(1 if n_fail else 0)
