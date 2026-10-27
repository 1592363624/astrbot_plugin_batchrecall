<div align="center">

![:shell](https://count.getloli.com/@github_monitor_shell?name=github_monitor_shell&theme=minecraft&padding=7&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)


[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-3.4%2B-orange.svg)](https://github.com/Soulter/AstrBot)
[![GitHub](https://img.shields.io/badge/作者-Shell-blue)](https://github.com/1592363624)

</div>

## 指令列表

- **批量撤回**：批量撤回最近的消息
  - 格式：批量撤回 @用户 撤回数量 (撤回指定用户消息)
  - 格式：批量撤回 撤回数量 (倒序撤回消息)

## 自动撤回说明

- 自动撤回任务由单一 FIFO 队列逐条处理；同一会话的撤回请求间隔至少 200 毫秒。
- 消息量较大或协议端响应较慢时，队列中的后续消息可能晚于配置时间撤回。
- 历史补录会在 15 秒窗口内按本次发送的文本片段和媒体类型匹配机器人消息，可在配置中调整为 0 到 15 秒。OneBot 历史不含插件来源信息；其他插件若在同一窗口发送相同内容或相同媒体类型，仍可能匹配。

## 🐔 联系作者

- **反馈**：欢迎在 [GitHub Issues](https://github.com/1592363624/astrbot_plugin_batchrecall/issues) 提交问题或建议
QQ群:91219736
telegram:[巅峰阁](https://t.me/ShellDFG)
