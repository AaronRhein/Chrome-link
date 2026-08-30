# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式。

## [1.0.0] - 2026-08-30

### Added

- 拖动转移：按住修饰键把 Chrome 窗口拖向面向对端的屏幕边缘（鼠标顶边即触发），支持 `shift` / `fling` / `both` 触发模式
- 全局热键兜底（默认 `Ctrl+Alt+G`）直接甩出当前 Chrome 窗口
- 双侧滑出/滑入动画（默认 240ms 缓动），接收端自动还原激活标签
- Chrome MV3 扩展：事件推送制（空闲零占用），上报窗口位置与标签列表，支持远程聚焦命令
- bridge.py 事件驱动检测（WinEvent 钩子，空闲零轮询）、单实例互斥、1MB 上限日志
- 局域网点对点传输协议（共享密钥 + ack，失败自动放弃并提示音）
- 双向支持：两台机器运行同一份代码与配置
- selftest 自动化自检（WebSocket / DPI 匹配 / 动画关窗 / 触发逻辑 / 真实 Chrome 端到端）
- 便携运行时一键下载脚本（`scripts/download-runtime.bat`，免安装 Python）
