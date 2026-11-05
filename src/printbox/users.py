# -*- coding: utf-8 -*-
"""PrintBox 系统账户管理：真实 Linux 账户 + 权限组（pb-ssh / pb-disk / pb-admin）。

设计要点 / Design
----------------
- 账户是真实的 Linux 系统账户（useradd / usermod / userdel / passwd），由 PrintBox 直接管理，
  与系统完全一致；root 受保护，绝不通过 WebUI 改动（删除/改密码/改权限都不行）。
- 权限用 Linux 组实现，三个语义组：
    pb-ssh   远程/SSH 访问      （加入后才能通过 SSH 登录）
    pb-disk  磁盘读写            （USB 自动挂载的共享目录归该组，SMB 共享也按该组鉴权）
    pb-admin 系统管理            （预留：未来用于区分可进「账户/系统」管理页的账户）
- SSH 管控带护栏：改写 sshd_config 前先备份；强制策略用「管理块 + 注释标记」隔离，
  不破坏用户其它配置；启用 AllowGroups 前，把当前已登录/在 sudo 组的用户纳入 pb-ssh，
  避免把自己锁在门外（真实盒 tempest32 就是普通用户 SSH + su - 拿 root）。
- 纯函数（render_sshd_policy / build_user_row / group_permission_map 等）与系统调用分离，
  方便单测；薄封装函数再做实际读写。
"""
import os
import shutil
import subprocess
import time

try:
    from . import config as C
except Exception:  # 独立单元测试时 printbox 可能不在 path
    C = None

# ---- 受保护账户：绝不通过 WebUI 改动 ----
PROTECTED_USERS = ("root",)

# ---- 语义权限组 ----
GROUPS = ("pb-ssh", "pb-disk", "pb-admin")
GROUP_DESC = {
    "zh": {
        "pb-ssh": "远程 / SSH 访问",
        "pb-disk": "磁盘读写（USB 共享）",
        "pb-admin": "系统管理",
    },
    "en": {
        "pb-ssh": "Remote / SSH access",
        "pb-disk": "Disk read/write (USB share)",
        "pb-admin": "System administration",
    },
}
# 系统盘上 printbox 自身可能以该用户运行，建账户时不应误伤
SYSTEM_UID_MIN = 1000

SSHD_CONFIG = "/etc/ssh/sshd_config"
SSHD_BAK_PREFIX = SSHD_CONFIG + ".printbox.bak."
SSHD_TMP = SSHD_CONFIG + ".printbox.tmp"
MARKER_BEGIN = "# ===== PrintBox managed SSH policy ====="
MARKER_END = "# ===== End PrintBox managed SSH policy ====="

DEFAULT_SHELL = "/bin/bash"


# ====================== 纯函数（可单测） ======================

def render_sshd_policy(text, allow_groups=("pb-ssh",), permit_root_login="no",
                       restrict=True):
    """纯函数：在 sshd_config 文本中替换 PrintBox 管理块。

    - 先整体删除旧的 MARKER 管理块（无论它在文件任何位置）；
    - 再在文末追加一个全新的管理块；
    - 不触碰块外的任何用户配置（AllowUsers / 端口 / 其它）。
    返回新文本。restrict=False 时只保留 PermitRootLogin，不写 AllowGroups。
    """
    out = []
    inside = False
    for ln in text.splitlines():
        s = ln.strip()
        if s == MARKER_BEGIN:
            inside = True
            continue
        if s == MARKER_END:
            inside = False
            continue
        if inside:
            continue
        out.append(ln)
    block = [MARKER_BEGIN, f"PermitRootLogin {permit_root_login}"]
    if restrict and allow_groups:
        block.append("AllowGroups " + " ".join(allow_groups))
    block.append(MARKER_END)
    body = "\n".join(out).rstrip("\n")
    return body + "\n" + "\n".join(block) + "\n"


def parse_sshd_policy(text):
    """纯函数：从 sshd_config 文本解析 PrintBox 管理块里的关键项。"""
    inside = False
    allow_groups = []
    permit_root = None
    for ln in text.splitlines():
        s = ln.strip()
        if s == MARKER_BEGIN:
            inside = True
            continue
        if s == MARKER_END:
            inside = False
            continue
        if not inside:
            continue
        if s.startswith("AllowGroups") or s.startswith("AllowUsers"):
            allow_groups = s.split()[1:]
        elif s.startswith("PermitRootLogin"):
            permit_root = s.split(None, 1)[1].strip() if len(s.split()) > 1 else ""
    return {
        "managed": inside or (permit_root is not None),
        "allow_groups": allow_groups,
        "permit_root_login": permit_root,
        "restrict": bool(allow_groups),
    }


def build_user_row(pw, groups_of_user, primary_group, locked=False):
    """纯函数：把一个 pwd 结构与「该用户所属组列表」整理成 WebUI 友好的 dict。"""
    name = pw.pw_name
    perms = {g: (g in groups_of_user) for g in GROUPS}
    return {
        "name": name,
        "uid": pw.pw_uid,
        "home": pw.pw_dir,
        "shell": pw.pw_shell,
        "primary_group": primary_group,
        "groups": list(groups_of_user),
        "perms": perms,
        "protected": name in PROTECTED_USERS,
        "system": pw.pw_uid < SYSTEM_UID_MIN,
        "locked": locked,
    }


def is_managed_user(row):
    """纯函数：判断该账户是否应出现在 PrintBox 账户管理页。

    显示：root + 所有『普通账户(uid>=1000)』 + 任何拥有至少一个 pb- 权限组的账户。
    过滤掉与打印/系统无关的海量系统账户，避免页面被几百个 daemon 账户刷屏。
    """
    if row["name"] in PROTECTED_USERS:
        return True
    if row["uid"] >= SYSTEM_UID_MIN:
        return True
    return any(row["perms"].get(g) for g in GROUPS)


def validate_username(name):
    """纯函数：校验用户名是否合法（与 useradd 的 NAME_REGEX 一致：小写字母数字 +-_.）。"""
    if not name:
        return False
    if name in PROTECTED_USERS:
        return False  # 不允许新建/改 root
    if len(name) > 32:
        return False
    import re
    return bool(re.fullmatch(r"[a-z_][a-z0-9_-]*", name))


# ====================== 组管理 ======================

def group_exists(name):
    try:
        import grp
        grp.getgrnam(name)
        return True
    except KeyError:
        return False
    except Exception:
        return False


def user_exists(name):
    try:
        import pwd
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False
    except Exception:
        return False


def ensure_groups():
    """创建 pb-ssh / pb-disk / pb-admin 三个权限组（幂等）。"""
    for g in GROUPS:
        if not group_exists(g):
            _run(["groupadd", g])
    return True


def group_members(name):
    import grp
    try:
        return list(grp.getgrnam(name).gr_mem)
    except KeyError:
        return []
    except Exception:
        return []


# ====================== 账户增删改 ======================

def create_user(name, password, perms=None, shell=DEFAULT_SHELL):
    """新建系统账户并设密码、加权限组。

    perms: dict {组名: True/False}，只处理 GROUPS 里的键；未给则不加任何权限组。
    返回 (ok, msg)。
    """
    if not validate_username(name):
        return False, "非法用户名（只能小写字母/数字/+-_，且不能为 root）"
    if user_exists(name):
        return False, f"账户已存在: {name}"
    ensure_groups()
    r = _run(["useradd", "-m", "-s", shell, name])
    if r.returncode != 0:
        return False, f"useradd 失败: {r.stderr.strip() or r.stdout.strip()}"
    if password:
        rc = _set_password_raw(name, password)
        if not rc:
            return False, "账户已建，但设置密码失败（请手动设置）"
    perms = perms or {}
    for g in GROUPS:
        if perms.get(g):
            _run(["usermod", "-aG", g, name])
    return True, f"已创建账户 {name}"


def delete_user(name, remove_home=True):
    """删除账户。root 受保护，拒绝。返回 (ok, msg)。"""
    if name in PROTECTED_USERS:
        return False, "root 账户受保护，不可删除"
    if not user_exists(name):
        return False, f"账户不存在: {name}"
    cmd = ["userdel"]
    if remove_home:
        cmd.append("-r")
    r = _run(cmd + [name])
    if r.returncode != 0:
        return False, f"userdel 失败: {r.stderr.strip() or r.stdout.strip()}"
    _remove_samba_user(name)
    return True, f"已删除账户 {name}"


def set_password(name, password):
    """设置账户密码。root 受保护，WebUI 不会调用；CLI 可调用。"""
    if name in PROTECTED_USERS:
        return False, "root 账户受保护，不可通过此接口改密码"
    if not user_exists(name):
        return False, f"账户不存在: {name}"
    if not password:
        return False, "密码不能为空"
    if _set_password_raw(name, password):
        return True, f"已更新 {name} 的密码"
    return False, "设置密码失败"


def _set_password_raw(name, password):
    """设置 Linux 密码；并尽力把同一密码同步进 Samba 密码库（smbpasswd）。

    Samba 用独立的密码库，只改 Linux 密码不足以让账户登录 SMB 共享——
    这是 Windows 访问 SMB 报『拒绝访问』的头号原因。smbpasswd 不存在
    （未装 samba）时静默跳过，不影响 Linux 账户本身。
    """
    ok = False
    try:
        p = subprocess.run(["chpasswd"], input=f"{name}:{password}\n",
                           capture_output=True, text=True)
        ok = (p.returncode == 0)
    except Exception:
        ok = False
    _sync_samba_password(name, password)
    return ok


def _sync_samba_password(name, password):
    """尽力把密码同步进 Samba 密码库（smbpasswd）。返回是否成功（best-effort）。"""
    smbpasswd = shutil.which("smbpasswd")
    if not smbpasswd:
        return False
    try:
        # -a 新建；若已存在则 -a 报错，去掉 -a 直接更新
        p = subprocess.run([smbpasswd, "-a", "-s", name],
                           input=f"{password}\n{password}\n",
                           capture_output=True, text=True)
        if p.returncode == 0:
            return True
        p2 = subprocess.run([smbpasswd, "-s", name],
                            input=f"{password}\n{password}\n",
                            capture_output=True, text=True)
        return p2.returncode == 0
    except Exception:
        return False


def _remove_samba_user(name):
    """删除 Samba 密码库中的对应条目（best-effort）。"""
    smbpasswd = shutil.which("smbpasswd")
    if not smbpasswd:
        return
    try:
        subprocess.run([smbpasswd, "-x", name], capture_output=True, text=True)
    except Exception:
        pass


def set_perms(name, perms):
    """按 perms dict {组名: bool} 调整账户权限组归属。root 受保护。"""
    if name in PROTECTED_USERS:
        return False, "root 账户受保护，不可改权限"
    if not user_exists(name):
        return False, f"账户不存在: {name}"
    ensure_groups()
    for g in GROUPS:
        want = bool(perms.get(g))
        have = name in group_members(g)
        if want and not have:
            _run(["usermod", "-aG", g, name])
        elif not want and have:
            _run(["gpasswd", "-d", name, g])
    return True, f"已更新 {name} 的权限"


def lock_user(name):
    if name in PROTECTED_USERS:
        return False, "root 账户受保护，不可锁定"
    r = _run(["passwd", "-l", name])
    if r.returncode != 0:
        return False, f"锁定失败: {r.stderr.strip()}"
    return True, f"已锁定 {name}（密码登录停用）"


def unlock_user(name):
    if name in PROTECTED_USERS:
        return False, "root 账户受保护"
    r = _run(["passwd", "-u", name])
    if r.returncode != 0:
        return False, f"解锁失败: {r.stderr.strip()}"
    return True, f"已解锁 {name}"


# ====================== 查询 ======================

def list_users():
    """返回全部系统账户的整理后列表（含受保护/系统/普通）。"""
    import pwd, grp
    all_groups = grp.getgrall()
    locked = _locked_users()
    rows = []
    for pw in pwd.getpwall():
        mem = set()
        for g in all_groups:
            if pw.pw_name in g.gr_mem:
                mem.add(g.gr_name)
        try:
            primary = grp.getgrgid(pw.pw_gid).gr_name
        except Exception:
            primary = ""
        if primary:
            mem.add(primary)
        rows.append(build_user_row(pw, mem, primary, locked=pw.pw_name in locked))
    return rows


def _locked_users():
    """读取 /etc/shadow 判断被锁定的账户（密码字段以 ! 或 * 开头）。非 root 读取失败则返回空集合。"""
    locked = set()
    try:
        with open("/etc/shadow", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split(":", 2)
                if len(parts) < 2:
                    continue
                name, pwfield = parts[0], parts[1]
                if pwfield and (pwfield.startswith("!") or pwfield.startswith("*")):
                    locked.add(name)
    except Exception:
        pass
    return locked


def list_managed_users():
    """返回应在管理页展示的账户（root + 普通账户 + 带权限组的账户）。"""
    return [r for r in list_users() if is_managed_user(r)]


def get_user(name):
    for r in list_users():
        if r["name"] == name:
            return r
    return None


# ====================== SSH 管控（带护栏） ======================

def current_session_users():
    """返回当前『很可能已经能登录』的用户名集合，用于护栏：开 AllowGroups 前先把他们加入 pb-ssh，
    避免把自己锁在门外。

    来源：who 当前会话、运行 printbox 的 euid、sudo/wheel 组成员。剔除 root。
    """
    import pwd, grp
    names = set()
    r = _run(["who"])
    if r.returncode == 0:
        for ln in r.stdout.splitlines():
            parts = ln.split()
            if parts:
                names.add(parts[0])
    try:
        names.add(pwd.getpwuid(os.geteuid()).pw_name)
    except Exception:
        pass
    for gn in ("sudo", "wheel", "adm"):
        try:
            names.update(grp.getgrnam(gn).gr_mem)
        except KeyError:
            pass
    names.discard("root")
    return names


def add_sessions_to_ssh_group():
    """护栏：把当前会话/特权组用户加入 pb-ssh。"""
    ensure_groups()
    added = []
    for u in current_session_users():
        if not user_exists(u):
            continue
        if u not in group_members("pb-ssh"):
            r = _run(["usermod", "-aG", "pb-ssh", u])
            if r.returncode == 0:
                added.append(u)
    return added


def add_sessions_to_disk_group():
    """把当前会话/特权组用户也纳入 pb-disk，使主用户默认可访问磁盘共享。

    对应需求 3c：系统已建立的账户应能直接访问磁盘。安装时若只把用户放进 pb-ssh，
    存储共享会回退到 nobody 落地账户，导致 SMB 二次鉴权失败（拒绝访问）。
    这里把活跃用户默认加入 pb-disk；之后仍可在 /users 页逐项开关。
    """
    ensure_groups()
    added = []
    for u in current_session_users():
        if not user_exists(u):
            continue
        if u not in group_members("pb-disk"):
            r = _run(["usermod", "-aG", "pb-disk", u])
            if r.returncode == 0:
                added.append(u)
    return added


def apply_ssh_policy(restrict=True, allow_groups=("pb-ssh",),
                     permit_root_login="no"):
    """改写 sshd_config 强制策略。带护栏：先备份，开 AllowGroups 前把现有登录用户纳入 pb-ssh，
    写前用 `sshd -t` 校验，校验不过则放弃；写后 reload sshd。

    返回 (ok, msg)。
    """
    if C is None:
        return False, "config 模块不可用"
    if not os.path.exists(SSHD_CONFIG):
        return False, "sshd_config 不存在，跳过 SSH 管控"
    ensure_groups()
    # 护栏：开限制前先放行现有会话用户
    if restrict:
        added = add_sessions_to_ssh_group()
        if added:
            C.log("users", "INFO", f"SSH 护栏：已将现有用户纳入 pb-ssh 防锁门: {', '.join(added)}")
    try:
        with open(SSHD_CONFIG, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception as e:
        return False, f"读取 sshd_config 失败: {e}"
    new = render_sshd_policy(text, allow_groups=allow_groups,
                             permit_root_login=permit_root_login, restrict=restrict)
    if new == text:
        return True, "无变更"
    # 备份
    bak = SSHD_BAK_PREFIX + time.strftime("%Y%m%d-%H%M%S")
    try:
        shutil.copy2(SSHD_CONFIG, bak)
    except Exception as e:
        return False, f"备份 sshd_config 失败: {e}"
    # 校验：用临时文件跑 sshd -t
    try:
        with open(SSHD_TMP, "w", encoding="utf-8") as f:
            f.write(new)
        rc, _o, err = C.run(["sshd", "-t", "-f", SSHD_TMP])
    except Exception as e:
        _safe_remove(SSHD_TMP)
        return False, f"sshd 校验异常: {e}"
    if rc != 0:
        _safe_remove(SSHD_TMP)
        return False, f"sshd 配置校验失败，已放弃改写: {err.strip()}"
    # 落盘
    try:
        os.replace(SSHD_TMP, SSHD_CONFIG)
    except Exception as e:
        _safe_remove(SSHD_TMP)
        return False, f"写入 sshd_config 失败: {e}"
    # reload
    for svc in ("ssh", "sshd"):
        rr = _run(["systemctl", "reload", svc])
        if rr.returncode == 0:
            break
    return True, ("已应用 SSH 策略" + ("（启用 AllowGroups 限制）" if restrict else "（仅禁用 root 登录）")
                  + f"，备份: {os.path.basename(bak)}")


def ssh_policy_status():
    """读取当前 sshd 管理块状态，供 WebUI 展示开关。"""
    if not os.path.exists(SSHD_CONFIG):
        return {"managed": False, "restrict": False,
                "allow_groups": [], "permit_root_login": None}
    try:
        with open(SSHD_CONFIG, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return {"managed": False, "restrict": False,
                "allow_groups": [], "permit_root_login": None}
    return parse_sshd_policy(text)


# ====================== 工具 ======================

def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except Exception as e:
        class _R:
            returncode = -1
            stdout = ""
            stderr = str(e)
        return _R()


def _safe_remove(p):
    try:
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass
