# ant-extend

## 安装与入口

安装 Python 3.10+ 和 Antigravity CLI（`agy`），解压后使用对应安装器。

| 系统 | 安装 | Python |
| --- | --- | --- |
| macOS | `Install.command` | 优先 Homebrew Python，支持 Apple Silicon 和 Intel |
| Windows | `Install.cmd` | `py -3` 或 PATH 中的 Python 3.10+ |

安装器使用本机解释器生成 Hook 和 Sidecar 配置，保留旧版设置与恢复记录。可用 `ANT_EXTEND_PYTHON` 指定已有解释器的完整路径。升级后如果扩展入口尚未出现，重开 Antigravity。

在 Antigravity 打开会话，在右侧 **Overview → Extensions → ant-extend** 打开侧栏。每个会话的侧栏显示自己的状态、计数与暂停。

## 自动恢复

| 环节 | 默认行为 |
| --- | --- |
| 无进展检测 | 连续 120 秒无进展时触发恢复 |
| 续跑前等待 | 10 → 20 → 30 → 60 秒，之后保持 60 秒 |
| 共享上限 | 提前恢复和 Stop Hook 共用每轮 8 次上限，首次自动恢复起算 30 分钟 |
| 工具与确认 | 工具运行、等待确认和后台任务期间暂停无进展检测 |
| 暂停 | 当前请求继续运行，暂停自动恢复 |
| 恢复 | 重新允许监测后续请求 |

恢复使用宿主的空输入续跑，沿用当前会话、模型、工作区及权限。次数表示已尝试恢复，最终回复仍由模型生成。Carbon 多次无进展时，侧栏提示可以在会话模型菜单里改用 Hydrogen 继续。

## 思考与执行

会话中的原生 `Worked for` 组保留中间过程。侧栏可展开查看本轮近期进展、宿主公开的思考内容和工具活动。打开期间，任务完成后自动收起。最终回答留在会话正文。

## 设置与兼容

切换到侧栏的「设置」页，可暂停全部会话、选择检测节奏，以及修改次数和递增间隔。编辑后点击「保存设置」。较长的推理任务可选稳健节奏。

在「无进展检测」里选择「自定义」，即可填写检测间隔。可填一个固定秒数，也可用逗号分隔多个递增秒数，每项最多 120 秒。检测间隔决定无进展多久后触发恢复，递增等待决定每次续跑前等多久，每项最多 60 秒。使用多个值时，达到序列末尾后沿用最后一个值。

终端仍支持 `--status`、`--check-config`、`--pause`、`--resume`。

侧栏要求宿主支持 UI Sidecar。扩展会检查本地会话接口，校验通过后即可使用监测和过程查看。读状态短暂失败时会重新连接。续跑所需接口无法校验时暂停提前恢复，Stop Hook 仍可处理请求结束时的临时错误。

侧栏使用 Antigravity 分配的回环端口和宿主令牌，状态位于 `~/.gemini/antigravity/plugin_data/ant-extend`。过程内容按需读取。停用或卸载使用应用插件开关或 `agy plugin disable ant-extend` / `agy plugin uninstall ant-extend`。

后台协议参考见 [第三方声明](../THIRD_PARTY_NOTICES.md)。侧栏集成说明见 [Antigravity Sidecars](https://antigravity.google/docs/sidecars) 和 [原生侧栏](https://antigravity.google/docs/side-panel)。
