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
- “撤回跳过视频消息”默认关闭；开启后，自动撤回会跳过视频段和常见视频格式文件。
- 历史扫描窗口默认为 15 秒，可调整为 0 到 15 秒。默认关闭“开启历史窗口撤回”时，仅撤回发送接口返回的消息 ID；拆分或连续发送多条消息时可能只撤回最后一条。开启后会撤回该时间窗口内的机器人消息，也会撤回其他插件发送的消息，请谨慎开启。

## 发送流程说明（v1.1.4）

- 装饰阶段不再抢先发送、不再清空消息链。AstrBot 原生 TTS、文本转图片、分段回复、@/引用回复均可正常执行。
- 插件幂等包装 OneBot `bot.call_action`，只读取原生发送回执中的 `message_id`，用于消息历史、编号撤回和自动撤回队列；发送失败不写入历史。
- 用 `ContextVar` 隔离并发回复并校验发送目标会话；拆分消息、语音单独发送等多条回执会逐条记录。插件卸载时恢复被包装的 API。
- 流式回复不经过 `ResultDecorateStage` 的 TTS 流程，自动撤回主要依赖协议端自身消息回显。

## 本地测试

```bash
python -m unittest discover -s tests -v
```

测试用 stub 模拟 AstrBot / aiocqhttp，无需安装运行时依赖，覆盖原生 TTS 链路、回执捕获、并发隔离、发送失败、重复安装和自动撤回 ID 等场景。

## 🐔 联系作者

- **反馈**：欢迎在 [GitHub Issues](https://github.com/1592363624/astrbot_plugin_batchrecall/issues) 提交问题或建议
QQ群:91219736
telegram:[巅峰阁](https://t.me/ShellDFG)
