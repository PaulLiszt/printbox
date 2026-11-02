# PrintBox 插件标准（Plugin Standard）v0.1

> 设计第一原则：**不重复造轮子**。PrintBox 只做「管理壳」，真正的功能来自两条既有生态：
> - **native 插件** → 复用 Debian apt 包（samba / aria2 / transmission…）
> - **container 插件** → 直接吃现成 Docker 镜像（Home Assistant / Frigate / qBittorrent…），并可经**兼容层**一键导入 CasaOS / Umbrel / runtipi 的应用商店定义。
>
> 你要写的只有「配置模板 + Web 开关 + 生命周期」，应用本体 100% 复用上游。

---

## 1. 插件形态

一个插件是一个目录，包含：

```
<plugin-name>/
  plugin.json      # 清单（必填）
  page.html        # Web 页面模板（可选，用 [[token]] 与 [[i18n:KEY]]）
  module.py        # 生命周期钩子（可选）
  docker-compose.yml / 其它资源（container 插件）
```

内置插件放在 `src/printbox/plugins/builtin/`；用户安装的第三方插件放在 `/etc/printbox/plugins.d/`。

---

## 2. 清单字段（plugin.json）

| 字段 | 类型 | 含义 |
|---|---|---|
| `name` | str | 唯一标识，仅 `[a-z0-9-_]`（也作为 systemd 单元前缀 `printbox-plugin-<name>.service`） |
| `display_name` | {zh,en} | 显示名 |
| `version` | str | 插件版本 |
| `type` | `"native"` \| `"container"` | 复用哪条生态 |
| `description` | {zh,en} | 说明 |
| `license` | str | 许可证（第三方导入时记 `see upstream`） |
| `required_capabilities` | list[str] | 启用前必须向用户确认的权限，如 `["root","apt-install","docker"]` |
| `apt_packages` | list[str] | native 专用：`enable` 时由宿主 `apt-get install` |
| `compose` | str | container 专用：相对插件目录的 docker-compose 文件 |
| `systemd_units` | list[str] | 宿主托管/启停的 systemd 单元 |
| `config_schema` | dict | 配置字段：`field -> {type, default, desc:{zh,en}}` |
| `web` | {route, template, title} | Web 接入点 |
| `module` | str | 可选：相对目录的 python 模块（含钩子） |

---

## 3. 生命周期

宿主在 `printbox plugin enable <name>` 时：

1. **权限确认**：若插件 `required_capabilities` 超出已确认集合，拒绝并提示（信任模型见 §6）。
2. **native**：`apt-get install` 声明的 `apt_packages`。
3. **install 钩子**（若有）：插件 `module.py` 的 `install(manifest)` 写配置/建目录。
4. **container**：宿主写 `/etc/systemd/system/printbox-plugin-<name>.service`（ExecStart=`docker compose up -d`），`systemctl enable`。
5. 在 `config.json["plugins"][name]["enabled"]=true` 标记，并注册 Web 路由。

`disable` 反向：停单元、调 `disable` 钩子、标记停用。

---

## 4. 配置命名空间

插件配置落在 `config.json["plugins"][<name>]["config"]`，与核心及其它插件互不踩。
读取：`manager.get_config(name)`（清单默认值 深合并 已保存值）；
写入：`manager.set_config(name, data)`（原子写）。

---

## 5. Web 集成

- `web.route` 声明页面路径（如 `/plugin/storage-share`）。
- 宿主在 `_do_get` / `_do_post` 开头拦截插件路由：命中则调插件的 `handle_get(handler, lang)` / `handle_post(handler, form, lang)`；未提供钩子则用通用页面渲染 `page.html`。
- 插件页面**直接复用核心的 `render_plugin_page`**，因此天然支持中英双语：模板用 `[[token]]` 填值，用 `[[i18n:KEY]]` 填长文案（KEY 在 `i18n.STRINGS` 里配 zh/en），短标签走全局 `i18n.PHRASES` 短语词典。
- `/plugins` 是插件索引页，导航栏已加「插件」入口。

---

## 6. 权限 / 信任模型（很重要）

插件以 **root** 运行，能装系统包、管服务、拉容器。**装别人写的插件 = 给 root 权限**。因此：

- 每个插件必须声明 `required_capabilities`；
- 宿主 `enable` 第三方插件前**必须显式列出权限让用户确认**（CLI 的 `--yes` 即代表确认）；
- 内置插件（storage-share / homeassistant）由 PrintBox 官方维护，默认可信；
- 第三方来源（CasaOS/Umbrel/runtipi）导入时，兼容层会保留 `_import.source` 与 `upstream_image`，UI 应展示「来源 + 镜像」供用户判断。

---

## 7. 兼容层：直接复用别人家的插件

`compat.import_compose(compose_path, metadata_path, name)` 把第三方 `docker-compose.yml` + 元数据
（CasaOS 的 `metadata.json`、Umbrel 的 `umbrel-app.yml`、runtipi 的 `config.json` 都认）转换成
PrintBox 清单（type=container）。已验证可正确提取镜像、端口、卷，并归一化插件名。

CLI 用法：

```bash
printbox plugin import ./docker-compose.yml --metadata ./metadata.json --name my-app
printbox plugin enable my-app --yes
```

> 注意：第三方商店假定自己的反向代理/卷布局/网络，兼容层做结构映射，但**个别应用仍需少量适配**（端口冲突、特权需求）。这正是「管理壳」要补的活，远小于从零写应用。

---

## 8. 你列的 5 类插件 —— 复用映射

| 需求 | 推荐类型 | 直接复用的上游 | PrintBox 要写的 |
|---|---|---|---|
| 磁盘挂载 / USB 硬盘共享 | native | 系统 `udisks2` / `mount` + `samba` | 挂载单元 + SMB 配置片 + Web 开关 |
| SMB + **Time Machine** | native | `samba` + `samba-vfs-modules`（`fruit` 模块） | 仅 smb.conf 几行配置（见 storage-share POC） |
| 下载 | native 或 container | apt: `aria2` / `qbittorrent-nox`；或容器 `p3terx/aria2-pro` | 配置 + Web 链接 |
| 监控录像 | container | `frigate`（官方就是 Docker，需 ffmpeg/Coral） | compose + systemd 托管 + Web 链接 |
| 智能家电 | container | **`homeassistant/home-assistant`**（自身即插件平台，白送上千集成） | 几乎只写 compose（见 homeassistant POC） |

---

## 9. 两个 POC 插件（已随包验证）

- **storage-share**（native）：SMB 共享 USB 磁盘 + Time Machine 开关。`generate_smb_conf()` 是纯函数，
  TM 开启时追加 `vfs objects = catia fruit streams_xattr` + `fruit:time machine = yes`；关闭则不含。
- **homeassistant**（container）：包官方 HA 容器，`network_mode: host`，由宿主 systemd 单元托管启停。

两者 + 兼容层导入，已通过 24 项单元测试（manifest / smb 生成 / compose 解析 / import 映射 / 路由渲染中英）。
