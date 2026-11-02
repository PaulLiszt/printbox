"""插件 Web 视图：/plugins 索引页 + 无自有 handle_get 时的通用插件页。"""
import os
from . import manager as MG
from . import manifest as M
from .. import i18n as I18N
from .. import webapp


def index(handler, lang):
    rows = []
    for p in MG.list_plugins():
        disp = p["display_name"].get(lang, p["display_name"]["zh"])
        cap = ", ".join(p["required_capabilities"]) or "-"
        rows.append(
            f'<tr><td><a href="{p["route"] if p["enabled"] else "#"}">{disp}</a></td>'
            f'<td>{p["type"]}</td><td>{"已启用" if p["enabled"] else "未启用"}</td>'
            f'<td><code>{cap}</code></td></tr>')
    body = f"""
  <h2>[[i18n:plugins_title]]</h2>
  <p class="muted">[[i18n:plugins_desc]]</p>
  <table class="tbl">
    <thead><tr><th>[[i18n:plugins_col_name]]</th><th>[[i18n:plugins_col_type]]</th>
    <th>[[i18n:plugins_col_state]]</th><th>[[i18n:plugins_col_caps]]</th></tr></thead>
    <tbody>{''.join(rows) or '<tr><td colspan="4">[[i18n:plugins_none]]</td></tr>'}</tbody>
  </table>
"""
    tokens = {
        "title": "插件" if lang == "zh" else "Plugins",
        "active": "plugins",
        "BODY": body,
    }
    # 用插件索引模板（复用 PAGE 布局）
    tpl = """<h2>[[i18n:plugins_title]]</h2>
  <p class="muted">[[i18n:plugins_desc]]</p>
  <table class="tbl">
    <thead><tr><th>[[i18n:plugins_col_name]]</th><th>[[i18n:plugins_col_type]]</th>
    <th>[[i18n:plugins_col_state]]</th><th>[[i18n:plugins_col_caps]]</th></tr></thead>
    <tbody>__ROWS__</tbody>
  </table>"""
    html = tpl.replace("__ROWS__", "".join(rows) or '<tr><td colspan="4">[[i18n:plugins_none]]</td></tr>')
    return webapp.render_plugin_page(html, tokens, lang)


def plugin_page(handler, m, lang):
    """无自有 handle_get 的插件：渲染其模板（由 Manifest.template 指定）。"""
    if not m.template:
        return webapp.render_plugin_page(
            "<p>该插件未提供页面。</p>", {"title": m.name, "active": "plugins"}, lang)
    tpl_path = os.path.join(m.dir, m.template)
    with open(tpl_path, "r", encoding="utf-8") as f:
        text = f.read()
    tokens = {
        "title": m.title.get(lang, m.title["zh"]),
        "active": "plugins",
        "display_name": m.display_name.get(lang, m.display_name["zh"]),
        "description": m.description.get(lang, m.description["zh"]),
        "config_json": __import__("json").dumps(MG.get_config(m.name), ensure_ascii=False),
    }
    return webapp.render_plugin_page(text, tokens, lang)
