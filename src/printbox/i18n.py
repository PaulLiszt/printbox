# -*- coding: utf-8 -*-
"""PrintBox Web UI 多语言支持 / Web UI internationalization.

设计 / Design
-------------
- 模板中少数“多行/长段落”用 [[i18n:KEY]] 占位，由 STRINGS 提供 zh/en 两版（render_i18n 解析）。
  其余短标签、代码生成的动态片段、JS 内中文、API 消息，统一用 PHRASES 短语词典翻译。
- 语言选择存于 /etc/printbox/config.json 的 lang 字段，重启不丢。
- zh 为默认；当 lang != "zh" 时才做替换，zh 路径输出原文，零改动、零回归。
A few multi-line paragraphs in templates use [[i18n:KEY]] (STRINGS holds zh/en). All other short
labels, code-generated fragments, in-JS Chinese and API messages are translated via the PHRASES
phrase dictionary. Language choice lives in config.json["lang"] and survives reboots. zh is
default; substitution only happens when lang != "zh", so the zh path is untouched.
"""

DEFAULT_LANG = "zh"
SUPPORTED = ("zh", "en")

# 多行长段落：[[i18n:KEY]] -> STRINGS[lang][KEY]
STRINGS = {
    "live_jobs_desc": {
        "zh": "实时显示当前联网设备（手机 / 电脑 / 鸿蒙等）提交到本服务器的打印作业。\n  若打印机反复出现“准备打印”的咔哒声却不出纸，通常是队列里有卡住 / 重复的作业——\n  在此一键取消即可，无需登录命令行。",
        "en": "Live view of print jobs submitted by devices on the network (phones / PCs / HarmonyOS etc.).\n  If the printer keeps making a 'preparing to print' clicking sound but outputs nothing, a stuck or\n  duplicate job is usually stuck in the queue — cancel it here with one click, no command line needed.",
    },
    "add_printer_hint": {
        "zh": "将 USB 打印机接入小主机，点击“检测并添加”。系统会自动选择驱动并发布到局域网（AirPrint / IPP / Mopria）。",
        "en": "Plug the USB printer into the box and click \"Detect & Add\". The system auto-selects a driver and publishes it to the LAN (AirPrint / IPP / Mopria).",
    },
    "print_options_desc": {
        "zh": "以下为该打印机驱动支持的可设置项（不同驱动暴露的选项不同；host-based 驱动可能较少）。保存后对所有打印任务生效。",
        "en": "These are the settable options exposed by this printer's driver (different drivers expose different options; host-based drivers may expose fewer). Changes apply to all future print jobs.",
    },
    "archive_desc": {
        "zh": "每次打印的原始文件都会自动保存到本机，空间按磁盘容量自动计算；存满后自动删除最旧的文件（轮替），不会无限占用空间。",
        "en": "Every print's original file is auto-saved locally; the quota is computed from disk capacity and oldest files are rotated out automatically when full, so it never grows without bound.",
    },
    "history_desc": {
        "zh": "显示本机保存的每一次打印记录。PDF / 图片 / 文本 / 网页等常见格式可在线预览（点击「预览」会以全屏遮罩打开，同一时刻只能看一个，关闭后才能看下一个，避免同时占用资源）；其它格式（如打印机原始数据流）提供下载。也可以一键「再次打印」，把原始文件重新发给打印机。",
        "en": "Shows every saved print record. Common formats (PDF / images / text / web pages) can be previewed online (click \"Preview\" to open a full-screen overlay; only one at a time). Other formats (e.g. raw printer data) offer download. You can also \"Reprint\" with one click to resend the original file to the printer.",
    },
    "net_overview_desc": {
        "zh": "无论插网线还是用 wpa_supplicant 手动连 WiFi，本页均读取真实状态；网线（以太网）默认路由优先级始终高于 WiFi，便于故障时插网线接管。",
        "en": "This page reads the real status whether you use Ethernet or connect WiFi via wpa_supplicant; Ethernet's default route priority is always higher than WiFi, so plugging in a cable takes over on failure.",
    },
    # ---- 插件系统（Plugins）----
    "plugins_title": {"zh": "插件", "en": "Plugins"},
    "plugins_desc": {"zh": "PrintBox 不重复造轮子：native 插件复用 Debian apt 包，container 插件直接吃现成 Docker 镜像，也可一键导入 CasaOS / Umbrel / runtipi 商店定义。",
        "en": "PrintBox reuses existing ecosystems instead of reinventing: native plugins wrap Debian apt packages, container plugins run off-the-shelf Docker images, and CasaOS / Umbrel / runtipi store definitions can be imported in one click."},
    "plugins_col_name": {"zh": "名称", "en": "Name"},
    "plugins_col_type": {"zh": "类型", "en": "Type"},
    "plugins_col_state": {"zh": "状态", "en": "State"},
    "plugins_col_caps": {"zh": "所需权限", "en": "Capabilities"},
    "plugins_none": {"zh": "暂无已安装插件", "en": "No plugins installed"},
    "storage_title": {"zh": "存储共享（SMB + 时间机器）", "en": "Storage Share (SMB + Time Machine)"},
    "storage_desc": {"zh": "把 USB 移动硬盘通过 SMB 共享到局域网；开启 Time Machine 后 macOS 可直接把它当作备份目标（无需硬盘自带网络功能）。",
        "en": "Share a USB drive over SMB to the LAN; with Time Machine on, macOS can use it as a backup target directly (no network feature needed on the disk itself)."},
    "storage_path_label": {"zh": "共享目录", "en": "Share directory"},
    "storage_path_hint": {"zh": "请先挂载你的移动硬盘到该路径（如 /mnt/usb）。", "en": "Mount your external drive here first (e.g. /mnt/usb)."},
    "storage_tm_label": {"zh": "启用 Time Machine 备份", "en": "Enable Time Machine backups"},
    "storage_tm_hint": {"zh": "开启后添加 fruit VFS 模块，macOS 在「时间机器」中可见此共享为合法目标。", "en": "Adds the fruit VFS module so macOS sees this share as a valid target in Time Machine."},
    "storage_guest_label": {"zh": "允许访客只读访问", "en": "Allow guest read-only access"},
    "storage_auto_label": {"zh": "插入 USB 存储时自动挂载", "en": "Auto-mount USB storage on plug-in"},
    "storage_auto_hint": {"zh": "开启后，插入的 USB 硬盘/优盘会被自动挂载（系统盘与已挂载的会被跳过），并归 pb-disk 组供共享读写。",
        "en": "When on, plugged-in USB disks are auto-mounted (system disk and already-mounted ones are skipped) and owned by the pb-disk group for shared read/write."},
    "storage_owner_label": {"zh": "共享归属账户", "en": "Share owner"},
    "storage_owner_hint": {"zh": "SMB 强制落地账户；留空则自动取首个 pb-disk 成员。磁盘读写权限由 pb-disk 组控制。",
        "en": "The forced SMB account; blank = first pb-disk member. Disk read/write is controlled by the pb-disk group."},
    "storage_hide_host_label": {"zh": "隐藏小主机自身磁盘（关闭家目录共享）", "en": "Hide the host's own disk (disable home-directory share)"},
    "storage_hide_host_hint": {"zh": "默认开启。Samba 的 [homes] 会在登录后自动把 /home/用户名 暴露为同名共享，极易误删系统文件。开启后该共享被彻底禁用（available = no），仅保留移动硬盘的 PrintBoxShare。",
        "en": "ON by default. Samba's [homes] auto-exposes /home/<user> as a same-named share after login, risking accidental deletion of system files. When on, that share is fully disabled (available = no); only the USB PrintBoxShare remains."},
    "storage_usb_title": {"zh": "可挂载的 USB 存储", "en": "Mountable USB storage"},
    "storage_usb_hint": {"zh": "下面列出当前未挂载、且非系统盘的 USB 存储。点「挂载」即可手动挂载到共享目录 /srv/printbox-share（自动出现在 SMB 共享里）。",
        "en": "USB storage that is not mounted and not the system disk is listed below. Click \"Mount\" to mount it under the shared directory /srv/printbox-share (it then appears in the SMB share)."},
    "storage_mounted_title": {"zh": "已挂载的 USB 共享", "en": "Mounted USB shares"},
    "storage_save": {"zh": "保存并重载", "en": "Save & reload"},
    "ha_title": {"zh": "智能家居（Home Assistant）", "en": "Smart Home (Home Assistant)"},
    "ha_desc": {"zh": "直接复用 Home Assistant 官方容器——它本身就是一个插件平台，等于白送你上千个设备集成，无需自己写任何智能家居逻辑。",
        "en": "Reuses the official Home Assistant container as-is: it is itself a plugin platform, giving you thousands of device integrations without writing any smart-home logic."},
    "ha_open_label": {"zh": "打开管理界面", "en": "Open the dashboard"},
    "ha_hint": {"zh": "容器由 PrintBox 经 systemd 托管（开机自启 / 随 PrintBox 启停）。首次启动后约 1 分钟可在浏览器完成初始化。",
        "en": "The container is managed by PrintBox via systemd (auto-start on boot). Allow ~1 minute after first start before opening the setup page."},
    # ---- 第三方插件导入 ----
    "plugins_import_title": {"zh": "安装第三方插件", "en": "Install a third-party plugin"},
    "plugins_import_desc": {"zh": "粘贴一个 docker-compose.yml 的直链（CasaOS / Umbrel / runtipi 商店定义也行），PrintBox 会自动转成本地插件并放到 /etc/printbox/plugins.d。启用前仍需确认权限。",
        "en": "Paste a direct link to a docker-compose.yml (CasaOS / Umbrel / runtipi store definitions work too); PrintBox converts it into a local plugin under /etc/printbox/plugins.d. You still confirm capabilities before enabling."},
    "plugins_import_url_label": {"zh": "compose 地址", "en": "Compose URL"},
    "plugins_import_url_hint": {"zh": "例如 https://raw.githubusercontent.com/.../docker-compose.yml（须是可直接下载的 raw 文本）。",
        "en": "e.g. https://raw.githubusercontent.com/.../docker-compose.yml (must be directly downloadable raw text)."},
    "plugins_import_meta_label": {"zh": "元数据地址（可选）", "en": "Metadata URL (optional)"},
    "plugins_import_name_label": {"zh": "插件名（可选，留空自动推断）", "en": "Plugin name (optional, auto-detected if blank)"},
    "plugins_import_name_ph": {"zh": "如 my-app", "en": "e.g. my-app"},
    "plugins_import_btn": {"zh": "导入插件", "en": "Import plugin"},
    # ---- 系统账户管理（Users）----
    "users_title": {"zh": "系统账户", "en": "System Accounts"},
    "users_desc": {"zh": "管理系统账户：新建、删除、改密码、调整权限（远程/SSH、磁盘读写、系统管理）。root 受保护，不可通过此页改动。",
        "en": "Manage system accounts: create, delete, reset password, adjust permissions (remote/SSH, disk read-write, system admin). root is protected and cannot be changed here."},
    "users_col_name": {"zh": "账户", "en": "Account"},
    "users_col_perms": {"zh": "权限", "en": "Permissions"},
    "users_col_shell": {"zh": "登录 Shell", "en": "Login shell"},
    "users_col_action": {"zh": "操作", "en": "Action"},
    "users_create_title": {"zh": "新建账户", "en": "Create account"},
    "users_username_label": {"zh": "账户名", "en": "Username"},
    "users_username_ph": {"zh": "小写字母/数字/+-_，不能叫 root", "en": "lowercase letters/digits/+-_, not root"},
    "users_password_label": {"zh": "密码", "en": "Password"},
    "users_password_ph": {"zh": "设置该账户的登录密码", "en": "Set this account's login password"},
    "users_perm_ssh": {"zh": "远程 / SSH 访问", "en": "Remote / SSH access"},
    "users_perm_disk": {"zh": "磁盘读写（USB 共享）", "en": "Disk read/write (USB share)"},
    "users_perm_admin": {"zh": "系统管理", "en": "System admin"},
    "users_create_btn": {"zh": "创建账户", "en": "Create account"},
    "users_del_btn": {"zh": "删除", "en": "Delete"},
    "users_lock_btn": {"zh": "锁定", "en": "Lock"},
    "users_unlock_btn": {"zh": "解锁", "en": "Unlock"},
    "users_setpass_label": {"zh": "重置密码", "en": "Reset password"},
    "users_ssh_title": {"zh": "SSH 访问控制", "en": "SSH access control"},
    "users_ssh_desc": {"zh": "开启后，仅 pb-ssh 组成员可通过 SSH 登录；开启前会把当前已登录/特权用户加入该组，避免把自己锁在门外。",
        "en": "When enabled, only members of pb-ssh may log in via SSH; before enabling, current logged-in/privileged users are added to pb-ssh to avoid locking yourself out."},
    "users_ssh_enable": {"zh": "启用 AllowGroups 限制", "en": "Enable AllowGroups restriction"},
    "users_ssh_apply": {"zh": "应用 SSH 策略", "en": "Apply SSH policy"},
    "users_root_protected": {"zh": "root 受保护，不可通过此页改动", "en": "root is protected and cannot be changed here"},
    "users_root_account": {"zh": "（root，受保护）", "en": "(root, protected)"},
    "users_confirm_del": {"zh": "确认删除该账户？此操作不可撤销。", "en": "Delete this account? This cannot be undone."},
}


def t(key, lang=DEFAULT_LANG):
    entry = STRINGS.get(key)
    if not entry:
        return key
    return entry.get(lang, entry.get(DEFAULT_LANG, key))


def render_i18n(text, lang=DEFAULT_LANG):
    """解析文本中的 [[i18n:KEY]] 占位为对应语言文案。"""
    import re
    if "[[i18n:" not in text:
        return text
    pat = re.compile(r"\[\[i18n:([a-zA-Z0-9_]+)\]\]")

    def _rep(m):
        return t(m.group(1), lang)
    return pat.sub(_rep, text)


# 短语词典：中文短语 -> 英文（仅 en 生效）。按中文长度降序替换，避免短词误伤长词。
PHRASES = [
    # 导航 / 页脚
    ("PrintBox 打印服务器", "PrintBox Print Server"),
    ("无头打印服务器", "Headless Print Server"),
    ("关于 PrintBox", "About PrintBox"),
    ("功能特性", "Features"),
    ("版本与更新日志", "Version & Changelog"),
    ("项目主页", "Project Homepage"),
    ("发布/下载", "Releases / Download"),
    ("（暂无）", "(none)"),
    # 概览
    ("系统状态", "System Status"),
    ("访问网址", "Access URL"),
    ("IP 地址", "IP Address"),
    ("CUPS 打印服务", "CUPS Printing Service"),
    ("管理打印机", "Manage Printers"),
    ("实时打印任务", "Live Print Jobs"),
    ("重启打印服务", "Restart Printing Service"),
    ("作业号", "Job ID"),
    ("提交时间", "Submitted"),
    ("无打印机，或 CUPS 未运行", "No printers, or CUPS not running"),
    # 打印机
    ("已配置打印机", "Configured Printers"),
    ("打印机发现状态", "Printer Discovery Status"),
    ("本机 IP", "Local IP"),
    ("局域网内打印机地址（IPP）", "Printer Addresses on LAN (IPP)"),
    ("重新发布到局域网 (AirPrint/mDNS)", "Re-publish to LAN (AirPrint/mDNS)"),
    ("重新检测打印机", "Re-detect Printers"),
    ("本机 mDNS 自检输出", "Local mDNS Self-check Output"),
    ("各系统如何添加这台打印机", "How to Add This Printer on Each OS"),
    ("打印机详情与设置", "Printer Details & Settings"),
    ("默认打印机", "Default Printer"),
    ("打印选项", "Print Options"),
    ("检测并添加", "Detect & Add"),
    ("打印测试页", "Print Test Page"),
    ("设为默认", "Set as Default"),
    # 历史 / 归档
    ("打印文件归档", "Print Job Archive"),
    ("归档状态", "Archive Status"),
    ("归档目录", "Archive Directory"),
    ("配额上限", "Quota Limit"),
    ("已用空间", "Used Space"),
    ("已保存文件数", "Saved Files"),
    ("最近一次", "Last Saved"),
    ("归档设置", "Archive Settings"),
    ("开启打印文件归档", "Enable Print Archive"),
    ("占用磁盘比例（0.05~0.5，例如 0.25 = 用 25% 磁盘空间存打印文件）",
     "Disk usage ratio (0.05~0.5, e.g. 0.25 = use 25% of disk for print files)"),
    ("最小配额（MB，磁盘过小时也至少保留这么多）", "Minimum quota (MB, reserved even on small disks)"),
    ("最大配额（MB，封顶，默认 30720 = 30GB）", "Maximum quota (MB, cap; default 30720 = 30GB)"),
    ("保存并应用", "Save & Apply"),
    ("清空归档", "Clear Archive"),
    ("打印历史", "Print History"),
    ("文件名", "File Name"),
    ("格式", "Format"),
    ("预览（关闭后可看下一个）", "Preview (close to view next)"),
    ("再次打印", "Reprint"),
    ("下载", "Download"),
    ("共 ", "Total "),
    (" 条记录（每 5 秒自动刷新）", " records (auto-refresh every 5s)"),
    # 系统
    ("硬件使用情况", "Hardware Usage"),
    ("联网概览", "Network Overview"),
    ("当前上网网卡", "Active Network Interface"),
    ("网络参数", "Network Parameters"),
    ("默认网关", "Default Gateway"),
    ("DNS 服务器", "DNS Servers"),
    ("网络设置（默认 DHCP，可改为静态）", "Network Settings (DHCP by default, can be static)"),
    ("网卡详情", "Interface Details"),
    ("监听端口与联网进程", "Listening Ports & Network Processes"),
    ("端口说明：80=Web 管理、631=CUPS/IPP 打印、5353=mDNS、22=SSH、53=DNS、3702=WS-Discovery 发现。",
     "Port legend: 80=Web admin, 631=CUPS/IPP printing, 5353=mDNS, 22=SSH, 53=DNS, 3702=WS-Discovery."),
    ("Web 管理界面登录", "Web UI Login"),
    ("管理员用户名", "Admin Username"),
    ("新密码（留空表示不修改）", "New Password (leave blank to keep)"),
    ("是否要求登录", "Require Login"),
    ("要求登录", "Require Login"),
    ("不要求（仅局域网）", "No (LAN only)"),
    ("保存登录设置", "Save Login Settings"),
    ("运行日志（全部组件合并）", "Runtime Logs (all components merged)"),
    ("导出全部日志（文本文件）", "Export All Logs (text file)"),
    ("系统进程（按 CPU 占用 Top）", "System Processes (by CPU)"),
    ("登录用户", "Logged-in Users"),
    # 状态徽章 / 动态片段
    ("运行中", "Running"),
    ("未运行", "Not Running"),
    ("已开启", "Enabled"),
    ("已关闭", "Disabled"),
    ("<span class='badge ok'>运行中 ✓</span>", "<span class='badge ok'>Running ✓</span>"),
    ("<span class='badge warn'>未运行（Windows 自动发现不可用，可用 IPP 地址手动添加）</span>",
     "<span class='badge warn'>Not running (Windows auto-discovery unavailable; add manually via IPP address)</span>"),
    # 短标签（放最后，避免误伤长词）
    ("概览", "Overview"),
    ("打印机", "Printers"),
    ("历史", "History"),
    ("系统", "System"),
    ("账户", "Accounts"),
    ("关于", "About"),
    ("新建账户", "Create account"),
    ("删除", "Delete"),
    ("锁定", "Lock"),
    ("解锁", "Unlock"),
    ("重置密码", "Reset password"),
    ("SSH 访问控制", "SSH access control"),
    ("root 受保护，不可通过此页改动", "root is protected and cannot be changed here"),
    ("root 受保护，不可删除", "root is protected and cannot be deleted"),
    ("确认删除该账户？此操作不可撤销。", "Delete this account? This cannot be undone."),
    ("主机名", "Hostname"),
    ("用户", "User"),
    ("文档", "Document"),
    ("大小", "Size"),
    ("操作", "Action"),
    ("预览", "Preview"),
    ("更新", "Update"),
    ("电源", "Power"),
    ("时间", "Time"),
    ("WiFi", "WiFi"),
    ("AirPrint / mDNS", "AirPrint / mDNS"),
    ("WS-Discovery", "WS-Discovery"),
    # JS 内联
    ("请输入 SSID", "Please enter SSID"),
    # API 动作消息
    ("检测完成，已添加打印机：", "Detection done, printer added: "),
    ("未检测到新打印机", "No new printer detected"),
    ("已重新发布到局域网", "Re-published to LAN"),
    ("正在重启打印服务…", "Restarting printing service…"),
    ("打印服务已重启", "Printing service restarted"),
    ("已取消作业", "Job cancelled"),
    ("已清空全部打印任务", "All print jobs cleared"),
    ("归档已清空", "Archive cleared"),
    ("归档设置已保存", "Archive settings saved"),
    ("登录设置已保存", "Login settings saved"),
    ("语言已切换为", "Language switched to "),
    ("已连接 WiFi：", "Connected to WiFi: "),
    ("WiFi 连接失败", "WiFi connection failed"),
    ("已保存网络配置", "Network configuration saved"),
    ("已删除已保存网络", "Saved network removed"),
    ("已启用网络", "Network enabled"),
    ("已禁用网络", "Network disabled"),
    ("操作成功", "Operation succeeded"),
    ("操作失败", "Operation failed"),
    ("请先登录", "Please log in first"),
    # 模板/脚本里残留的短中文（按钮、placeholder、JS 注释）
    ("关闭", "Close"),
    ("留空则不修改", "leave blank to keep"),
    ("WiFi SSID", "WiFi SSID"),
    ("已合并 web / printer / network / drivers / archive / init 全部日志（保存在 /var/log/printbox，断电不丢），无需切换类型。",
     "Merged web/printer/network/drivers/archive/init logs (saved in /var/log/printbox, survive power loss); no type switching needed."),
    ("WiFi 扫描较重，仅刷新", "WiFi scan is heavy; only refresh"),
    ("已保存/附近网络", "saved/nearby networks"),
    ("展示列表（不动连接表单，避免清掉正在输入的密码）", "the list (don't touch the connect form, to avoid wiping a password being typed)"),
    ("切换网卡下拉时，从 /api/net_iface 拉取该网卡真实配置回填表单", "when switching NIC dropdown, fetch the NIC's real config from /api/net_iface to refill the form"),
    ("确保网络设置表单与当前网卡一致", "ensure the network settings form matches the current NIC"),
    ("轻量：硬件/进程/用户/监听端口", "lightweight: hardware/processes/users/listening ports"),
    ("较重：WiFi 扫描，30s 一次避免频繁扫描", "heavier: WiFi scan, every 30s to avoid frequent scans"),
]


def _sorted_phrases():
    return sorted(PHRASES, key=lambda kv: len(kv[0]), reverse=True)


def translate(text, lang=DEFAULT_LANG):
    """对文本做中文短语->英文替换；zh 或非法语言直接原样返回。"""
    if lang == DEFAULT_LANG or lang not in SUPPORTED:
        return text
    s = str(text)
    for zh, en in _sorted_phrases():
        if zh in s:
            s = s.replace(zh, en)
    return s


def translate_obj(obj, lang=DEFAULT_LANG):
    """递归翻译 dict / list / str 中的字符串。"""
    if lang == DEFAULT_LANG or lang not in SUPPORTED:
        return obj
    if isinstance(obj, str):
        return translate(obj, lang)
    if isinstance(obj, list):
        return [translate_obj(x, lang) for x in obj]
    if isinstance(obj, dict):
        return {k: translate_obj(v, lang) for k, v in obj.items()}
    return obj


# API 消息与通用文本共用同一短语词典
translate_msg = translate


def set_lang(lang):
    """校验并写入 config.json 的 lang 字段；成功返回 True。"""
    if lang not in SUPPORTED:
        return False
    try:
        from . import config as C
        cfg = C.load_config()
        cfg["lang"] = lang
        C.save_config(cfg)
        return True
    except Exception:
        return False
