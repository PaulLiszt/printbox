"""PrintBox Web 管理界面（纯标准库，无第三方依赖）。

监听 :80，提供状态/打印机/归档/打印历史/设置/日志页面。
局域网访问：http://<本机主机名>.local  （或路由器分配的 IP）
"""
import os
import socket
import sys
import base64
import json
import html
import urllib.parse
import shutil
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config as C
from . import archive as A
from . import network as NS
from . import printers as PS
from . import wifi as WIFI


def _wifi_backend_note():
    """WiFi 后端提示：区分『NetworkManager 未安装』与『已安装但被本系统禁用』。

    本系统部署时主动禁用 NM（managed=false + 停服务）改用 wpa_supplicant，故 nmcli 仍存在但
    has_nm() 为 False；提示应如实说明，避免误导用户以为需要重装。"""
    if shutil.which("nmcli") is None:
        return ('<p><span class="badge warn">未安装 NetworkManager，当前由 wpa_supplicant 管理 WiFi'
                '（仅影响无线设置界面，不动已连的 WiFi / 以太网 / SSH）。</span></p>')
    return ('<p><span class="badge warn">NetworkManager 已安装但被本系统主动禁用，当前由 wpa_supplicant '
            '管理 WiFi（仅影响无线设置界面，不动已连的 WiFi / 以太网 / SSH）。如需用 NM 统一管理可另行启用。</span></p>')
from . import sysinfo as SYS
from . import printopts as PO
from . import about as AB
from . import discovery as DIS
from . import printqueue as PQ
from . import __version__
from . import i18n as I18N

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
TPL = os.path.join(WEB, "templates")
STATIC = os.path.join(WEB, "static")

PORT = 80


def self_url():
    """展示本机访问地址（IP 优先，回退主机名 .local）。"""
    import socket
    try:
        h = socket.gethostname()
    except Exception:
        h = "printbox"
    ip = ""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    parts = []
    if ip:
        parts.append(f"http://{ip}")
    parts.append(f"http://{h}.local")
    # 品牌别名 printbox.local 由 printbox-mdns-alias.service 经 avahi 发布，固定可用，一并展示
    if h != "printbox":
        parts.append("http://printbox.local")
    return "  ·  ".join(parts)


# ---------- 工具 ----------
def load_cfg():
    return C.load_config()


def token_replace(tpl, tokens):
    out = tpl
    for k, v in tokens.items():
        out = out.replace("[[" + k + "]]", str(v))
    import re
    out = re.sub(r"\[\[[a-zA-Z0-9_]+\]\]", "", out)
    return out


def render(tpl_name, tokens, lang=I18N.DEFAULT_LANG):
    with open(os.path.join(TPL, tpl_name), "r", encoding="utf-8") as f:
        frag = f.read()
    frag = token_replace(frag, tokens)
    active = tokens.get("active", "")
    nav = {k: ' class="active"' if active == k else "" for k in
           ("home", "printers", "archive", "system", "settings", "about")}
    page = PAGE.replace("[[TITLE]]", tokens.get("title", "PrintBox")) \
               .replace("[[BODY]]", frag) \
               .replace("[[SELF_URL]]", self_url()) \
               .replace("[[NAV_HOME]]", nav["home"]) \
               .replace("[[NAV_PRINTERS]]", nav["printers"]) \
               .replace("[[NAV_ARCHIVE]]", nav["archive"]) \
               .replace("[[NAV_SYSTEM]]", nav["system"]) \
               .replace("[[NAV_SETTINGS]]", nav["settings"]) \
               .replace("[[NAV_ABOUT]]", nav["about"])
    # 多语言：先解析模板里的 [[i18n:KEY]]（长段落），再短语替换其余中文标签
    page = I18N.render_i18n(page, lang)
    if lang != I18N.DEFAULT_LANG:
        page = I18N.translate(page, lang)
        page = page.replace('<html lang="zh-CN">', '<html lang="en">')
    return page


def status_tokens():
    cfg = load_cfg()
    ns = NS.status()
    ips = []
    for ifc in ns.get("interfaces", []):
        ips.extend(ifc["addresses"])
    ip = ips[0] if ips else "?"
    host = ns.get("hostname") or cfg.get("hostname") or "printbox"
    rc, _, _ = C.run(["systemctl", "is-active", "--quiet", "cups"])
    cups_state = "运行中" if rc == 0 else "未运行"
    cups_badge = "ok" if rc == 0 else "err"
    try:
        printers = PS.list_printers()
    except Exception:
        printers = []
    try:
        airprint_ok = PS.airprint_visible() if printers else False
    except Exception:
        airprint_ok = False
    if printers:
        rows = []
        for p in printers:
            try:
                meta = PO.get_printer_meta(p)
            except Exception:
                meta = {}
            if meta.get("uri"):
                if meta.get("shared") and airprint_ok:
                    badge = "<span class='badge ok'>就绪 · 已发布</span>"
                elif not meta.get("shared"):
                    badge = "<span class='badge warn'>就绪 · 未共享</span>"
                else:
                    badge = "<span class='badge warn'>就绪 · 未发布到局域网</span>"
            else:
                badge = "<span class='badge err'>未配置 URI（无法打印）</span>"
            rows.append(f"<tr><td>{html.escape(p)}</td><td>{badge}</td></tr>")
        p_html = ("<table><tr><th>名称</th><th>状态</th></tr>"
                 + "".join(rows)
                 + "</table>"
                 + "<p class='muted' style='margin-top:8px'>“已发布”= 已通过 mDNS/AirPrint 在局域网可见，"
                   "手机/电脑可发现；“未配置 URI”请到「打印机」页点「检测并添加打印机」。</p>")
    else:
        p_html = "<p class='muted'>尚未添加打印机，请到" \
                 "<a href='/printers'>管理打印机</a> 检测并添加。</p>"
    return {
        "hostname": html.escape(host),
        "url": f"http://{host}.local",
        "ip": html.escape(ip),
        "wifi_ssid": html.escape(str(ns.get("wifi", {}).get("ssid", "(未连接)"))),
        "cups_state": cups_state,
        "cups_badge": cups_badge,
        "printers": p_html,
    }


# ---------- 认证 ----------
def check_auth(handler):
    cfg = load_cfg()
    if not cfg.get("require_auth"):
        return True
    hdr = handler.headers.get("Authorization", "")
    if not hdr.startswith("Basic "):
        return False
    try:
        user, pw = base64.b64decode(hdr[6:]).decode().split(":", 1)
    except Exception:
        return False
    return user == cfg.get("admin_user") and pw == cfg.get("admin_password")


def auth_head(handler):
    handler.send_response(401)
    handler.send_header("WWW-Authenticate", 'Basic realm="PrintBox"')
    handler.end_headers()


# ---------- 处理器 ----------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, ok, msg):
        """返回 JSON 响应（供 AJAX 动作接口使用）。"""
        lang = load_cfg().get("lang", I18N.DEFAULT_LANG)
        body = json.dumps({"ok": bool(ok), "msg": I18N.translate_msg(str(msg), lang)}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            return self._do_get()
        except Exception:
            import traceback as _tb
            C.log("web", "ERROR", "GET 处理异常:\n" + _tb.format_exc())
            try:
                self._send(500, "<h1>500 内部错误</h1><p>详情见 /var/log/printbox/web.log</p>")
            except Exception:
                pass

    def _do_get(self):
        if not check_auth(self):
            return auth_head(self)
        path = urllib.parse.urlparse(self.path).path
        # 语言切换：?lang=zh|en 写入配置并重定向到干净路径（重启不丢）
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        req_lang = q.get("lang", [""])[0]
        if req_lang in I18N.SUPPORTED:
            I18N.set_lang(req_lang)
            return self._redirect(path)
        lang = load_cfg().get("lang", I18N.DEFAULT_LANG)
        if path == "/static/style.css":
            return self.serve_static()
        if path == "/api/status":
            return self._send(200, json.dumps(I18N.translate_obj(status_tokens(), lang)), "application/json")
        if path == "/api/live":
            # 动态局部刷新：只返回任务/状态片段，前端用 fetch 更新，不再整页重载 printbox.local
            s = status_tokens()
            j = self.jobs_tokens()
            data = {
                "cups_state": s["cups_state"],
                "cups_badge": s["cups_badge"],
                "printers_html": s["printers"],
                "jobs_rows": j["rows"],
                "jobs_cups_state": j["cups_state"],
                "jobs_cups_badge": j["cups_badge"],
            }
            data = I18N.translate_obj(data, lang)
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/system":
            # 系统与网络页动态局部刷新：返回硬件/进程/用户/监听端口/WiFi 片段，前端 fetch 更新
            t = self.system_tokens()
            data = {"hw": t["hw"], "proc": t["proc"], "users": t["users"],
                    "listening": t["listening"], "wifi_connect": t["wifi_connect"],
                    "wifi_saved": t["wifi_saved"], "wifi_avail": t["wifi_avail"]}
            data = I18N.translate_obj(data, lang)
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/net_iface":
            # 网络参数设置：切换网卡时返回该网卡当前配置，供前端回填表单
            q = urllib.parse.urlparse(self.path).query
            iface = urllib.parse.parse_qs(q).get("iface", [""])[0]
            cfg = NS.iface_config(iface) if iface else {}
            body = json.dumps(cfg).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/history":
            # 归档与历史页动态局部刷新：只返回历史表格片段，前端 fetch 更新，不整页重载
            h = self.history_tokens()
            data = {"rows": h["rows"], "count": h["count"]}
            data = I18N.translate_obj(data, lang)
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/log":
            # 日志导出：下载对应组件日志为文本文件
            return self.serve_log_download()
        if path == "/":
            return self._send(200, render("overview.html", {**self.overview_tokens(),
                "title": "概览", "active": "home"}, lang))
        if path == "/printers":
            return self._send(200, render("printers.html", {**self.printers_page_tokens(),
                "title": "打印机", "active": "printers"}, lang))
        if path == "/archive":
            return self._send(200, render("archive.html", {**self.archive_page_tokens(),
                "title": "历史", "active": "archive"}, lang))
        if path == "/system":
            return self._send(200, render("system.html", {**self.system_page_tokens(),
                "title": "系统", "active": "system"}, lang))
        if path == "/settings":
            # 设置与日志已并入“系统”页，直接跳转过去
            return self._redirect("/system")
        if path == "/about":
            return self._send(200, render("about.html", self.about_tokens(lang), lang))
        if path == "/archive/file":
            return self.serve_archive_file()
        # 旧路径重定向到合并后的对应页面，兼容书签
        if path in ("/jobs", "/discovery", "/printing"):
            return self._redirect("/")
        if path == "/history":
            return self._redirect("/archive")
        if path == "/logs":
            return self._redirect("/settings")
        if path in ("/network", "/wifi"):
            return self._redirect("/system")
        return self._send(200, render("overview.html", {**self.overview_tokens(),
            "title": "概览", "active": "home"}, lang))

    def do_POST(self):
        try:
            return self._do_post()
        except Exception:
            import traceback as _tb
            C.log("web", "ERROR", "POST 处理异常:\n" + _tb.format_exc())
            try:
                self._send(500, "<h1>500 内部错误</h1><p>详情见 /var/log/printbox/web.log</p>")
            except Exception:
                pass

    def _do_post(self):
        if not check_auth(self):
            return auth_head(self)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", "ignore")
        form = urllib.parse.parse_qs(raw)
        get = lambda k: form.get(k, [""])[0].strip()
        keys = list(form.keys())
        path = urllib.parse.urlparse(self.path).path
        act = get("act")

        # ---- AJAX 动作接口（返回 JSON，前端局部刷新，不整页重载）----
        if path == "/api/cancel":
            ok, msg = PQ.cancel_job(get("job"))
            return self._json(ok, msg)
        if path == "/api/cancel_all":
            ok, msg = PQ.cancel_all(get("printer") or None)
            return self._json(ok, msg)
        if path == "/api/restart_cups":
            ok, msg = PQ.restart_cups()
            return self._json(ok, msg)
        if path == "/api/testpage":
            self.testpage()
            return self._json(True, "已发送测试页到默认打印机")
        if path == "/api/republish":
            try:
                lines = DIS.republish_all()
                return self._json(True, "；".join(lines))
            except Exception as e:
                return self._json(False, f"重新发布失败: {e}")
        if path == "/api/detect":
            try:
                PS.main()
                return self._json(True, "已检测并添加打印机，请查看日志")
            except Exception as e:
                return self._json(False, f"检测失败: {e}")
        if path == "/api/power":
            if act == "reboot":
                C.run(["systemctl", "reboot"])
                return self._json(True, "设备正在重启…")
            if act == "shutdown":
                C.run(["systemctl", "poweroff"])
                return self._json(True, "设备正在关机…")
            return self._json(False, "未知电源操作")
        if path == "/api/wifi":
            ok, msg = (False, "未知操作")
            if act == "connect":
                ok, msg = WIFI.connect(get("ssid"), get("password") or None)
            elif act == "connect_saved":
                ok, msg = WIFI.connect_saved(get("uuid"))
            elif act in ("up", "down"):
                ok, msg = WIFI.move(get("uuid"), act)
            elif act == "forget":
                ok, msg = WIFI.forget(get("uuid"))
            return self._json(ok, msg)

        # ---- 概览页（状态 / 任务 / 发现 / 快速操作）----
        if path == "/":
            notice = ""
            if act == "testpage":
                self.testpage()
                notice = "<p class='badge ok'>已发送测试页</p>"
            elif act == "reboot":
                C.run(["systemctl", "reboot"])
            elif act == "shutdown":
                C.run(["systemctl", "poweroff"])
            elif act == "cancel":
                ok, msg = PQ.cancel_job(get("job"))
                notice = f"<p class='badge {'ok' if ok else 'err'}'>{html.escape(msg)}</p>"
            elif act == "cancel_all":
                ok, msg = PQ.cancel_all(get("printer") or None)
                notice = f"<p class='badge {'ok' if ok else 'err'}'>{html.escape(msg)}</p>"
            elif act == "restart_cups":
                ok, msg = PQ.restart_cups()
                notice = f"<p class='badge {'ok' if ok else 'err'}'>{html.escape(msg)}</p>"
            elif act == "republish":
                try:
                    lines = DIS.republish_all()
                    notice = "<p class='badge ok'>" + html.escape("；".join(lines)) + "</p>"
                except Exception as e:
                    notice = "<p class='badge err'>" + html.escape(f"重新发布失败: {e}") + "</p>"
            return self._send(200, render("overview.html", {**self.overview_tokens(notice),
                "title": "概览", "active": "home"}))

        # ---- 打印机页（探测 / 测试 / 默认 / 选项 / 重发布）----
        if path == "/printers":
            notice = ""
            if act == "detect":
                PS.main()
                notice = "<p class='badge ok'>已检测并添加打印机，请查看日志</p>"
            elif act == "testpage":
                self.testpage()
                notice = "<p class='badge ok'>已发送测试页</p>"
            elif act == "setdefault":
                name = get("printer")
                ok, err = PO.set_default_printer(name)
                notice = ("<p class='badge ok'>已将 " + html.escape(name) +
                          " 设为默认打印机</p>") if ok else \
                         ("<p class='badge err'>设默认失败: " + html.escape(err) + "</p>")
            elif act == "save":
                name = get("printer")
                opts = {k[4:]: get(k) for k in keys if k.startswith("opt_")}
                res = PO.apply_options(name, opts)
                notice = ("<p class='badge ok'>已保存：" +
                          html.escape("；".join(res) or "无变更") + "</p>")
            elif act == "republish":
                PS.republish_airprint()
                notice = "<p class='badge ok'>已重新发布到局域网，手机/电脑应能在数秒内发现打印机</p>"
            return self._send(200, render("printers.html", {**self.printers_page_tokens(notice),
                "title": "打印机", "active": "printers"}))

        # ---- 归档与历史页（归档设置 / 清空 / 再次打印）----
        if path == "/archive":
            notice = ""
            if act == "reprint":
                self.reprint(get("name"))
                notice = "<p class='badge ok'>已重新发送到打印机，请查看日志</p>"
            else:
                self.apply_archive(get, keys)
                notice = "<p class='badge ok'>已保存并应用</p>"
            return self._send(200, render("archive.html", {**self.archive_page_tokens(notice),
                "title": "历史", "active": "archive"}))

        # ---- 系统与网络页（系统动作 + WiFi 动作 + 网络参数设置）----
        if path == "/system":
            notice = ""
            if act in ("apt_update", "printbox_update", "reboot", "shutdown"):
                if act == "apt_update":
                    if SYS.start_system_update():
                        notice = "<p class='badge ok'>系统升级已在后台启动，进度见下方日志（可能耗时数分钟）</p>"
                    else:
                        notice = "<p class='badge warn'>已有系统升级正在进行中</p>"
                elif act == "printbox_update":
                    if SYS.start_printbox_update():
                        notice = "<p class='badge ok'>PrintBox 升级已在后台启动（若你用本地源码部署，请改用 scp + install.sh --local，避免被 PyPI 版本覆盖）</p>"
                    else:
                        notice = "<p class='badge warn'>已有 PrintBox 升级正在进行中</p>"
                elif act == "reboot":
                    C.run(["systemctl", "reboot"])
                elif act == "shutdown":
                    C.run(["systemctl", "poweroff"])
            elif act == "net_apply":
                iface = get("iface")
                data = {
                    "method_v4": get("method_v4"), "address_v4": get("address_v4"),
                    "prefix_v4": get("prefix_v4"), "gateway_v4": get("gateway_v4"),
                    "dns_v4": get("dns_v4"),
                    "method_v6": get("method_v6"), "address_v6": get("address_v6"),
                    "prefix_v6": get("prefix_v6"), "gateway_v6": get("gateway_v6"),
                    "dns_v6": get("dns_v6"),
                }
                if not iface:
                    notice = "<p class='badge err'>未指定网卡</p>"
                else:
                    ok, msg = NS.set_iface_config(iface, data)
                    notice = (f"<p class='badge ok'>{html.escape(msg)}</p>" if ok
                              else f"<p class='badge err'>{html.escape(msg)}</p>")
            else:
                ok, msg = (False, "未知操作")
                if act == "connect":
                    ok, msg = WIFI.connect(get("ssid"), get("password") or None)
                elif act == "connect_saved":
                    ok, msg = WIFI.connect_saved(get("uuid"))
                elif act in ("up", "down"):
                    ok, msg = WIFI.move(get("uuid"), act)
                elif act == "forget":
                    ok, msg = WIFI.forget(get("uuid"))
                notice = (f"<p class='badge ok'>{html.escape(msg)}</p>" if ok
                          else f"<p class='badge err'>{html.escape(msg)}</p>")
            return self._send(200, render("system.html", {**self.system_page_tokens(notice),
                "title": "系统", "active": "system"}))

        # ---- 设置与日志页（登录设置）----
        if path == "/settings":
            cfg = load_cfg()
            if "require_auth" in keys or "admin_password" in keys or "admin_user" in keys:
                cfg["require_auth"] = (get("require_auth") == "yes")
                au = get("admin_user") or cfg.get("admin_user", "admin")
                cfg["admin_user"] = au
                ap = get("admin_password")
                if ap:
                    cfg["admin_password"] = ap
                C.save_config(cfg)
            # 设置与日志已并入“系统”页，保存后跳回系统页
            return self._redirect("/system")

        # ---- 旧 /action 兼容 ----
        if path == "/action":
            if act == "testpage":
                self.testpage()
            elif act == "reboot":
                C.run(["systemctl", "reboot"])
            elif act == "shutdown":
                C.run(["systemctl", "poweroff"])
            return self._send(200, render("overview.html", {**self.overview_tokens(),
                "title": "概览", "active": "home"}))

        return self._send(302, "", "text/plain")

    def serve_static(self):
        try:
            with open(os.path.join(STATIC, "style.css"), "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/css")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            self.send_error(404)

    def serve_log_download(self):
        """导出日志为文本文件下载（GET /api/log?c=<组件|all>&download=1）。
        c=all 时一次性合并全部组件日志，方便一键导出排查。"""
        comps = ["web", "printer", "network", "drivers", "archive", "init"]
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        c = q.get("c", [comps[0]])[0]
        if c == "all":
            chunks = []
            for comp in comps:
                logpath = os.path.join(C.LOG_DIR, f"{comp}.log")
                chunks.append(f"\n===== {comp} =====\n")
                if os.path.exists(logpath):
                    try:
                        with open(logpath, "r", encoding="utf-8", errors="replace") as f:
                            chunks.append(f.read())
                    except Exception:
                        chunks.append("(读取失败)\n")
                else:
                    chunks.append("(无日志)\n")
            data = "".join(chunks).encode("utf-8")
            fname = "printbox-all.log"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if c not in comps:
            c = comps[0]
        logpath = os.path.join(C.LOG_DIR, f"{c}.log")
        if not os.path.exists(logpath):
            return self._send(404, "日志不存在")
        try:
            with open(logpath, "rb") as f:
                data = f.read()
        except Exception:
            return self._send(500, "读取日志失败")
        fname = f"printbox-{c}.log"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------- 逻辑 ----------
    def settings_tokens(self):
        cfg = load_cfg()
        a = cfg.get("archive", {})
        usage = C.archive_usage()
        return {
            "title": "设置", "active": "settings",
            "admin_user": html.escape(cfg.get("admin_user", "admin")),
            "auth_no": "selected" if not cfg.get("require_auth") else "",
            "auth_yes": "selected" if cfg.get("require_auth") else "",
            "enabled": "checked" if a.get("enabled", True) else "",
            "enabled_text": "已开启" if a.get("enabled", True) else "已关闭",
            "dir": html.escape(usage.get("dir", "")),
            "max_size_mb": usage.get("max_size_mb", 0),
            "used_mb": usage.get("used_mb", 0),
            "count": usage.get("count", 0),
            "last_file": html.escape(usage.get("last_file", "") or "（暂无）"),
            "ratio": a.get("ratio", 0.25),
            "min_mb": a.get("min_mb", 2048),
            "cap_mb": a.get("max_mb_cap", 30720),
            "notice": "",
        }

    def apply_settings(self, get, form_keys=None):
        cfg = load_cfg()
        keys = set(form_keys or [])
        # Web 登录（仅当登录表单提交时才更新，避免误改）
        if "require_auth" in keys or "admin_password" in keys or "admin_user" in keys:
            cfg["require_auth"] = (get("require_auth") == "yes")
            au = get("admin_user") or cfg.get("admin_user", "admin")
            cfg["admin_user"] = au
            ap = get("admin_password")
            if ap:
                cfg["admin_password"] = ap
        # 归档设置（仅当归档表单提交时才更新）
        if "ratio" in keys:
            cfg.setdefault("archive", {})
            cfg["archive"]["enabled"] = (get("archive_enabled") == "on")
            try:
                cfg["archive"]["ratio"] = float(get("ratio") or 0.25)
            except ValueError:
                cfg["archive"]["ratio"] = 0.25
            try:
                cfg["archive"]["min_mb"] = int(get("min_mb") or 2048)
            except ValueError:
                cfg["archive"]["min_mb"] = 2048
            try:
                cfg["archive"]["max_mb_cap"] = int(get("cap_mb") or 30720)
            except ValueError:
                cfg["archive"]["max_mb_cap"] = 30720
            # 重新计算实际配额（按当前磁盘）
            try:
                st = os.statvfs("/")
                total = st.f_frsize * st.f_blocks
                cfg["archive"]["max_size_mb"] = C.compute_archive_quota(
                    total, cfg["archive"]["ratio"],
                    cfg["archive"]["min_mb"], cfg["archive"]["max_mb_cap"])
            except Exception:
                pass
            if get("act") == "clear":
                d = cfg["archive"].get("dir", C.ARCHIVE_DIR)
                if os.path.isdir(d):
                    for fn in os.listdir(d):
                        try:
                            os.remove(os.path.join(d, fn))
                        except OSError:
                            pass
                C.log("web", "INFO", "已清空打印归档")
        C.save_config(cfg)
        C.log("web", "INFO", "设置已通过 Web 应用")

    def testpage(self):
        printers = PS.list_printers()
        if not printers:
            C.log("web", "WARN", "无打印机，无法打印测试页")
            return
        name = printers[0]
        rc, _, _ = C.run(["lp", "-d", name, "/usr/share/cups/data/testprint"])
        if rc != 0:
            C.run(["bash", "-c", f"printf '' | lp -d {name}"])
        C.log("web", "INFO", f"已发送测试页到 {name}")

    def archive_tokens(self):
        return self.settings_tokens_archive()

    def settings_tokens_archive(self):
        cfg = load_cfg()
        a = cfg.get("archive", {})
        usage = C.archive_usage()
        return {
            "title": "打印归档", "active": "archive",
            "enabled": "checked" if a.get("enabled", True) else "",
            "enabled_text": "已开启" if a.get("enabled", True) else "已关闭",
            "dir": html.escape(usage.get("dir", "")),
            "max_size_mb": usage.get("max_size_mb", 0),
            "used_mb": usage.get("used_mb", 0),
            "count": usage.get("count", 0),
            "last_file": html.escape(usage.get("last_file", "") or "（暂无）"),
            "ratio": a.get("ratio", 0.25),
            "min_mb": a.get("min_mb", 2048),
            "cap_mb": a.get("max_mb_cap", 30720),
            "notice": "",
        }

    def apply_archive(self, get, form_keys=None):
        self.apply_settings(get, form_keys)

    def jobs_tokens(self, notice=""):
        jobs = PQ.list_jobs()
        states = PQ.printer_states()
        running = PQ.cups_running()
        cups_state = "运行中" if running else "未运行"
        cups_badge = "ok" if running else "err"
        if states:
            p_html = "<table><tr><th>打印机</th><th>状态</th></tr>"
            for name, st in states.items():
                if not st["enabled"]:
                    badge = "<span class='badge warn'>已停用</span>"
                elif st["state"] == "printing":
                    badge = "<span class='badge ok'>打印中</span>"
                elif st["state"] == "idle":
                    badge = "<span class='badge ok'>就绪</span>"
                else:
                    badge = f"<span class='badge warn'>{html.escape(st['state'])}</span>"
                p_html += f"<tr><td>{html.escape(name)}</td><td>{badge}</td></tr>"
            p_html += "</table>"
        else:
            p_html = "<p class='muted'>无打印机，或 CUPS 未运行</p>"
        if jobs:
            rows = []
            for j in jobs:
                cancel_form = (
                    f'<button class="btn" type="button" '
                    f'onclick="cancelJob(\'{html.escape(j["raw"], quote=True)}\')">取消</button>')
                rows.append(
                    f"<tr><td>{html.escape(j['printer'])}</td>"
                    f"<td>{html.escape(j['job_id'])}</td>"
                    f"<td>{html.escape(j['user'])}</td>"
                    f"<td>{html.escape(j['name'])}</td>"
                    f"<td>{html.escape(j['size_h'])}</td>"
                    f"<td>{html.escape(j['submitted'])}</td>"
                    f"<td>{cancel_form}</td></tr>")
            rows_html = "".join(rows)
        else:
            rows_html = ('<tr><td colspan="7" class="muted">'
                         '当前没有排队中的打印任务</td></tr>')
        # 取消全部：下拉始终显示，来源为已配置打印机（不再依赖是否有在跑作业，
        # 解决「实时打印任务」的打印机下拉在队列为空时消失的问题）
        all_printers = PS.list_printers()
        opts = "".join(
            f'<option value="{html.escape(n)}">{html.escape(n)}</option>'
            for n in all_printers)
        printer_select_html = (
            '<div class="row" style="gap:8px;align-items:center;margin-bottom:10px">'
            '<label style="margin:0;color:var(--muted);font-size:13px">选择打印机</label>'
            f'<select id="cancel_all_printer"><option value="">所有打印机</option>{opts}</select>'
            '</div>')
        cancel_all_html = (
            '<button class="btn" type="button" onclick="cancelAll()">'
            '取消该打印机全部任务</button>')
        return {
            "title": "打印任务", "active": "jobs",
            "cups_state": cups_state, "cups_badge": cups_badge,
            "printers": p_html,
            "rows": rows_html,
            "printer_select": printer_select_html,
            "cancel_all": cancel_all_html,
            "notice": notice,
        }

    def history_tokens(self, notice=""):
        # 打开历史页时补抓可能漏网的作业（归档守护进程偶发间隔内错过的）
        try:
            A.run_once()
        except Exception:
            pass
        jobs = C.list_archive_jobs()
        rows = []
        for j in jobs:
            at = html.escape(j["archived_at"])
            jobname = html.escape(j["job_name"] or j["primary"])
            user = html.escape(j["user"])
            fmt = html.escape(j["format"] or "未知")
            size = f'{j["total_mb"]} MB'
            q = urllib.parse.quote(j["primary"])
            previewable = j["can_preview"] or j["convertible"]
            if previewable:
                # 预览用全屏遮罩打开，同一时刻只能看一个，关闭后才能看下一个
                preview = (f'<button class="btn" type="button" '
                           f'onclick="openPreview(\'/archive/file?name={q}\')">预览</button>')
            else:
                preview = f'<a class="btn ghost" href="/archive/file?name={q}">下载</a>'
            reprint = (f'<form method="post" action="/archive" style="display:inline">'
                       f'<input type="hidden" name="act" value="reprint">'
                       f'<input type="hidden" name="name" value="{html.escape(j["primary"])}">'
                       f'<button class="btn" type="submit">再次打印</button></form>')
            rows.append(
                f"<tr><td>{at}</td><td>{jobname}</td><td>{user}</td>"
                f"<td>{fmt}</td><td>{size}</td><td>{preview}</td><td>{reprint}</td></tr>")
        rows_html = "".join(rows) if rows else (
            '<tr><td colspan="7" class="muted">暂无打印历史</td></tr>')
        return {
            "title": "打印历史", "active": "history",
            "rows": rows_html,
            "count": len(jobs),
            "notice": notice,
        }

    def _convert_to_pdf(self, fp, ext):
        """最佳努力把 ps / urf 转成 PDF 以便预览；成功返回临时 PDF 路径，失败返回 None。
        调用方负责删除返回的临时文件。urf 转换依赖主机已安装 cups-filters 的 urftopdf；
        ps 转换依赖 ghostscript (gs)。"""
        out = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False,
                                          prefix="printbox-prev-")
        out.close()
        try:
            if ext == "ps" and shutil.which("gs"):
                r = subprocess.run(
                    ["gs", "-q", "-dNOPAUSE", "-dBATCH", "-sDEVICE=pdfwrite",
                     f"-sOutputFile={out.name}", fp],
                    capture_output=True, timeout=60)
                if r.returncode == 0 and os.path.getsize(out.name) > 0:
                    return out.name
            elif ext == "urf" and shutil.which("urftopdf"):
                # cups-filters 的 urftopdf 是 CUPS 过滤器：从 stdin 读 URF，PDF 写到 stdout
                with open(fp, "rb") as fin, open(out.name, "wb") as fout:
                    r = subprocess.run(
                        ["urftopdf", "1", "printbox", "doc", "1", ""],
                        stdin=fin, stdout=fout, capture_output=True, timeout=60)
                if r.returncode == 0 and os.path.getsize(out.name) > 0:
                    return out.name
            # pwg 暂无可移植的通用转换工具，回退为下载
        except Exception:
            pass
        try:
            os.remove(out.name)
        except OSError:
            pass
        return None

    def serve_archive_file(self):
        cfg = load_cfg()
        d = cfg["archive"].get("dir", C.ARCHIVE_DIR)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        name = q.get("name", [""])[0]
        if not name or "/" in name or "\\" in name or ".." in name:
            return self.send_error(400)
        fp = os.path.normpath(os.path.join(d, name))
        nd = os.path.normpath(d)
        if not fp.startswith(nd + os.sep) or not os.path.isfile(fp):
            return self.send_error(404)
        ext = C._ext_of(name)

        IMAGE_CT = {
            "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "gif": "image/gif", "bmp": "image/bmp", "webp": "image/webp",
            "tif": "image/tiff", "tiff": "image/tiff",
        }
        ctype = None
        temp_path = None
        if ext in IMAGE_CT:
            ctype = IMAGE_CT[ext]
        elif ext == "pdf":
            ctype = "application/pdf"
        elif ext in ("txt", "html", "htm"):
            ctype = "text/plain" if ext == "txt" else "text/html"
        elif ext in C.CONVERTIBLE_EXTS:
            pdf_path = self._convert_to_pdf(fp, ext)
            if pdf_path:
                temp_path = pdf_path
                fp = pdf_path
                ctype = "application/pdf"
        if ctype is None:
            # 无法预览：作为附件下载
            ctype = "application/octet-stream"
        try:
            with open(fp, "rb") as f:
                data = f.read()
        except Exception:
            if temp_path:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            return self.send_error(404)
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass
        inline = ("inline" if (ctype == "application/pdf"
                   or ctype.startswith("image/")
                   or ctype in ("text/plain", "text/html")) else "attachment")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition",
                         f'{inline}; filename="{name}"')
        self.end_headers()
        self.wfile.write(data)

    def reprint(self, name):
        if not name or "/" in name or "\\" in name or ".." in name:
            return
        cfg = load_cfg()
        d = cfg["archive"].get("dir", C.ARCHIVE_DIR)
        fp = os.path.normpath(os.path.join(d, name))
        nd = os.path.normpath(d)
        if not fp.startswith(nd + os.sep) or not os.path.isfile(fp):
            C.log("web", "WARN", f"再次打印失败：文件不存在 {name}")
            return
        printers = PS.list_printers()
        if not printers:
            C.log("web", "WARN", "再次打印失败：无可用打印机")
            return
        # 选择目标打印机：优先「已配置 URI 且非 raw（带 PPD）」的队列，保证能正常出纸；
        # 其次退而求其次任意已配置 URI 的队列；都没有才用列表第一个。
        chosen = None
        for p in printers:
            try:
                meta = PO.get_printer_meta(p)
            except Exception:
                meta = {}
            if meta.get("uri") and not PS.is_raw_printer(p):
                chosen = p
                break
        if chosen is None:
            for p in printers:
                try:
                    meta = PO.get_printer_meta(p)
                except Exception:
                    meta = {}
                if meta.get("uri"):
                    chosen = p
                    break
        if chosen is None:
            chosen = printers[0]
        # 仅当目标队列本身为 raw（无 PPD）时才用 -o raw；PPD 队列盲加 raw 会把原文件
        # 直接丢给后端导致乱码/废页，故默认不加（移动端 URF/PWG 原文件在 PPD 队列下本就无法
        # 直接光栅化，但至少不会损坏打印机，且 PDF/PS 归档可正常重打）。
        opts = ["-o", "raw"] if PS.is_raw_printer(chosen) else []
        rc, _out, err = C.run(["lp", "-d", chosen, *opts, fp])
        if rc != 0:
            C.log("web", "ERROR", f"再次打印失败 {name}: {err}")
        else:
            C.log("web", "INFO", f"已再次打印 {name} -> {chosen}")

    def logs_tokens(self):
        """运行日志页：统一展示全部组件日志，不再分类型下拉/切换刷新。

        各组件以 `===== 组件名 =====` 分段合并，便于一站式查阅；导出按钮
        直接下载合并后的 printbox-all.log（GET /api/log?c=all&download=1）。
        """
        comps = ["web", "printer", "network", "drivers", "archive", "init"]
        parts = []
        for comp in comps:
            p = os.path.join(C.LOG_DIR, f"{comp}.log")
            parts.append(f"\n===== {comp} =====\n")
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()[-200:]
                except Exception:
                    lines = ["(读取失败)\n"]
            else:
                lines = ["(无日志)\n"]
            parts.append("".join(lines))
        text = html.escape("".join(parts))
        return {"title": "日志", "active": "logs", "log_text": text}

    # ---------- 打印设置 ----------
    def _q(self, key):
        q = urllib.parse.urlparse(self.path).query
        return urllib.parse.parse_qs(q).get(key, [""])[0]

    def printing_tokens(self):
        printers = PO.list_printers()
        default = PO.get_default_printer()
        target = self._q("p") or default or (printers[0] if printers else "")
        if not target:
            return {
                "title": "打印设置", "active": "printing",
                "printer_select": "", "meta": "", "default": "",
                "options": '<p class="muted">尚未添加打印机，请到'
                           '<a href="/printers">管理打印机</a>检测并添加。</p>',
                "republish": "", "notice": "",
            }
        # 打印机选择
        psel = "".join(
            f'<option value="{html.escape(p)}" {"selected" if p==target else ""}>'
            f'{html.escape(p)}</option>' for p in printers)
        psel_html = ('<form method="get" action="/printing"><label>选择打印机</label>'
                     f'<select name="p" onchange="this.form.submit()">{psel}</select></form>')
        meta = PO.get_printer_meta(target)
        uri_disp = meta["uri"] or "(未设置)"
        meta_html = (
            f'<div class="kvline"><span class="k">设备 URI</span><span class="v">{html.escape(uri_disp)}</span></div>'
            f'<div class="kvline"><span class="k">驱动</span><span class="v">{html.escape(meta["nickname"] or "?")}</span></div>'
            f'<div class="kvline"><span class="k">共享</span><span class="v">{"是" if meta["shared"] else "否"}</span></div>'
            f'<div class="kvline"><span class="k">默认</span><span class="v">{"是" if meta["is_default"] else "否"}</span></div>')
        if not meta["uri"]:
            meta_html += ('<p class="badge err" style="display:inline-block;margin-top:10px">'
                         '设备 URI 为空：打印作业会卡在队列里、不出纸，且无法发布到局域网。'
                         '请点上方「检测并添加打印机」重新探测并写入设备 URI。</p>')
        # 默认打印机设置
        dsel = "".join(
            f'<option value="{html.escape(p)}" {"selected" if p==default else ""}>'
            f'{html.escape(p)}</option>' for p in printers)
        default_html = ('<form method="post" action="/printing">'
                        '<input type="hidden" name="act" value="setdefault">'
                        '<label>默认打印机</label>'
                        f'<select name="printer">{dsel}</select> '
                        '<button class="btn" type="submit">设为默认</button></form>')
        # 可设置选项表单
        opts, err = PO.get_printer_options(target)
        if err:
            opts_html = f'<p class="muted">读取选项失败: {html.escape(err)}</p>'
        elif not opts:
            opts_html = '<p class="muted">该打印机驱动未暴露可设置的选项（常见于 host-based 驱动，功能由打印机自身固定）。</p>'
        else:
            rows = []
            for o in opts:
                sel = "".join(
                    f'<option value="{html.escape(c["value"])}" '
                    f'{"selected" if c["current"] else ""}>{html.escape(c["value"])}</option>'
                    for c in o["choices"])
                rows.append(
                    "<tr><td style='white-space:nowrap'>"
                    f'{html.escape(o["desc"] or o["name"])}'
                    f'<br><span class="muted">{html.escape(o["name"])}</span></td>'
                    f'<td><select name="opt_{html.escape(o["name"])}">{sel}</select></td></tr>')
            opts_html = ('<form method="post" action="/printing">'
                         '<input type="hidden" name="act" value="save">'
                         f'<input type="hidden" name="printer" value="{html.escape(target)}">'
                         '<table>' + "".join(rows) + '</table>'
                         '<p style="margin-top:12px"><button class="btn" type="submit">保存打印设置</button></p>'
                         '</form>')
        repub = ('<button class="btn ghost" type="button" onclick="republishAirprint()">'
                 '重新发布到局域网 (AirPrint/mDNS)</button>')
        return {
            "title": "打印设置", "active": "printing",
            "printer_select": psel_html,
            "meta": meta_html,
            "default": default_html,
            "options": opts_html,
            "republish": repub,
            "notice": "",
        }

    # ---------- WiFi 管理 ----------
    def _wifi_saved_html(self, saved):
        if not saved:
            return '<p class="muted">尚无已保存的 WiFi 网络。</p>'
        rows = []
        for s in saved:
            badge = '<span class="badge ok">当前</span>' if s["active"] else ""
            uuid = html.escape(s["uuid"], quote=True)
            rows.append(
                "<tr>"
                f"<td>{html.escape(s['name'])} {badge}</td>"
                f"<td>{s['priority']}</td>"
                "<td style='white-space:nowrap'>"
                f'<button class="btn tiny" type="button" onclick="wifiUp(\'{uuid}\')" title="提升优先级">↑</button> '
                f'<button class="btn tiny" type="button" onclick="wifiDown(\'{uuid}\')" title="降低优先级">↓</button> '
                f'<button class="btn tiny" type="button" onclick="wifiAction(\'connect_saved\',\'{uuid}\')">连接</button> '
                f'<button class="btn tiny danger" type="button" onclick="wifiAction(\'forget\',\'{uuid}\')">忘记</button>'
                "</td></tr>")
        return ("<table><tr><th>网络</th><th>优先级</th><th>操作</th></tr>"
                + "".join(rows) + "</table>")

    def _wifi_available_html(self, avail):
        if not avail:
            return '<p class="muted">未发现附近 WiFi（或当前无无线网卡 / 未安装 nmcli 或 iw）。</p>'
        rows = []
        for w in avail:
            ssid = html.escape(w["ssid"], quote=True)
            rows.append(
                "<tr>"
                f"<td>{html.escape(w['ssid'])}</td>"
                f"<td>{html.escape(str(w.get('signal', '')))}</td>"
                f"<td>{html.escape(str(w.get('sec', '')))}</td>"
                "<td style='white-space:nowrap'>"
                f'<button class="btn tiny" type="button" onclick="fillWifiSsid(\'{ssid}\')">连接</button>'
                "</td></tr>")
        return ("<table><tr><th>SSID</th><th>信号</th><th>加密</th><th>连接</th></tr>"
                + "".join(rows) + "</table>")

    # ---------- 网络（原 WiFi 页，重命名为“网络”）----------
    def _net_active_html(self, active, ifaces):
        iface = next((i for i in ifaces if i["name"] == active), None)
        if not active:
            return '<span class="badge err">未检测到默认路由</span> ' \
                   '<span class="muted">本机当前无默认网关（可能离线或仅链路本地）。</span>'
        kind = "以太网（网线）" if (iface and iface["type"] == "ether") else "WiFi（无线）"
        return (f'<span class="badge ok">{html.escape(active)}</span> '
                f'<span class="muted">{html.escape(kind)} · 网线优先级始终高于 WiFi</span>')

    def _net_interfaces_html(self, ifaces, active):
        if not ifaces:
            return '<p class="muted">未发现任何网卡（lo 已忽略）。</p>'
        cards = []
        for i in ifaces:
            kind = "以太网（网线）" if i["type"] == "ether" else "WiFi（无线）"
            ips = ", ".join(i["addresses"]) if i["addresses"] else "—"
            state_cls = "ok" if i["state"] == "up" else "warn"
            tag = ' <span class="badge ok">当前上网</span>' if i["name"] == active else ""
            cards.append(
                '<div class="card" style="padding:12px 14px;margin-bottom:10px">'
                f'<h2 style="font-size:15px;margin:0 0 10px">{html.escape(i["name"])} '
                f'<span class="badge {state_cls}">{html.escape(i["state"])}</span>{tag}</h2>'
                '<div class="row">'
                f'<div class="kv"><span class="k">类型</span><span class="v">{html.escape(kind)}</span></div>'
                f'<div class="kv"><span class="k">MAC 地址</span><span class="v">{html.escape(i["mac"] or "—")}</span></div>'
                f'<div class="kv"><span class="k">IP 地址</span><span class="v">{html.escape(ips)}</span></div>'
                '</div></div>')
        return "".join(cards)

    def _net_listening_html(self, svcs):
        if not svcs:
            return '<p class="muted">未发现监听端口（或未安装 ss）。</p>'
        rows = "".join(
            f"<tr><td>{html.escape(s['proto'].upper())}</td>"
            f"<td class='mono'>{html.escape(s['local'])}</td>"
            f"<td class='mono'>{html.escape(s['port'])}</td>"
            f"<td>{html.escape(s['proc'] or '—')}</td>"
            f"<td>{html.escape(s['svc'] or '—')}</td></tr>"
            for s in svcs)
        return ("<table><tr><th>协议</th><th>本地地址</th><th>端口</th>"
                "<th>进程</th><th>说明</th></tr>" + rows + "</table>")

    def network_tokens(self, notice=""):
        try:
            st = NS.status()
        except Exception:
            st = {"interfaces": [], "active": "", "dns": [], "wifi": {}}
        active = st.get("active", "")
        ifaces = st.get("interfaces", [])
        dns = st.get("dns") or []
        wifi_ssid = st.get("wifi", {}).get("ssid", "")
        try:
            has_nm = WIFI.has_nm()
            cur = WIFI.current_ssid()
            saved = WIFI.list_saved()
            avail = WIFI.list_available()
        except Exception:
            has_nm = False
            cur = saved = avail = None
        # WiFi 连接管理：NM 或 wpa_supplicant 后端自动选择（无 NM 时走 wpa_supplicant，不动以太网）
        wifi_connect = (
            '<div class="card">'
            '<h2>连接到 WiFi</h2>'
            '<form id="wifi_connect_form" onsubmit="return wifiConnect(event)">'
            '<div class="row">'
            '<input type="text" name="ssid" id="wifi_ssid_input" placeholder="WiFi 名称 (SSID)" required>'
            '<input type="password" name="password" id="wifi_password_input" placeholder="密码（开放网络留空）">'
            '<button class="btn" type="submit">连接</button>'
            '</div></form>'
            '<p class="muted">也可以从下方「附近网络」点「连接」自动填入 SSID，再填密码连接。</p>'
            + ('' if has_nm else _wifi_backend_note())
            + '</div>')
        wifi_saved = (
            '<div class="card">'
            '<h2>已保存的网络（靠前的优先连接）</h2>'
            f'{self._wifi_saved_html(saved)}'
            '</div>')
        wifi_avail = (
            '<div class="card">'
            '<h2>附近网络</h2>'
            f'{self._wifi_available_html(avail)}'
            '</div>')
        # 网络参数设置表单：默认 DHCP，可切静态手动指定 IPv4/IPv6 地址、掩码、网关、DNS
        cfg0 = NS.iface_config(active) if active else {
            "method_v4": "dhcp", "address_v4": "", "netmask_v4": "", "prefix_v4": "",
            "gateway_v4": "", "dns_v4": "", "method_v6": "auto", "address_v6": "",
            "prefix_v6": "", "gateway_v6": "", "dns_v6": ""}
        iface_opts = "".join(
            f'<option value="{html.escape(i["name"])}" {"selected" if i["name"] == active else ""}>'
            f'{html.escape(i["name"])}（{("WiFi" if i["type"] == "wifi" else "以太网")} · {i["state"]}）</option>'
            for i in ifaces) or '<option value="">（无网卡）</option>'
        v4_method = cfg0.get("method_v4", "dhcp")
        v6_method = cfg0.get("method_v6", "auto")

        def _sel(name, val, opts):
            return "".join(
                f'<option value="{v}" {"selected" if v == val else ""}>{l}</option>'
                for v, l in opts)
        net_settings_html = f'''<form method="post" action="/system" id="net_cfg_form">
  <input type="hidden" name="act" value="net_apply">
  <label>选择网卡</label>
  <select name="iface" id="net_iface_sel" onchange="netLoadIfaced()">{iface_opts}</select>
  <label>IPv4 获取方式</label>
  <select name="method_v4">
    {_sel("method_v4", v4_method, [("dhcp", "DHCP（自动获取）"), ("static", "静态（手动指定）")])}
  </select>
  <div class="row">
    <div><label>IPv4 地址</label><input type="text" name="address_v4" value="{html.escape(cfg0.get('address_v4',''))}" placeholder="192.168.1.10"></div>
    <div><label>子网前缀/掩码</label><input type="text" name="prefix_v4" value="{html.escape(str(cfg0.get('prefix_v4') or cfg0.get('netmask_v4') or ''))}" placeholder="24 或 255.255.255.0"></div>
    <div><label>网关</label><input type="text" name="gateway_v4" value="{html.escape(cfg0.get('gateway_v4',''))}" placeholder="192.168.1.1"></div>
    <div><label>DNS（空格分隔）</label><input type="text" name="dns_v4" value="{html.escape(cfg0.get('dns_v4',''))}" placeholder="8.8.8.8 1.1.1.1"></div>
  </div>
  <label>IPv6 获取方式</label>
  <select name="method_v6">
    {_sel("method_v6", v6_method, [("auto", "自动（SLAAC/auto）"), ("dhcp", "DHCPv6"), ("static", "静态"), ("none", "禁用 IPv6")])}
  </select>
  <div class="row">
    <div><label>IPv6 地址</label><input type="text" name="address_v6" value="{html.escape(cfg0.get('address_v6',''))}" placeholder="2001:db8::10"></div>
    <div><label>IPv6 前缀</label><input type="text" name="prefix_v6" value="{html.escape(str(cfg0.get('prefix_v6') or ''))}" placeholder="64"></div>
    <div><label>IPv6 网关</label><input type="text" name="gateway_v6" value="{html.escape(cfg0.get('gateway_v6',''))}" placeholder="2001:db8::1"></div>
    <div><label>IPv6 DNS</label><input type="text" name="dns_v6" value="{html.escape(cfg0.get('dns_v6',''))}" placeholder="2606:4700:4700::1111"></div>
  </div>
  <p class="warn-note">⚠ 修改网络参数会重启该网卡，可能短暂断网。建议用静态 IP 固定打印机服务器地址，使 AirPrint/mDNS 更稳定。
  若设置错误导致失联，重启小主机通常可恢复（回到 DHCP）。当前上网网卡：<b>{html.escape(active or "?")}</b>。</p>
  <p style="margin-top:12px"><button class="btn" type="submit">保存并应用网络设置</button></p>
</form>'''
        return {
            "title": "网络", "active": "network",
            "hostname": html.escape(st.get("hostname") or "?"),
            "active_html": self._net_active_html(active, ifaces),
            "interfaces_html": self._net_interfaces_html(ifaces, active),
            "gateway": html.escape(st.get("gateway") or "—"),
            "dns": html.escape(", ".join(dns)) if dns else "—",
            "wifi_ssid": html.escape(wifi_ssid) if wifi_ssid else "(未连接)",
            "listening_html": self._net_listening_html(st.get("listening", [])),
            "wifi_connect": wifi_connect,
            "wifi_saved": wifi_saved,
            "wifi_avail": wifi_avail,
            "net_settings": net_settings_html,
            "notice": notice,
        }

    # ---------- 系统 ----------
    def system_tokens(self):
        hw = SYS.hardware_usage()
        disk = SYS.disk_model()
        procs = SYS.list_processes()
        users = SYS.logged_in_users()
        n_up, _ = SYS.apt_upgradable()
        upd = SYS.update_status()
        ver = __version__
        cpu = hw["cpu"]
        mem = hw["mem"]
        disku = hw["disk"]
        temp = hw["temp"]
        disk_label = disk.get("model") or "未知"
        if disk.get("size_gb"):
            disk_label += f" （{disk['size_gb']} GB）"
        hw_html = (
            '<div class="row">'
            f'<div class="kv"><span class="k">CPU 占用</span><span class="v">{cpu if cpu is not None else "?"}%</span></div>'
            f'<div class="kv"><span class="k">内存</span><span class="v">{mem["used_mb"]}/{mem["total_mb"]} MB ({mem["pct"]}%)</span></div>'
            f'<div class="kv"><span class="k">磁盘型号</span><span class="v">{html.escape(disk_label)}</span></div>'
            f'<div class="kv"><span class="k">磁盘 /</span><span class="v">{disku["used_gb"]}/{disku["total_gb"]} GB ({disku["pct"]}%)</span></div>'
            f'<div class="kv"><span class="k">温度</span><span class="v">{temp if temp is not None else "?"}°C</span></div>'
            f'<div class="kv"><span class="k">开机时长</span><span class="v">{hw["uptime"]}</span></div>'
            f'<div class="kv"><span class="k">负载(1/5/15)</span><span class="v">{hw["load1"]}/{hw["load5"]}/{hw["load15"]}</span></div>'
            '</div>')
        if procs:
            proc_rows = "".join(
                f"<tr><td>{html.escape(p['pid'])}</td><td>{html.escape(p['comm'])}</td>"
                f"<td>{html.escape(str(p['cpu']))}</td><td>{html.escape(str(p['mem']))}</td>"
                f"<td>{html.escape(str(p['time']))}</td></tr>"
                for p in procs)
            proc_html = (
                "<table><tr><th>PID</th><th>进程</th><th>CPU%</th><th>内存</th>"
                "<th>运行时间</th></tr>" + proc_rows + "</table>")
        else:
            proc_html = '<p class="muted">无法读取进程信息。</p>'
        if users:
            user_rows = "".join(
                f"<tr><td>{html.escape(u['user'])}</td><td>{html.escape(u['term'])}</td>"
                f"<td>{html.escape(u['method'])}</td>"
                f"<td>{html.escape(u['host'] or '—')}</td>"
                f"<td>{html.escape(u['when'] or '—')}</td></tr>"
                for u in users)
            users_html = (
                "<table><tr><th>用户</th><th>终端</th><th>登录方式</th>"
                "<th>来源</th><th>登录时间</th></tr>" + user_rows + "</table>")
        else:
            users_html = '<p class="muted">当前没有登录用户。</p>'
        upd_html = (
            '<div class="row">'
            f'<div class="kv"><span class="k">PrintBox 版本</span><span class="v">{html.escape(ver)}</span></div>'
            f'<div class="kv"><span class="k">可用系统更新</span><span class="v">{n_up} 个</span></div>'
            '</div>'
            '<p style="margin-top:12px">'
            '<form method="post" action="/system" style="display:inline">'
            '<input type="hidden" name="act" value="apt_update">'
            '<button class="btn" type="submit">升级系统 (apt)</button></form> '
            '<form method="post" action="/system" style="display:inline">'
            '<input type="hidden" name="act" value="printbox_update">'
            '<button class="btn ghost" type="submit">升级 PrintBox</button></form>'
            '</p>'
            '<p class="muted">系统升级在后台执行，进度见下方日志。PrintBox 升级默认走 pip（若你用本地源码部署，'
            '请改用 scp + install.sh --local，避免被 PyPI 版本覆盖）。</p>'
            f'<p>更新状态：系统=<b>{upd["system"]}</b> ｜ PrintBox=<b>{upd["printbox"]}</b></p>'
            f'<pre>{html.escape(upd["log_tail"] or "(暂无)")}</pre>')
        sysact = (
            '<p style="margin-top:8px">'
            '<form method="post" action="/system" style="display:inline">'
            '<input type="hidden" name="act" value="reboot">'
            '<button class="btn ghost" type="submit">重启</button></form> '
            '<form method="post" action="/system" style="display:inline">'
            '<input type="hidden" name="act" value="shutdown">'
            '<button class="btn danger" type="submit">关机</button></form>'
            '</p>')
        # 监听端口与 WiFi 动态片段（供 /api/system 局部刷新，避免整页重载）
        nt = self.network_tokens()
        listening = nt["listening_html"]
        wifi_connect = nt["wifi_connect"]
        wifi_saved = nt["wifi_saved"]
        wifi_avail = nt["wifi_avail"]
        return {
            "title": "系统", "active": "system",
            "hw": hw_html, "proc": proc_html, "users": users_html,
            "listening": listening, "wifi_connect": wifi_connect,
            "wifi_saved": wifi_saved, "wifi_avail": wifi_avail,
            "update": upd_html, "sysact": sysact, "notice": "",
        }

    # ---------- 打印机发现 ----------
    def discovery_tokens(self, notice=""):
        try:
            d = DIS.diagnostics()
        except Exception as e:
            d = {"lan_ip": "", "airprint_seen": False, "airprint_browse": str(e),
                 "wsdd_binary": "", "wsdd_active": False, "printers": [],
                 "ipp_examples": [], "web_url": "http://<本机IP>"}
        seen = d.get("airprint_seen")
        air = ("<span class='badge ok'>已发现 ✓</span>" if seen
               else "<span class='badge err'>未发现 ✗（见下方排错）</span>")
        wsdd = ("<span class='badge ok'>运行中 ✓</span>" if d.get("wsdd_active")
                else "<span class='badge warn'>未运行（Windows 自动发现不可用，可用 IPP 地址手动添加）</span>")
        ipp = "".join(
            f"<li><code>{html.escape(u)}</code></li>" for u in d.get("ipp_examples", [])) \
            or "<li class='muted'>（暂无打印机）</li>"
        # 各系统添加指引
        guides = """
        <div class="guide"><h3>iPhone / iPad（AirPrint）</h3>
          <p>打开「照片 / 文件」→ 分享 → 打印 → 选 <b>HP LaserJet P1505n @ 本机主机名</b>。
          若列表为空：在「发现」页点「重新发布」，等约 10 秒；仍无则确认手机与打印机在同一 Wi-Fi。</p></div>
        <div class="guide"><h3>macOS（AirPrint / IPP）</h3>
          <p>系统设置 → 打印机与扫描仪 → 添加 → 左侧应出现该打印机；或「IP」页填
          <code>http://%s:631/printers/hp_laserjet_p1505n</code>，协议选「IPP」。</p></div>
        <div class="guide"><h3>Android / HarmonyOS（Mopria / IPP）</h3>
          <p>系统「设置 → 连接 → 打印」开启默认打印服务（或装 Mopria Print Service），
          应自动发现；否则用浏览器/文件管理器打开上面的 IPP 地址打印。</p></div>
        <div class="guide"><h3>Windows 10/11（WS-Discovery + IPP）</h3>
          <p>设置 → 蓝牙和其他设备 → 打印机和扫描仪 → 「添加设备」会看到 <b>PrintBox 打印服务器</b>（WSD）。
          若未出现或添加后无法打印：选「我需要的打印机不在列表中」→
          「按名称或 TCP/IP 地址选择」→ 输入 <code>http://%s:631/printers/hp_laserjet_p1505n</code>
          → 驱动选「Internet Printing Protocol」或 HP LaserJet P1505n 类驱动。</p></div>
        <div class="guide"><h3>Linux（mDNS / IPP）</h3>
          <p>多数桌面环境设置里会自动列出；或
          <code>lpadmin -p p1505 -v http://%s:631/printers/hp_laserjet_p1505n -m drv:///sample.drv/generic.ppd</code>。</p></div>
        """ % (d.get("lan_ip", "192.168.1.100"), d.get("lan_ip", "192.168.1.100"),
               d.get("lan_ip", "192.168.1.100"))
        if d.get("airprint_browse"):
            browse_html = "<pre class='log'>" + html.escape(d["airprint_browse"]) + "</pre>"
        else:
            browse_html = "<p class='muted'>（本机 avahi-browse 无输出，说明服务未注册）</p>"
        return {
            "title": "打印机发现", "active": "discovery",
            "lan_ip": html.escape(d.get("lan_ip", "")),
            "airprint_status": air,
            "wsdd_status": wsdd,
            "ipp_list": ipp,
            "browse": browse_html,
            "guides": guides,
            "notice": notice,
        }

    # ---------- 关于 ----------
    def about_tokens(self, lang=I18N.DEFAULT_LANG):
        data = AB.load_about(lang)
        ver = __version__
        feats = "".join(f"<li>{html.escape(f)}</li>" for f in data.get("features", [])) \
            or '<li class="muted">（暂无）</li>'
        rel = data.get("releases_url", "")
        home = data.get("homepage", "")
        cl_html = ""
        for e in data.get("changelog", []):
            changes = "".join(
                f"<li>{html.escape(c)}</li>" for c in e.get("changes", []))
            cl_html += (
                f'<div class="release"><h3>{html.escape(e.get("version",""))} '
                f'<span class="muted">{html.escape(e.get("date",""))}</span></h3>'
                f'<p class="muted">{html.escape(e.get("title",""))}</p><ul>{changes}</ul></div>')
        links = ""
        if home:
            links += f'<a class="btn" href="{html.escape(home)}" target="_blank">项目主页</a> '
        if rel:
            links += f'<a class="btn" href="{html.escape(rel)}" target="_blank">发布/下载</a>'
        return {
            "title": "关于 PrintBox", "active": "about",
            "tagline": html.escape(data.get("tagline", "")),
            "desc": html.escape(data.get("description", "")),
            "version": html.escape(ver),
            "features": feats,
            "changelog": cl_html,
            "links": links,
        }


    # ---------- 合并页：概览（状态 + 打印任务 + 发现）----------
    def overview_tokens(self, notice=""):
        s = status_tokens()
        j = self.jobs_tokens(notice=notice)
        return {
            "hostname": s["hostname"], "url": s["url"], "ip": s["ip"],
            "wifi_ssid": s["wifi_ssid"], "cups_state": s["cups_state"],
            "cups_badge": s["cups_badge"], "printers_html": s["printers"],
            "printer_select": j["printer_select"],
            "jobs_rows": j["rows"], "jobs_cancel_all": j["cancel_all"],
            "jobs_cups_state": j["cups_state"], "jobs_cups_badge": j["cups_badge"],
            "notice": notice,
        }

    # ---------- 合并页：打印机（打印机 + 打印设置）----------
    def printers_page_tokens(self, notice=""):
        p = self.printing_tokens()
        s = status_tokens()
        d = self.discovery_tokens(notice="")
        detect_form = (
            '<div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:12px">'
            '<button class="btn" type="button" onclick="detectPrinter()">检测并添加打印机</button>'
            '<button class="btn ghost" type="button" onclick="testpage()">打印测试页（默认）</button>'
            '</div>')
        return {
            "printers_list": s["printers"],
            "printers_actions": detect_form,
            "printer_select": p["printer_select"],
            "meta": p["meta"],
            "default": p["default"],
            "options": p["options"],
            "republish": p["republish"],
            "disc_lan_ip": d["lan_ip"], "disc_airprint_status": d["airprint_status"],
            "disc_wsdd_status": d["wsdd_status"], "disc_ipp_list": d["ipp_list"],
            "disc_browse": d["browse"], "disc_guides": d["guides"],
            "notice": notice,
        }

    # ---------- 合并页：归档与历史（归档 + 打印历史）----------
    def archive_page_tokens(self, notice=""):
        a = self.settings_tokens_archive()
        h = self.history_tokens(notice=notice)
        return {
            "enabled_text": a["enabled_text"], "dir": a["dir"],
            "max_size_mb": a["max_size_mb"], "used_mb": a["used_mb"],
            "count": a["count"], "last_file": a["last_file"],
            "enabled": a["enabled"], "ratio": a["ratio"],
            "min_mb": a["min_mb"], "cap_mb": a["cap_mb"],
            "hist_rows": h["rows"], "hist_count": h["count"], "notice": notice,
        }

    # ---------- 合并页：系统（系统 + 网络 + 设置 + 日志）----------
    def system_page_tokens(self, notice=""):
        s = self.system_tokens()
        n = self.network_tokens(notice=notice)
        st = self.settings_tokens()
        lg = self.logs_tokens()
        merged = {}
        merged.update(s)
        merged.update(n)
        merged.update(st)
        merged["log_text"] = lg["log_text"]
        merged["notice"] = notice
        return merged

    # ---------- 合并页：设置与日志（设置 + 日志）----------
    def settings_page_tokens(self, notice=""):
        st = self.settings_tokens()
        lg = self.logs_tokens()
        return {
            "admin_user": st["admin_user"], "auth_no": st["auth_no"],
            "auth_yes": st["auth_yes"],
            "log_text": lg["log_text"],
            "notice": notice,
        }


PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>[[TITLE]] - PrintBox</title>
<link rel="stylesheet" href="/static/style.css"></head>
<body>
<header><h1>PrintBox 打印服务器</h1>
<span class="url">[[SELF_URL]]</span>
<span class="langswitch"><a href="?lang=zh">中文</a> · <a href="?lang=en">EN</a></span></header>
<nav>
  <a href="/"[[NAV_HOME]]>概览</a>
  <a href="/printers"[[NAV_PRINTERS]]>打印机</a>
  <a href="/archive"[[NAV_ARCHIVE]]>历史</a>
  <a href="/system"[[NAV_SYSTEM]]>系统</a>
  <a href="/about"[[NAV_ABOUT]]>关于</a>
</nav>
<main>[[BODY]]</main>
<div class="footer">PrintBox · 无头打印服务器</div>
</body></html>"""


class DualStackHTTPServer(ThreadingHTTPServer):
    """双栈 HTTP 服务：同一套接字同时接受 IPv4 与 IPv6。

    printbox.local 经 mDNS 可能解析到 IPv6 地址；若只绑 0.0.0.0（仅 IPv4），
    浏览器解析到 IPv6 时会『拒绝连接』。绑定 :: 并关闭 IPV6_V6ONLY 后，
    同一监听既收 IPv4 也收 IPv6，根除该问题。"""
    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except Exception:
            pass
        super().server_bind()


def main():
    C.ensure_dirs()
    # 首启若未配置则跑一次打印机探测（含驱动自动安装）
    if not PS.list_printers():
        try:
            PS.main()
        except Exception as e:
            C.log("web", "WARN", f"首启打印机探测异常: {e}")
    # 后台自动维护局域网发布：若 AirPrint 不可见则自动重新发布（不阻塞 Web 启动）。
    # 即便 repair / 定时器未跑，服务一起动就会自愈一次，避免“手机/电脑搜不到”长期存在。
    try:
        import threading
        t = threading.Thread(target=PS.self_heal_publish, daemon=True)
        t.start()
    except Exception:
        pass
    srv = DualStackHTTPServer(("::", PORT), Handler)
    C.log("web", "INFO", f"Web 管理界面已启动 :{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
