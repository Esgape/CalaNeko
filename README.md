# 🐱 CalaNeko — Windows 游戏进程优化工具

> 面向 Windows 游戏场景的进程优化助手：自动调整 CPU / IO / 内存优先级，降低后台干扰，让游戏更流畅。

CalaNeko 是一个轻量级 Windows 桌面工具，基于 Python 实现，通过 Web 界面管理。启动后它会持续监控游戏进程，自动将游戏进程提升到高优先级，同时把后台非必要进程降权，并支持一键还原。

## 功能特性

- **优先级自动调整**：游戏进程 CPU 高优先级，后台进程自动降权（below_normal / low）
- **IO / 内存优先级**：游戏进程 IO/内存正常，后台进程 IO/内存降至 low
- **4 套预设**：通用 / 卡拉彼丘·FPS / 单机游戏 / 联网二游（含原神、星铁、绝区零等反作弊进程处理）
- **反作弊（ACE）降权开关**：可选择性降低反作弊进程权重，避免其抢占游戏资源
- **崩溃自动恢复**：ledger 机制记录每次修改，异常退出后下次启动自动还原全部设置
- **Web 管理界面**：浏览器访问 `http://127.0.0.1:18765` 即可操作，无需命令行
- **单实例互斥**：端口检测 + Mutex 双重保障，防止重复启动
- **双模式运行**：管理员权限完整功能；普通权限可优化当前用户会话内的进程
- **日志导出**：一键导出系统信息与操作日志，方便反馈问题

## 快速开始

```bash
# 1. 安装依赖（仅需 psutil）
pip install -r requirements.txt

# 2. 启动
python src/game_boost_web.py
# 或直接双击「启动 CalaNeko.bat」

# 3. 浏览器打开
http://127.0.0.1:18765
```

> 💡 建议以管理员身份运行，功能更完整（可优化其他用户会话的进程）。普通权限也能运行，功能受限。

## 预设说明

| 预设 | 适用场景 | 说明 |
|---|---|---|
| 通用 | 大多数游戏 | 默认配置 |
| 卡拉彼丘·FPS | 卡拉彼丘、三角洲、瓦国服等 FPS | 针对 FPS 进程优化，ACE 降权可关 |
| 单机游戏 | 3A / 独立游戏 | 常规优化 |
| 联网二游 | 原神、异环、少前2等 | 进程处理 |

可在 Web 界面中「存为预设」自定义，预设保存在 `config/presets/`。

## 目录结构

```
CalaNeko/
├── src/                  # Python 源码
│   ├── game_boost_web.py # Web 服务与主逻辑
│   ├── game_boost_pro.py # 核心优化逻辑
│   ├── game_boost_cli.py # 命令行入口
│   ├── game_boost_gui_simple.py # 简易 GUI
│   └── win_process_api.py# Windows API 封装（ctypes）
├── config/
│   ├── config.example.json # 配置示例
│   └── presets/           # 预设文件
├── assets/               # 图标、favicon 等资源
├── tests/                # 单元测试
└── docs/                 # 文档
```

## 测试

```bash
python tests/test_core.py
```

覆盖：ledger 原子写入/崩溃恢复、单实例互斥、配置加载保存、线程安全，共 11 项。

## 借鉴与致谢

本项目部分设计与实现参考了以下开源项目，在此致谢：

- **[quick-fps-optimizer](https://github.com/quick-fps-optimizer)**：durable ledger（崩溃恢复持久化）设计
- **Pavise-Game**：崩溃自愈（HealFromCrash）、启动参数、单实例互斥设计
- **[ProcGovernor](https://github.com/Prohect/ProcGovernor)**：可选第二引擎（Windows 进程治理工具），默认不内置，可自行下载放入 `engine/ProcGovernor/`
- 腾讯 **GameShift**：系统级调优机制的调研参考
- **图标素材**：糖猫头像直接取自表情包网图（仅作个人项目展示，版权归原作者所有；如需商用请自行替换）

## 开发记录

本项目由 AI（豆包）辅助开发，开发过程采用多模型交叉代码审查（DeepSeek / GLM / MiMo / 知乎直答），每个版本均经过审查闭环与单元测试验证。详见 `docs/操作手册.md`。

## License

MIT License

Copyright (c) 2026 Esgape

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
