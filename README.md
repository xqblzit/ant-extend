# ant-extend

在 Antigravity 原生侧栏查看自动重试、会话状态与上下文占用。支持 macOS 和 Windows。

| 功能 | 体验 |
| --- | --- |
| 静默恢复 | 递增间隔，在同一会话继续 |
| 原生侧栏 | 当前会话的重试次数、倒计时和暂停 |
| 会话详情 | 展开查看恢复记录与思考、执行过程 |
| 运行监测 | 宿主运行状态、上下文分类与最近一次请求的逐项 Token 明细 |
| 设置页 | 全部会话的恢复开关、重试策略与侧栏显示 |

安装包需要 Python 3.10+ 和 Antigravity CLI。macOS 运行 `Install.command`，Windows 运行 `Install.cmd`。

打开一个会话，在右侧 **Overview → Extensions → ant-extend** 查看。

[完整使用说明](docs/GUIDE.md)
