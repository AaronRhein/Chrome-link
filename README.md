<div align="center">

# 🪟 Chrome Window Bridge

**按住 Shift，把 Chrome 窗口「甩」到另一台电脑**

*Throw an open Chrome window from one PC to another with a single drag*

[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue)](#)
[![Python](https://img.shields.io/badge/python-3.8%2B-informational)](#)
[![Browser](https://img.shields.io/badge/browser-Chrome-yellow)](#)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](#license)

[特性](#-特性) · [工作原理](#-工作原理) · [安装](#-安装) · [使用](#-使用) · [配置](#️-配置) · [故障排查](#-故障排查)

</div>

> **English**: Chrome Window Bridge is a missing piece for KVM keyboard/mouse sharing setups (Input Director, Mouse without Borders, Synergy…). Those tools share your input and clipboard — but windows stay trapped on their own machine. This tool lets you "throw" an open Chrome window across: hold **Shift**, push the window toward the screen edge facing the other PC, and it slides off this machine and slides in on the other one with all of its tabs, animated over the LAN. A global hotkey (`Ctrl+Alt+G`) does the same without the mouse.

## 📖 背景

Input Director / Mouse without Borders / Synergy 只共享**键鼠和剪贴板**，窗口永远留在各自电脑里；Chrome 自带的「发送到你的设备」一次只能发一个标签页，还得解锁目标设备。这个项目补上缺失的一环：**像拖窗口到副屏一样，把整个 Chrome 窗口拖到另一台电脑**。

## ✨ 特性

- 🖱️ **拖边即走** — 按住 `Shift` 把 Chrome 窗口推向面向对端的屏幕边缘，鼠标顶边的瞬间窗口脱手飞出（正是 KVM 切换控制的那一瞬间）
- ⌨️ **全局热键** — `Ctrl+Alt+G` 不经鼠标，直接把当前 Chrome 窗口甩到对端
- 🎬 **双侧动画** — 本机滑出、对端滑入，各 240ms 缓动，观感与系统拖拽同级
- 🗂️ **整窗迁移** — 窗口内全部标签（含顺序、固定态、激活标签）原样重建
- 🔁 **双向可用** — 两台机器跑同一份代码，谁拖谁生效
- 🪶 **近乎零占用** — 事件驱动架构（WinEvent 钩子 + 扩展事件推送），空闲 CPU ≈ 0、内存约 21MB、无常驻网络连接
- 🔒 **本地点对点** — 仅局域网直连 + 共享密钥校验，不经过任何第三方服务
- 🛟 **安全兜底** — 对端不可达时放弃转移并提示音；被甩走的窗口随时可 `Ctrl+Shift+T` 找回

## ⚙️ 工作原理

```text
        电脑 A (主)                                电脑 B (从)
┌───────────────────────┐                 ┌───────────────────────┐
│  Chrome               │  Shift+推向左缘  │                       │
│  [窗口] ────────► 滑出 │ ═══════════════► │ 滑入 ◄──────── [窗口]  │
│  bridge.py            │  TCP:9430(密钥)  │  bridge.py            │
│    ▲ WebSocket:9422   │                 │    ▲ WebSocket:9422   │
│    扩展(事件推送标签态) │                 │    扩展(事件推送标签态) │
└───────────────────────┘                 └───────────────────────┘
```

- **Chrome 扩展（MV3）**：仅在标签/窗口变化时被唤醒，把每个 Chrome 窗口的位置与标签列表推送给本机 bridge，随后即可休眠（空闲零占用）。拖动窗口本身不改变标签列表，因此触发时用最近一次状态即准确。
- **bridge.py（每台机器一个）**：
  - **检测**：`SetWinEventHook` 感知「真的有窗口开始被拖动」才进入 30ms 采样（平时 0.5s 兜底心跳，空闲零轮询）。触发 = 拖动 Chrome 窗口时鼠标顶到 `transfer_side` 边缘且按住修饰键。
  - **发送方**：先与对端握手（共享密钥 + ack）→ 播放滑出动画 → `WM_CLOSE` 优雅关窗 → 发送标签列表。
  - **接收方**：启动/复用 Chrome，新窗口先定位到屏幕外，再从相邻边缘滑入，并命令扩展把原激活标签置前。

## 📦 安装

> 两台电脑执行相同步骤，仅需按机器改两处配置。要求：Windows 10/11、Google Chrome、同一局域网。

### 1. 获取项目

```bat
git clone https://github.com/<you>/chrome-window-bridge.git
```

### 2. 准备运行时（二选一）

- **方式 A（推荐，免安装）**：双击运行 `scripts\download-runtime.bat`，自动下载官方便携版 Python（约 11MB）到 `python-embed\`。
- **方式 B**：机器上已有 **Python 3.8+**，跳过即可（脚本会自动回退到系统 Python）。

### 3. 配置

```bat
copy config.example.json config.json
```

每台机器改两个字段，其余保持一致：

| 字段 | 说明 |
|---|---|
| `transfer_side` | 本机**面向对方**的屏幕边缘（`left` / `right`）。例：笔记本在 PC1 左侧 → PC1 填 `left`，笔记本填 `right` |
| `peer_ip` | **对方**的局域网 IP（对方机器上运行 `ipconfig` 查看） |

> `secret` 两台必须一致，建议改成随机长字符串；端口两台保持一致即可。

### 4. 安装 Chrome 扩展（每台一次）

打开 `chrome://extensions` → 右上角开启「开发者模式」→「加载已解压的扩展程序」→ 选择本项目的 `extension` 目录。

### 5. 防火墙放行（每台一次，管理员运行）

```bat
netsh advfirewall firewall add rule name="ChromeWindowBridge" dir=in action=allow protocol=TCP localport=9430
```

不执行也行：首次运行时在 Windows 弹窗中点「允许访问」。

### 6. 启动与开机自启

- **调试启动**：双击 `start_console.bat`（带日志窗口，`Ctrl+C` 退出）。
- **日常/自启**：双击 `start_bridge.vbs` 静默运行；`Win+R` → `shell:startup`，把 `start_bridge.vbs` 的**快捷方式**放进启动文件夹即可开机自启。

## 🖱️ 使用

| 动作 | 操作 |
|---|---|
| PC1 → 笔记本 | 按住 `Shift` 拖住窗口标题栏向**左**推，**鼠标顶到屏幕左边缘**的瞬间窗口飞出（窗口本身不需要贴边） |
| 笔记本 → PC1 | 按住 `Shift` 拖向**右边缘**，同理* |
| 不想拖拽 | `Ctrl+Alt+G` 直接甩出当前 Chrome 窗口 |
| 找回误甩窗口 | 原机器按 `Ctrl+Shift+T` |

\* 多数 KVM（含 Input Director）为主从单向，反向拖拽时光标不会跟着过去，转移本身不受影响。

## ️⚙️ 配置项

`config.json` 完整字段（模板见 `config.example.json`）：

| 键 | 默认 | 说明 |
|---|---|---|
| `transfer_side` | `left` | 面向对端的屏幕边缘：`left` / `right` |
| `peer_ip` | — | 对端局域网 IP（**必填**） |
| `peer_port` | `9430` | 对端接收端口，两台一致 |
| `listen` | `0.0.0.0` | 本机接收监听地址 |
| `secret` | — | 共享密钥，两台一致 |
| `ws_port` | `9422` | 扩展通信端口（改后需同步改 `extension/bg.js` 顶部 `PORT`） |
| `trigger_mode` | `shift` | `shift`（修饰键+顶边）/ `fling`（甩出屏幕 ≥45%）/ `both` / `off` |
| `debug` | `false` | `true` 时日志记录拖动事件与触发判定细节 |
| `modifier_vk` | `16` | 触发修饰键虚拟键码（16=Shift） |
| `zone_px` | `45` | 兜底路径：窗口贴边感应区宽度（物理像素） |
| `fling_ratio` | `0.45` | `fling` 模式甩出比例 |
| `hotkey` | `Ctrl+Alt+G` | `{"enabled":true,"mod":3,"vk":71}`；mod：1=Alt 2=Ctrl 4=Shift；被占用就换 `vk` |
| `anim_ms` | `240` | 两侧动画时长（毫秒） |
| `chrome_path` | 空 | Chrome 非默认安装位置时手动指定 |

## 🩺 故障排查

<details>
<summary><b>拖到边缘没反应</b></summary>

先用 `start_console.bat` 看日志：

- 没有「扩展已连接」→ 扩展没装好或 Chrome 没开；
- 「对端不可达」→ 对端 bridge 没跑 / `peer_ip` 没填 / 防火墙没放行；
- 什么日志都没有 → `config.json` 设 `"debug": true` 重启后拖一次，把 `bridge.log` 内容提交 issue。
- 另外注意手势：必须**先按住 Shift 再拖**，触发点是「鼠标顶到面向对端的边缘」，不是窗口贴边。

</details>

<details>
<summary><b>「哔」一声但不转移</b></summary>

窗口未匹配到扩展状态（bridge 刚重启而 Chrome 一直没动过）——随便开关一个标签页或等最多 1 分钟闹铃刷新后再试。对端不可达时也会提示音并放弃（窗口保持原样）。

</details>

<details>
<summary><b>之前能用，突然失灵</b></summary>

多半是路由器 DHCP 重新分配了某台机器的 IP——重新 `ipconfig` 更新 `peer_ip`；一劳永逸可在路由器后台给两台机器绑定静态 IP。

</details>

<details>
<summary><b>端口被占用 / 改了配置不生效</b></summary>

`netstat -ano | findstr 9422` 查占用；改 `ws_port` 后需同步改扩展 `bg.js` 的 `PORT` 并在 Chrome 里重载扩展。改 `config.json` 后需重启 bridge。

</details>

## 🚧 已知边界

- 无痕窗口、PWA 应用窗口、DevTools 窗口不参与转移
- 最大化窗口请先还原再甩
- 只监测主显示器；多显示器请把面向对端的屏设为主屏
- 接收端 Chrome 未运行时首次有 1–2s 冷启动（建议保持 Chrome 挂后台）
- 扩展仅需 `tabs` 与 `alarms` 权限；数据只在两台机器间点对点传输

## 🧪 自测

```bat
python-embed\python.exe selftest.py          # 基础 7 项：WebSocket / DPI 匹配 / 动画 / 关窗
python-embed\python.exe selftest.py --drag   # + 触发路径逻辑测试
python-embed\python.exe selftest.py --full   # + 真实 Chrome 开窗滑入端到端测试
```

## 📁 项目结构

```text
chrome-window-bridge/
├── bridge.py                 # 主程序（两台机器同一份）
├── config.example.json       # 配置模板（复制为 config.json）
├── extension/                # Chrome 扩展 (MV3)
│   ├── manifest.json
│   └── bg.js
├── scripts/
│   └── download-runtime.bat  # 一键下载便携 Python 运行时
├── selftest.py               # 自动化自检
├── start_bridge.vbs          # 静默启动（可开机自启）
├── start_console.bat         # 调试启动（带日志窗口）
└── README.md
```

## 📄 License

[MIT](LICENSE) © 2026 Lorris
