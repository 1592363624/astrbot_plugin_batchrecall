import asyncio
import re

from aiocqhttp.exceptions import ActionFailed

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import At, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


@register(
    "astrbot_plugin_batchrecall",
    "Shell",
    "批量撤回,指定撤回,自动撤回,防撤回,撤回,撤回,撤回,撤回自身",
    "1.0.3",
    "https://github.com/1592363624/astrbot_plugin_batchrecall",
)
class BatchRecall(Star):
    def __init__(self, context: Context, config):
        super().__init__(context)
        self.conf = config
        self.recall_tasks = set()
        logger.info(f"自动撤回插件已加载，撤回时间: {self.conf['recall_time']}秒")

    def _remove_task(self, task: asyncio.Task):
        """移除已完成的任务"""
        self.recall_tasks.discard(task)

    async def _recall_msg(self, client, message_id: int):
        """撤回消息 - 参考其他插件的写法"""
        recall_time = self.conf["recall_time"]
        logger.info(f"⏰ 等待 {recall_time} 秒后撤回消息 {message_id}")

        await asyncio.sleep(recall_time)
        try:
            if message_id and message_id != 0:
                await client.delete_msg(message_id=message_id)
                logger.info(f"✅ 已自动撤回消息: {message_id}")
        except ActionFailed as e:
            if getattr(e, "retcode", None) == 1200:
                logger.info(
                    f"撤回消息可能已超时或被撤回，message_id={message_id}, retcode={e.retcode}",
                )
                return
            logger.error(f"撤回消息失败: {e}")
        except Exception as e:
            logger.error(f"撤回消息失败: {e}")

    def _should_enable_recall(self, event: AstrMessageEvent) -> bool:
        """判断是否应该启用撤回"""
        if not event.get_group_id():
            # 私聊场景
            if event.is_admin():
                # 如果是管理员，检查是否允许撤回管理员私聊
                return self.conf.get("enable_admin_private_recall", False)
            return self.conf.get("enable_private_recall", True)

        group_id = event.get_group_id()
        group_whitelist = self.conf.get("group_whitelist", [])
        if group_whitelist and str(group_id) not in group_whitelist:
            return False
        return self.conf.get("enable_group_recall", True)

    @filter.on_decorating_result(priority=999)
    async def intercept_and_recall(self, event: AstrMessageEvent):
        """拦截消息并安排撤回 - 参考其他插件的模式"""
        try:
            # 检查是否启用撤回
            if not self._should_enable_recall(event):
                return
            if not isinstance(event, AiocqhttpMessageEvent):
                return

            # 获取配置中的撤回时间
            recall_time = self.conf["recall_time"]
            logger.info(f"🎯 拦截到机器人消息，{recall_time}秒后撤回")

            # 获取原始消息链
            result = event.get_result()
            if not result or not result.chain:
                logger.warning("消息链为空，跳过处理")
                return

            original_chain = result.chain.copy()
            result.chain.clear()
            message_chain = MessageChain(chain=original_chain)
            onebot_messages = await AiocqhttpMessageEvent._parse_onebot_json(
                message_chain,
            )
            if not onebot_messages:
                logger.warning("待发送消息为空，跳过处理")
                return

            is_group = bool(event.get_group_id())
            session_id = event.get_group_id() if is_group else event.get_sender_id()

            try:
                if is_group:
                    send_result = await event.bot.call_action(
                        "send_group_msg",
                        group_id=int(session_id),
                        message=onebot_messages,
                    )
                else:
                    send_result = await event.bot.call_action(
                        "send_private_msg",
                        user_id=int(session_id),
                        message=onebot_messages,
                    )
            except Exception as send_exc:
                logger.error(f"发送消息失败: {send_exc}")
                return

            message_id = None
            if isinstance(send_result, dict):
                message_id = send_result.get("message_id")

            if not message_id:
                logger.error("❌ 发送消息失败，无法获取消息ID")
                return

            logger.info(f"📤 发送成功，获取到消息ID: {message_id}")
            task = asyncio.create_task(self._recall_msg(event.bot, int(message_id)))
            task.add_done_callback(self._remove_task)
            self.recall_tasks.add(task)
            logger.info(f"✅ 已安排消息在 {recall_time} 秒后撤回")

        except Exception as e:
            logger.error(f"消息拦截处理失败: {e}")

    # 备选方案：使用消息历史记录获取消息ID
    async def _get_recent_bot_messages(self, event: AiocqhttpMessageEvent, count: int = 5):
        """获取最近的机器人消息 - 参考其他插件的模式"""
        try:
            payloads = {
                "group_id": int(event.get_group_id()),
                "count": count,
            }
            result = await event.bot.api.call_action("get_group_msg_history", **payloads)
            messages = result.get("messages", [])
            bot_messages = [
                msg
                for msg in messages
                if str(msg.get("sender", {}).get("user_id", "")) == event.get_self_id()
            ]
            return bot_messages
        except Exception as e:
            logger.error(f"获取消息历史失败: {e}")
            return []

    # 测试命令
    @filter.command("test_recall")
    async def test_recall_command(self, event: AstrMessageEvent):
        """测试撤回功能"""
        recall_time = self.conf["recall_time"]
        yield event.plain_result(f"🧪 测试消息，{recall_time}秒后此消息将会撤回...")

    @filter.command("recall_config")
    async def recall_config_command(self, event: AstrMessageEvent):
        """查看当前配置"""
        config_info = "📋 当前撤回配置:\n"
        config_info += f"撤回时间: {self.conf['recall_time']}秒\n"
        config_info += f"私聊启用: {self.conf.get('enable_private_recall', True)}\n"
        config_info += f"群聊启用: {self.conf.get('enable_group_recall', True)}\n"
        group_whitelist = self.conf.get("group_whitelist", [])
        if group_whitelist:
            config_info += f"白名单群: {len(group_whitelist)}个\n"
        else:
            config_info += "白名单群: 所有群聊\n"
        yield event.plain_result(config_info)

    async def terminate(self):
        """插件卸载时取消所有撤回任务"""
        for task in self.recall_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.recall_tasks, return_exceptions=True)
        self.recall_tasks.clear()
        logger.info("自动撤回插件已卸载")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("撤回自身")
    async def recall_bot_messages_command(self, event: AstrMessageEvent):
        """
        撤回机器人自身发送的消息:撤回自身 撤回数量
        """
        if not isinstance(event, AiocqhttpMessageEvent):
            yield event.plain_result("当前平台不支持撤回自身消息，仅支持 QQ 协议端。")
            return

        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("当前仅支持群聊中撤回机器人自身消息。")
            return

        # 解析撤回数量参数
        messages = event.get_messages()
        plain_parts: list[str] = []
        for segment in messages:
            if isinstance(segment, Plain):
                plain_parts.append(segment.text)
        full_text = "".join(plain_parts).strip()

        tail = full_text
        if "撤回自身" in tail:
            tail = tail.split("撤回自身", 1)[1]
        nums = re.findall(r"\d+", tail)
        if not nums:
            yield event.plain_result("请在指令后填写需要撤回的数量，例如：撤回自身 5")
            return

        count = int(nums[-1])
        if count <= 0:
            yield event.plain_result("撤回数量必须为正整数。")
            return

        max_count = int(self.conf.get("batch_max_count", 20))
        if count > max_count:
            count = max_count

        try:
            fetch_count = min(max_count * 3, 100)
            payloads = {
                "group_id": int(group_id),
                "count": fetch_count,
            }
            result = await event.bot.call_action("get_group_msg_history", **payloads)
            history_messages = result.get("messages", []) if isinstance(result, dict) else []
        except Exception as exc:
            logger.error(f"撤回自身获取消息历史失败: {exc}")
            yield event.plain_result("获取消息历史失败，无法执行撤回。")
            return

        # 筛选机器人自身消息
        bot_self_id = event.get_self_id()
        bot_messages = [
            msg
            for msg in history_messages
            if str(msg.get("sender", {}).get("user_id", "")) == bot_self_id
        ]

        if not bot_messages:
            yield event.plain_result("未找到可撤回的机器人消息。")
            return

        # 按时间倒序排列，取最近的 N 条
        bot_messages.sort(key=lambda item: item.get("time", 0), reverse=True)
        to_recall = bot_messages[:count]

        success = 0
        for msg in to_recall:
            message_id = msg.get("message_id")
            if not message_id:
                continue
            try:
                await event.bot.delete_msg(message_id=message_id)
                success += 1
            except Exception as exc:
                logger.error(f"撤回机器人消息失败, message_id={message_id}: {exc}")

        yield event.plain_result(f"已尝试撤回机器人最近 {success} 条消息。")
        # 阻止后续 handler 和 LLM 处理
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("批量撤回")
    async def batch_recall_command(self, event: AstrMessageEvent):
        """
        批量撤回最近的消息:批量撤回 @用户 撤回数量 (撤回指定用户消息)
        批量撤回最近的消息:批量撤回 撤回数量 (倒序撤回消息)
        """
        if not isinstance(event, AiocqhttpMessageEvent):
            yield event.plain_result("当前平台不支持批量撤回，仅支持 QQ 协议端。")
            return

        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("当前仅支持群聊批量撤回。")
            return

        messages = event.get_messages()
        target_qq = None
        for segment in messages:
            if isinstance(segment, At) and str(segment.qq) != "all":
                target_qq = str(segment.qq)
                break

        plain_parts: list[str] = []
        for segment in messages:
            if isinstance(segment, Plain):
                plain_parts.append(segment.text)
        full_text = "".join(plain_parts).strip()

        tail = full_text
        if "批量撤回" in tail:
            tail = tail.split("批量撤回", 1)[1]
        nums = re.findall(r"\d+", tail)
        if not nums:
            yield event.plain_result("请在指令后填写需要撤回的数量，例如：批量撤回 5")
            return

        count = int(nums[-1])
        if count <= 0:
            yield event.plain_result("撤回数量必须为正整数。")
            return

        max_count = int(self.conf.get("batch_max_count", 20))
        if count > max_count:
            count = max_count

        try:
            fetch_count = min(max_count * 3, 100)
            payloads = {
                "group_id": int(group_id),
                "count": fetch_count,
            }
            result = await event.bot.call_action("get_group_msg_history", **payloads)
            history_messages = result.get("messages", []) if isinstance(result, dict) else []
        except Exception as exc:
            logger.error(f"批量撤回获取消息历史失败: {exc}")
            yield event.plain_result("获取消息历史失败，无法执行批量撤回。")
            return

        if target_qq:
            filtered_messages = [
                msg
                for msg in history_messages
                if str(msg.get("sender", {}).get("user_id", "")) == str(target_qq)
            ]
        else:
            filtered_messages = history_messages

        if not filtered_messages:
            if target_qq:
                yield event.plain_result("未找到可撤回的目标用户消息。")
            else:
                yield event.plain_result("未找到可撤回的机器人消息。")
            return

        filtered_messages.sort(key=lambda item: item.get("time", 0), reverse=True)
        to_recall = filtered_messages[:count]

        success = 0
        for msg in to_recall:
            message_id = msg.get("message_id")
            if not message_id:
                continue
            try:
                await event.bot.delete_msg(message_id=message_id)
                success += 1
            except Exception as exc:
                logger.error(f"批量撤回消息失败, message_id={message_id}: {exc}")

        if target_qq:
            yield event.plain_result(f"已尝试撤回 {success} 条该用户的最近消息。")
        else:
            yield event.plain_result(f"已尝试撤回最近 {success} 条群消息。")
        # 阻止后续 handler 和 LLM 处理
        event.stop_event()
