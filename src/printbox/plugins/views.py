"""插件 Web 视图：/plugins 索引页 + 无自有 handle_get 时的通用插件页。"""
import os
import html
from . import manager as MG
from . import manifest as M
from .. import i18n as I18N
from .. import webapp


def index(handler, lang, notice=""):
    rows = []
    for p in MG.list_plugins():
        disp = p["display_name"].get(lang, p["display_name"]["zh"])
        cap = ", ".join(p["required_capabilities"]) or "-"
        if p["enabled"]:
            state = "已启用" if lang == "zh" else "Enabled"
        else:
            state = "未启用" if lang == "zh" else "Disabled"
        rows.append(
            f'<tr><td><a href="{p["route"] if p["enabled"] else "#"}">{disp}</a></td>'
            f'<td>{p["type"]}</td><td>{state}</td>'
            f'<td><code>{cap}</code></td></tr>')
    rows_html = "".join(rows) or '<tr><td colspan="4">[[i18n:plugins_none]]</td></tr>'

    notice_html = ""
    if notice:
        notice_html = f'<p class="badge ok">{I18N.translate(html.escape(notice), lang)}</p>'

    import_card = f"""
  <div class="card" style="margin-top:18px">
    <h2>[[i18n:plugins_import_title]]</h2>
    <p class="muted">[[i18n:plugins_import_desc]]</p>
    <form method="post" action="/plugins">
      <input type="hidden" name="act" value="import_url">
      <label>[[i18n:plugins_import_url_label]]
        <input type="text" name="url" size="50"
               placeholder="https://example.com/docker-compose.yml">
      </label>
      <p class="muted">[[i18n:plugins_import_url_hint]]</p>
      <label>[[i18n:plugins_import_meta_label]]
        <input type="text" name="metadata_url" size="50"
               placeholder="https://example.com/metadata.json (可选)">
      </label>
      <label>[[i18n:plugins_import_name_label]]
        <input type="text" name="name" size="30" placeholder="[[i18n:plugins_import_name_ph]]">
      </label>
      <p><button type="submit">[[i18n:plugins_import_btn]]</button></p>
    </form>
    {notice_html}
  </div>
"""
    body = f"""
  <h2>[[i18n:plugins_title]]</h2>
  <p class="muted">[[i18n:plugins_desc]]</p>
  <table class="tbl">
    <thead><tr><th>[[i18n:plugins_col_name]]</th><th>[[i18n:plugins_col_type]]</th>
    <th>[[i18n:plugins_col_state]]</th><th>[[i18n:plugins_col_caps]]</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
  {import_card}
"""
    tokens = {
        "title": "插件" if lang == "zh" else "Plugins",
        "active": "plugins",
        "BODY": body,
    }
    return webapp.render_plugin_page(body, tokens, lang)


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
