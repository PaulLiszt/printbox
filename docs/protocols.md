# 无线打印协议覆盖矩阵

目标：让 Windows / macOS / iOS / Linux / Android / HarmonyOS 都能"无驱动/少驱动"地使用这台打印机。

## 通用底座
所有协议都建立在 **IPP（Internet Printing Protocol）** 之上。CUPS 原生就是 IPP 服务器，
因此只要 CUPS + Avahi 起来，绝大多数现代系统都能用。

## 各系统如何发现并打印

| 系统 | 发现机制 | 打印路径 | 备注 |
|---|---|---|---|
| **Windows 10/11** | WS-Discovery（`wsdd` 发布） | IPP（`http://print.local:631/printers/...`）/ LPD | "网络"里可见设备；也可手动加 IPP 端口 631 |
| **macOS** | Bonjour/mDNS（`_ipp._tcp` + AirPrint TXT） | AirPrint / IPP | 系统"打印机与扫描仪"自动出现 |
| **iOS / iPadOS** | AirPrint（mDNS `_ipp._tcp` + `TXT` 含 `air=all`） | AirPrint | 分享/打印菜单直接可见 |
| **Linux（桌面）** | Avahi + CUPS Browsed | IPP Everywhere | 自动出现，无需驱动（驱动在服务器端） |
| **Android** | Mopria（基于 IPP Everywhere） | IPP | 系统打印框架/厂商打印服务自动发现 |
| **HarmonyOS** | Mopria / IPP | IPP | 系统打印框架自动发现 |

## 发布的 mDNS 服务
1. `_ipp._tcp` —— 基础 IPP（Linux/Windows/Mopria 共用）
2. `_printer._tcp` —— 传统 LPD/Socket 兼容
3. `_uscan._tcp` / `_ippusb._tcp` —— `ipp-usb` 自动发布的 USB 打印机网络服务
4. 显式 `airprint.service` —— 带 AirPrint TXT 记录，确保 iOS/macOS 识别

## 驱动策略（服务器端）
- 打印机驱动装在小主机上，终端**几乎不需要驱动**（"驱动在服务器"模型）。
- HP P1505：HPLIP `hpcups`（或 `hpijs`），由 `printer_setup.py` 自动选择。
- 通用 PostScript/PCL 激光：`gutenprint` / `foomatic` / `postscript` 驱动兜底。
- 现代 USB 打印机优先 `ipp-usb`（网络协议跑 USB），兼容性最佳。

## 浏览器入口
所有系统都可打开 `http://print.local`（或路由器分配的 `print.lan` / `print.home`，
兜底用 Web 首页显示的 IP）进行：查看打印机状态、打印测试页、改设置、看日志。

## 防火墙
- 放行：TCP 631（IPP/CUPS）、TCP 80（Web）、TCP 22（SSH）、UDP 5353（mDNS）、
  UDP 3702（WS-Discovery）、TCP 515（LPD，可选）。
- 默认仅监听局域网接口，不暴露到公网。
