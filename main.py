"""
批量撤回插件 - 支持自动撤回、按数量批量撤回、按编号撤回、引用撤回等功能
编号规则：最新消息为1号，依次递增，记录所有群消息（包含群友和机器人）
指令回复消息（消息列表、撤回结果通知等）记录到历史但不自动撤回
用户发送的指令消息本身不记录到历史（避免编号偏移），可通过引用撤回
按编号撤回时，以用户发送撤回指令前的消息快照为准，之后产生的消息不影响编号映射
支持任意命令前缀（/、.、!等），参数提取优先使用AstrBot框架解析后的消息段
"""
import asyncio
import re
import time
from typing import Optional

from aiocqhttp.exceptions import ActionFailed

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import At, Plain, Reply
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


class BatchRecall(Star):
    """批量撤回插件主类"""

    def __init__(self, context: Context, config):
        super().__init__(context)
        self.conf = config
        self.recall_tasks = set()
        # 消息历史记录：{session_id: [(message_id, content_preview, timestamp, sender_id, sender_name, is_bot), ...]}
        # 最新消息在列表最前面，编号从1开始
        self.message_history: dict[str, list[tuple[int, str, int, str, str, bool]]] = {}
        self.max_history = self.conf.get("max_message_history", 50)
        # 不自动撤回计数器：标记下N条机器人消息是指令回复，需要记录但不自动撤回
        self._no_auto_recall_count = 0
        logger.info(f"批量撤回插件已加载，自动撤回时间: {self.conf['recall_time']}秒，消息历史记录: {self.max_history}条")

    def _get_session_id(self, event) -> str:
        """获取会话ID，群聊用group_id，私聊用user_id"""
        is_group = bool(event.get_group_id())
        return str(event.get_group_id()) if is_group else str(event.get_sender_id())

    def _add_message_to_history(
        self,
        session_id: str,
        message_id: int,
        content: str,
        sender_id: str,
        sender_name: str,
        is_bot: bool = False,
        msg_time: Optional[int] = None,
    ):
        """添加消息到历史记录，保持最新消息在最前面（编号1为最新）"""
        if session_id not in self.message_history:
            self.message_history[session_id] = []

        if msg_time is None:
            msg_time = int(time.time())

        # 检查是否已存在此消息ID（避免重复）
        for existing in self.message_history[session_id]:
            if existing[0] == message_id:
                return

        # 插入到最前面（最新消息编号为1）
        self.message_history[session_id].insert(
            0, (message_id, content[:100], msg_time, sender_id, sender_name, is_bot)
        )

        # 限制历史记录长度
        if len(self.message_history[session_id]) > self.max_history:
            self.message_history[session_id] = self.message_history[session_id][:self.max_history]

    def _remove_message_from_history(self, session_id: str, message_id: int):
        """从历史记录中移除已撤回的消息"""
        if session_id in self.message_history:
            self.message_history[session_id] = [
                msg for msg in self.message_history[session_id] if msg[0] != message_id
            ]

    def _extract_message_content_from_segments(self, segments: list) -> str:
        """从OneBot消息段列表中提取纯文本内容预览"""
        content_parts = []
        for msg in segments:
            if isinstance(msg, dict):
                msg_type = msg.get("type")
                if msg_type == "text":
                    text = msg.get("data", {}).get("text", "").strip()
                    if text:
                        content_parts.append(text)
                elif msg_type == "image":
                    content_parts.append("[图片]")
                elif msg_type == "face":
                    content_parts.append("[表情]")
                elif msg_type == "at":
                    content_parts.append("[@]")
                elif msg_type == "reply":
                    content_parts.append("[引用]")
        content = "".join(content_parts)
        return content[:50] if content else "[无文本内容]"

    def _remove_task(self, task: asyncio.Task):
        """移除已完成的任务"""
        self.recall_tasks.discard(task)

    def _mark_no_auto_recall(self):
        """标记下一条机器人消息为指令回复：正常发送和记录，但不自动撤回"""
        self._no_auto_recall_count += 1

    def _strip_command_prefix(self, text: str) -> str:
        """剥离开头的命令前缀符号（/、.、!、#等，包括中英文全半角符号），返回去除前缀后的文本"""
        return text.lstrip("/.!#$%^&*~-+=?，。、！!＠@＃#＄$％%＾^＆&＊*～~｀`｜|＼\\ 　\t")

    def _extract_command_tail(self, raw_text: str, cmd_names: tuple[str, ...]) -> str:
        """
        从原始文本中提取命令名之后的参数部分。
        raw_text: 用户发送的原始文本（可能包含命令前缀如/.!等）
        cmd_names: 可能的命令名元组，按长度降序排列以优先匹配长命令名
        返回命令名之后的参数文本（已strip），如果不是本命令返回空字符串
        """
        if not raw_text:
            return ""
        stripped = self._strip_command_prefix(raw_text)
        if not stripped:
            return ""
        # 按长度降序排列，避免短命令名先匹配（如"撤回"先于"撤回自身"）
        sorted_cmds = sorted(cmd_names, key=len, reverse=True)
        for cmd in sorted_cmds:
            if stripped.startswith(cmd):
                return stripped[len(cmd):].strip()
        return ""

    def _extract_text_from_plain_segments(self, event) -> str:
        """从消息段的 Plain 组件中提取并拼接纯文本"""
        plain_parts: list[str] = []
        for segment in event.get_messages():
            if isinstance(segment, Plain):
                plain_parts.append(segment.text)
        return "".join(plain_parts).strip()

    def _is_plugin_command(self, text: str) -> bool:
        """
        判断文本是否是本插件的指令消息（用户发送的）
        指令消息不记录到历史，避免编号偏移
        支持任意命令前缀（/、.、!、#等），通过剥离开头所有非字母数字字符来识别
        """
        t = text.strip()
        if not t:
            return False
        stripped = self._strip_command_prefix(t)
        if not stripped:
            return False
        # 精确匹配指令前缀（指令词后必须是空格、数字、逗号、@或结尾，避免误过滤普通聊天）
        cmd_prefixes = ("批量撤回", "消息列表", "撤回自身", "test_recall", "recall_config")
        for prefix in cmd_prefixes:
            if stripped.startswith(prefix):
                after = stripped[len(prefix):]
                if after == "" or after[0] in (" ", "\t", ",", "，", "@", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"):
                    return True
        # 单独的"撤回"指令（用于引用撤回，精确匹配）
        if stripped in ("撤回",):
            return True
        return False

    async def _recall_msg(self, client, message_id: int, session_id: Optional[str] = None):
        """自动撤回消息"""
        recall_time = self.conf["recall_time"]
        logger.info(f"⏰ 等待 {recall_time} 秒后撤回消息 {message_id}")

        await asyncio.sleep(recall_time)
        try:
            if message_id and message_id != 0:
                await client.delete_msg(message_id=message_id)
                logger.info(f"✅ 已自动撤回消息: {message_id}")
                if session_id:
                    self._remove_message_from_history(session_id, message_id)
        except ActionFailed as e:
            if getattr(e, "retcode", None) == 1200:
                logger.info(
                    f"撤回消息可能已超时或被撤回，message_id={message_id}, retcode={e.retcode}",
                )
                if session_id:
                    self._remove_message_from_history(session_id, message_id)
                return
            logger.error(f"撤回消息失败: {e}")
        except Exception as e:
            logger.error(f"撤回消息失败: {e}")

    def _should_enable_recall(self, event: AstrMessageEvent) -> bool:
        """判断是否应该启用自动撤回"""
        if not event.get_group_id():
            if event.is_admin():
                return self.conf.get("enable_admin_private_recall", False)
            return self.conf.get("enable_private_recall", True)

        group_id = event.get_group_id()
        group_whitelist = self.conf.get("group_whitelist", [])
        if group_whitelist and str(group_id) not in group_whitelist:
            return False
        return self.conf.get("enable_group_recall", True)

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有收到的消息，记录到历史中；同时处理引用撤回的fallback"""
        try:
            if not isinstance(event, AiocqhttpMessageEvent):
                return

            # 只记录群消息
            if not event.get_group_id():
                return

            # 提前获取 raw_event，用于 fallback 中的权限检查
            raw_event = getattr(event.message_obj, "raw_message", None)

            # ===== 引用撤回 fallback：当 @filter.command("撤回") 无法匹配带Reply段的消息时 =====
            # 检测消息是否包含 Reply 段 + 文本为"撤回"（可能带命令前缀）
            messages = event.get_messages()
            reply_id = None
            has_recall_text = False
            for segment in messages:
                if isinstance(segment, Reply):
                    reply_id = int(segment.id)
                elif isinstance(segment, Plain):
                    # 剥离开头的命令前缀符号后检查是否为"撤回"
                    stripped = segment.text.strip().lstrip("/.!#$%^&*~-+=?，。、 ")
                    if stripped == "撤回":
                        has_recall_text = True

            if reply_id and has_recall_text:
                logger.info(f"[DEBUG-撤回-fallback] 检测到引用撤回, reply_id={reply_id}")
                # 权限检查：优先用 event.is_admin()，回退用 raw_event 中的 role 字段
                is_admin = False
                if hasattr(event, "is_admin"):
                    try:
                        is_admin = event.is_admin()
                    except Exception:
                        pass
                if not is_admin:
                    # 回退：从 raw_event 的 sender.role 判断
                    if raw_event:
                        role = raw_event.get("sender", {}).get("role", "")
                        is_admin = role in ("owner", "admin")
                if not is_admin:
                    self._mark_no_auto_recall()
                    yield event.plain_result("撤回命令仅管理员可使用。")
                    return
                session_id = self._get_session_id(event)
                ok, err = await self._do_recall(event.bot, reply_id)
                self._remove_message_from_history(session_id, reply_id)
                if self.conf.get("enable_recall_notification", True):
                    self._mark_no_auto_recall()
                    if ok:
                        logger.info(f"[DEBUG-撤回-fallback] 撤回成功, reply_id={reply_id}")
                        yield event.plain_result("✅ 已撤回引用的消息。")
                    else:
                        logger.info(f"[DEBUG-撤回-fallback] 撤回失败: {err}")
                        yield event.plain_result(f"❌ {err}\n提示：机器人需要管理员权限才能撤回他人消息，且消息需在2分钟内")
                event.stop_event()
                return
            # ===== 引用撤回 fallback 结束 =====

            session_id = self._get_session_id(event)
            if not raw_event:
                return

            message_id = raw_event.get("message_id")
            if not message_id:
                return

            # 提取消息纯文本
            raw_message = raw_event.get("message", [])
            content = self._extract_message_content_from_segments(raw_message)

            sender_id = str(raw_event.get("sender", {}).get("user_id", ""))
            bot_self_id = str(event.get_self_id())
            is_bot = (sender_id == bot_self_id)

            # 用户发的插件指令消息不记录（避免编号偏移）
            # bot自己发的消息由intercept_and_recall记录，这里如果收到（某些协议端可能推送）也跳过避免重复
            if not is_bot and self._is_plugin_command(content):
                return

            sender_name = raw_event.get("sender", {}).get("card") or raw_event.get("sender", {}).get("nickname", "未知")
            msg_time = raw_event.get("time", int(time.time()))

            self._add_message_to_history(
                session_id, int(message_id), content, sender_id, sender_name, is_bot, int(msg_time)
            )
        except Exception as e:
            logger.error(f"记录消息到历史失败: {e}")

    @filter.on_decorating_result(priority=999)
    async def intercept_and_recall(self, event: AstrMessageEvent):
        """
        拦截机器人发出的消息，手动发送、记录到历史，并按需安排自动撤回
        指令回复消息（计数器>0时）正常记录但不自动撤回
        """
        try:
            if not isinstance(event, AiocqhttpMessageEvent):
                return

            result = event.get_result()
            if not result or not result.chain:
                return

            # 判断是否是指令回复（不自动撤回）
            is_command_reply = self._no_auto_recall_count > 0
            if is_command_reply:
                self._no_auto_recall_count -= 1

            original_chain = result.chain.copy()
            result.chain.clear()
            message_chain = MessageChain(chain=original_chain)
            onebot_messages = await AiocqhttpMessageEvent._parse_onebot_json(
                message_chain,
            )
            if not onebot_messages:
                return

            is_group = bool(event.get_group_id())
            session_id = self._get_session_id(event)
            bot_self_id = str(event.get_self_id())

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

            message_id = int(message_id)
            logger.info(f"📤 发送成功，获取到消息ID: {message_id}")

            # 记录机器人消息到历史（on_message可能也会记录，这里通过message_id去重）
            content_preview = self._extract_message_content_from_segments(onebot_messages)
            bot_name = event.get_self_name() if hasattr(event, 'get_self_name') else "机器人"
            self._add_message_to_history(
                session_id, message_id, content_preview, bot_self_id, bot_name, is_bot=True
            )

            # 指令回复不自动撤回；普通消息按配置决定是否自动撤回
            if not is_command_reply and self._should_enable_recall(event):
                recall_time = self.conf["recall_time"]
                logger.info(f"🎯 拦截到机器人消息，{recall_time}秒后撤回")
                task = asyncio.create_task(self._recall_msg(event.bot, message_id, session_id))
                task.add_done_callback(self._remove_task)
                self.recall_tasks.add(task)
                logger.info(f"✅ 已安排消息在 {recall_time} 秒后撤回")

        except Exception as e:
            logger.error(f"消息拦截处理失败: {e}")

    async def _do_recall(self, bot, message_id: int) -> tuple[bool, str]:
        """执行撤回操作，返回(是否成功, 错误信息)"""
        try:
            await bot.delete_msg(message_id=message_id)
            return True, ""
        except ActionFailed as e:
            if getattr(e, "retcode", None) == 1200:
                return False, "消息已撤回或超时"
            return False, f"撤回失败(retcode={e.retcode})"
        except Exception as e:
            return False, f"撤回失败: {str(e)}"

    @filter.command("test_recall")
    async def test_recall_command(self, event: AstrMessageEvent):
        """测试撤回功能（此测试消息会被记录并自动撤回，不标记为指令回复）"""
        recall_time = self.conf["recall_time"]
        yield event.plain_result(f"🧪 测试消息，{recall_time}秒后此消息将会撤回...")

    @filter.command("recall_config")
    async def recall_config_command(self, event: AstrMessageEvent):
        """查看当前配置"""
        self._mark_no_auto_recall()
        config_info = "📋 当前撤回配置:\n"
        config_info += f"撤回时间: {self.conf['recall_time']}秒\n"
        config_info += f"私聊启用: {self.conf.get('enable_private_recall', True)}\n"
        config_info += f"群聊启用: {self.conf.get('enable_group_recall', True)}\n"
        config_info += f"撤回结果通知: {self.conf.get('enable_recall_notification', True)}\n"
        config_info += f"消息历史记录: {self.conf.get('max_message_history', 50)}条\n"
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
        logger.info("批量撤回插件已卸载")

    async def _load_history_from_api(self, event: AiocqhttpMessageEvent, session_id: str):
        """从API加载历史消息（当本地历史为空时使用）"""
        is_group = bool(event.get_group_id())
        if not is_group:
            return

        try:
            fetch_count = min(self.max_history * 2, 100)
            payloads = {
                "group_id": int(session_id),
                "count": fetch_count,
            }
            result = await event.bot.call_action("get_group_msg_history", **payloads)
            history_messages = result.get("messages", []) if isinstance(result, dict) else []

            # 按时间倒序排列（最新在前）
            history_messages.sort(key=lambda item: item.get("time", 0), reverse=True)

            bot_self_id = str(event.get_self_id())
            history_list = []
            for msg in history_messages[:self.max_history]:
                msg_id = msg.get("message_id")
                if not msg_id:
                    continue
                sender_info = msg.get("sender", {})
                sender_id = str(sender_info.get("user_id", ""))
                # 跳过API返回的历史中用户发送的指令消息（避免把历史中的"批量撤回"等指令也编号）
                raw_msg = msg.get("message", [])
                if isinstance(raw_msg, list):
                    content = self._extract_message_content_from_segments(raw_msg)
                    if sender_id != bot_self_id and self._is_plugin_command(content):
                        continue
                else:
                    content = str(raw_msg)[:50]

                sender_name = sender_info.get("card") or sender_info.get("nickname", "未知")
                msg_time = msg.get("time", int(time.time()))
                is_bot = (sender_id == bot_self_id)

                history_list.append((int(msg_id), content, int(msg_time), sender_id, sender_name, is_bot))

            self.message_history[session_id] = history_list
        except Exception as exc:
            logger.error(f"从API获取消息历史失败: {exc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("消息列表")
    async def message_list_command(self, event: AstrMessageEvent):
        """
        显示最近消息列表:消息列表 [显示数量]
        包含所有群成员的消息，最新消息编号为1
        本消息列表自身将占据编号1，历史消息从编号2开始
        """
        if not isinstance(event, AiocqhttpMessageEvent):
            self._mark_no_auto_recall()
            yield event.plain_result("当前平台不支持此功能，仅支持 QQ 协议端。")
            return

        session_id = self._get_session_id(event)
        is_group = bool(event.get_group_id())
        if not is_group:
            self._mark_no_auto_recall()
            yield event.plain_result("当前仅支持群聊中查看消息列表。")
            return

        # 获取当前指令消息的ID，用于排除（用户发的指令消息不编号）
        current_msg_id = None
        raw_event = getattr(event.message_obj, "raw_message", None)
        if raw_event:
            current_msg_id = raw_event.get("message_id")

        # 参数提取：从 Plain 消息段拼接文本，回退使用 message_str
        plain_parts: list[str] = []
        for segment in event.get_messages():
            if isinstance(segment, Plain):
                plain_parts.append(segment.text)
        raw_text_from_plain = "".join(plain_parts).strip()
        raw_text_from_msg_str = event.get_message_str().strip()
        
        # 优先使用内容更长的那个（说明提取更完整）
        if len(raw_text_from_plain) >= len(raw_text_from_msg_str):
            raw_text = raw_text_from_plain
        else:
            raw_text = raw_text_from_msg_str
        
        tail = self._extract_command_tail(raw_text, ("消息列表",))

        nums = re.findall(r"\d+", tail)
        show_count = int(nums[0]) if nums else 10
        # 预留1个位置给本消息列表自身
        show_count = min(show_count, self.max_history - 1)

        # 加载历史，排除当前用户指令消息
        history = self.message_history.get(session_id, [])
        if not history:
            await self._load_history_from_api(event, session_id)
            history = self.message_history.get(session_id, [])
        if current_msg_id and history:
            history = [m for m in history if m[0] != int(current_msg_id)]

        if not history:
            self._mark_no_auto_recall()
            yield event.plain_result("当前没有可显示的消息记录。请先发几条消息后再试。")
            return

        # 历史消息从编号2开始显示（编号1预留给本消息列表）
        display_msgs = history[:show_count]
        total = len(history) + 1  # +1是本消息列表自身
        list_text = f"📋 最近消息 (共{total}条，显示{len(display_msgs) + 1}条):\n"
        list_text += "格式: [编号] 发送者: 内容\n"
        list_text += "─" * 25 + "\n"

        # 编号1是本消息列表自身
        bot_name = event.get_self_name() if hasattr(event, 'get_self_name') else "机器人"
        list_text += f"[1] 🤖 {bot_name}: (本消息列表)\n"

        # 历史消息从编号2开始
        for offset, (msg_id, content, msg_time, sender_id, sender_name, is_bot) in enumerate(display_msgs, 2):
            name_prefix = "🤖 " if is_bot else ""
            display_name = sender_name[:8] + ".." if len(sender_name) > 8 else sender_name
            list_text += f"[{offset}] {name_prefix}{display_name}: {content}\n"

        list_text += "─" * 25 + "\n"
        list_text += "💡 使用方法:\n"
        list_text += "• 批量撤回 1 3 5 - 撤回指定编号消息\n"
        list_text += "• 批量撤回 1,3,5 - 逗号分隔也支持\n"
        list_text += "• 引用消息+撤回 - 撤回被引用的消息\n"
        list_text += "• 批量撤回 5 - 撤回最近5条消息(兼容旧版)\n"
        list_text += "• 撤回自身 5 - 仅撤回机器人最近5条"

        self._mark_no_auto_recall()
        yield event.plain_result(list_text)
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("撤回自身")
    async def recall_bot_messages_command(self, event: AstrMessageEvent):
        """
        撤回机器人自身发送的消息:撤回自身 撤回数量
        """
        if not isinstance(event, AiocqhttpMessageEvent):
            self._mark_no_auto_recall()
            yield event.plain_result("当前平台不支持撤回自身消息，仅支持 QQ 协议端。")
            return

        group_id = event.get_group_id()
        if not group_id:
            self._mark_no_auto_recall()
            yield event.plain_result("当前仅支持群聊中撤回机器人自身消息。")
            return

        # 获取当前指令消息ID
        current_msg_id = None
        raw_event_local = getattr(event.message_obj, "raw_message", None)
        if raw_event_local:
            current_msg_id = raw_event_local.get("message_id")

        # 参数提取：从 Plain 消息段拼接文本，回退使用 message_str
        plain_parts: list[str] = []
        for segment in event.get_messages():
            if isinstance(segment, Plain):
                plain_parts.append(segment.text)
        raw_text_from_plain = "".join(plain_parts).strip()
        raw_text_from_msg_str = event.get_message_str().strip()
        
        if len(raw_text_from_plain) >= len(raw_text_from_msg_str):
            raw_text = raw_text_from_plain
        else:
            raw_text = raw_text_from_msg_str
        
        tail = self._extract_command_tail(raw_text, ("撤回自身",))

        nums = re.findall(r"\d+", tail)
        if not nums:
            self._mark_no_auto_recall()
            yield event.plain_result("请在指令后填写需要撤回的数量，例如：撤回自身 5")
            return

        count = int(nums[-1])
        if count <= 0:
            self._mark_no_auto_recall()
            yield event.plain_result("撤回数量必须为正整数。")
            return

        max_count = int(self.conf.get("batch_max_count", 20))
        if count > max_count:
            count = max_count

        session_id = self._get_session_id(event)

        # 获取发送指令前的消息快照（优先使用本地历史中的机器人消息）
        history = self.message_history.get(session_id, [])
        if not history:
            await self._load_history_from_api(event, session_id)
            history = self.message_history.get(session_id, [])
        # 创建快照，避免并发修改影响
        if current_msg_id and history:
            history = [m for m in history if m[0] != int(current_msg_id)]
        else:
            history = list(history)

        bot_messages = [msg for msg in history if msg[5]]  # is_bot=True

        if not bot_messages:
            try:
                fetch_count = min(max_count * 3, 100)
                payloads = {
                    "group_id": int(group_id),
                    "count": fetch_count,
                }
                result = await event.bot.call_action("get_group_msg_history", **payloads)
                history_messages = result.get("messages", []) if isinstance(result, dict) else []
                bot_self_id = str(event.get_self_id())
                bot_messages_raw = [
                    msg for msg in history_messages
                    if str(msg.get("sender", {}).get("user_id", "")) == bot_self_id
                ]
                bot_messages_raw.sort(key=lambda item: item.get("time", 0), reverse=True)
                bot_messages = []
                for msg in bot_messages_raw:
                    msg_id = msg.get("message_id")
                    if msg_id:
                        bot_messages.append((int(msg_id), "", 0, bot_self_id, "机器人", True))
            except Exception as exc:
                logger.error(f"撤回自身获取消息历史失败: {exc}")
                self._mark_no_auto_recall()
                yield event.plain_result("获取消息历史失败，无法执行撤回。")
                return

        if not bot_messages:
            if self.conf.get("enable_recall_notification", True):
                self._mark_no_auto_recall()
                yield event.plain_result("未找到可撤回的机器人消息。")
            return

        success = 0
        failed_msgs = []
        for msg in bot_messages:
            if success >= count:
                break
            message_id = msg[0]
            if not message_id:
                continue
            ok, err = await self._do_recall(event.bot, message_id)
            if ok:
                success += 1
                self._remove_message_from_history(session_id, message_id)
            else:
                if "已撤回" not in err:
                    failed_msgs.append(f"{message_id}({err})")
                else:
                    self._remove_message_from_history(session_id, message_id)

        if self.conf.get("enable_recall_notification", True):
            result_text = f"已尝试撤回机器人最近 {success} 条消息。"
            if failed_msgs:
                result_text += f"\n失败: {', '.join(failed_msgs[:5])}"
            self._mark_no_auto_recall()
            yield event.plain_result(result_text)
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("批量撤回")
    async def batch_recall_command(self, event: AstrMessageEvent):
        """
        批量撤回消息:
        - 批量撤回 1 3 4 9 (按编号撤回指定消息)
        - 批量撤回 1,3,4,9 (逗号分隔也支持)
        - 批量撤回 @用户 1 3 (撤回指定用户的指定编号消息)
        - 批量撤回 5 (撤回最近5条消息，兼容旧版)
        - 引用消息+批量撤回 (撤回被引用的消息)
        """
        if not isinstance(event, AiocqhttpMessageEvent):
            self._mark_no_auto_recall()
            yield event.plain_result("当前平台不支持批量撤回，仅支持 QQ 协议端。")
            return

        session_id = self._get_session_id(event)
        is_group = bool(event.get_group_id())
        if not is_group:
            self._mark_no_auto_recall()
            yield event.plain_result("当前仅支持群聊批量撤回。")
            return

        messages = event.get_messages()
        target_qq = None
        reply_id = None
        current_msg_id = None

        # 获取当前指令消息的原始ID
        raw_event = getattr(event.message_obj, "raw_message", None)
        if raw_event:
            current_msg_id = raw_event.get("message_id")

        # 从消息段中提取@和引用（这些结构化数据从event.get_messages()获取最可靠）
        for segment in messages:
            if isinstance(segment, At) and str(segment.qq) != "all":
                target_qq = str(segment.qq)
            elif isinstance(segment, Reply):
                reply_id = int(segment.id)

        # 参数提取：从 Plain 消息段拼接文本，回退使用 message_str
        plain_parts: list[str] = []
        for segment in messages:
            if isinstance(segment, Plain):
                plain_parts.append(segment.text)
        raw_text_from_plain = "".join(plain_parts).strip()
        raw_text_from_msg_str = event.get_message_str().strip()
        
        logger.info(f"[DEBUG-批量撤回] Plain segments ({len(plain_parts)}): {plain_parts!r}")
        logger.info(f"[DEBUG-批量撤回] message_str: {raw_text_from_msg_str!r}")
        
        # 优先使用内容更长的那个（说明提取更完整）
        if len(raw_text_from_plain) >= len(raw_text_from_msg_str):
            raw_text = raw_text_from_plain
        else:
            raw_text = raw_text_from_msg_str
        
        logger.info(f"[DEBUG-批量撤回] 使用的raw_text: {raw_text!r}")
        
        # 剥离开头的命令前缀符号和命令名
        tail = self._extract_command_tail(raw_text, ("批量撤回",))
        logger.info(f"[DEBUG-批量撤回] 提取的参数tail: {tail!r}")

        # 直接用正则提取所有数字
        all_numbers = [int(x) for x in re.findall(r"\d+", tail)]
        logger.info(f"[DEBUG-批量撤回] 提取到的数字: {all_numbers!r}")
        
        # 如果从 Plain 段提取不到数字，尝试从 message_str 提取
        if not all_numbers:
            tail_from_msg_str = self._extract_command_tail(raw_text_from_msg_str, ("批量撤回",))
            all_numbers_from_msg_str = [int(x) for x in re.findall(r"\d+", tail_from_msg_str)]
            logger.info(f"[DEBUG-批量撤回] Fallback到message_str: tail={tail_from_msg_str!r}, numbers={all_numbers_from_msg_str!r}")
            if all_numbers_from_msg_str:
                tail = tail_from_msg_str
                all_numbers = all_numbers_from_msg_str

        # 有引用消息时优先处理引用撤回
        if reply_id:
            ok, err = await self._do_recall(event.bot, reply_id)
            self._remove_message_from_history(session_id, reply_id)
            if self.conf.get("enable_recall_notification", True):
                self._mark_no_auto_recall()
                if ok:
                    yield event.plain_result(f"✅ 已撤回引用的消息。")
                else:
                    yield event.plain_result(f"❌ {err}")
            event.stop_event()
            return

        if not all_numbers:
            self._mark_no_auto_recall()
            yield event.plain_result(
                "使用方法:\n"
                "• 批量撤回 1 3 5 - 按编号撤回指定消息\n"
                "• 批量撤回 1,3,5 - 逗号分隔也支持\n"
                "• 批量撤回 @用户 1 3 - 撤回指定用户的消息\n"
                "• 批量撤回 5 - 撤回最近5条消息\n"
                "• 引用消息+批量撤回 - 撤回被引用的消息\n"
                "发送「消息列表」可查看消息编号"
            )
            return

        max_count = int(self.conf.get("batch_max_count", 20))

        # 辅助函数：获取发送撤回指令之前的消息快照（排除当前用户指令消息）
        # 返回的是一个全新列表副本，后续即使有新消息进来也不会影响此快照的索引
        async def get_history_snapshot():
            hist = self.message_history.get(session_id, [])
            if not hist:
                await self._load_history_from_api(event, session_id)
                hist = self.message_history.get(session_id, [])
            # 始终创建浅拷贝，避免返回self.message_history的引用导致并发修改
            if current_msg_id and hist:
                snapshot = [m for m in hist if m[0] != int(current_msg_id)]
            else:
                snapshot = list(hist)
            return snapshot

        # 判断模式：
        # - 参数中有逗号 -> 按编号撤回
        # - 数字个数 >= 2 -> 按编号撤回（无论空格还是逗号分隔，多个数字就是多个编号）
        # - 参数中有空格分隔（说明有多个参数段） -> 按编号撤回
        # - 只有1个数字，无逗号无空格，无@ -> 按数量撤回（兼容旧版，撤回最近N条）
        has_comma = ("," in tail) or ("，" in tail)
        has_space_sep = bool(re.search(r"\d+\s+\d+", tail))  # 数字之间有空白分隔 -> 多个编号
        is_by_count = (
            len(all_numbers) == 1
            and not has_comma
            and not has_space_sep
            and not target_qq
        )
        
        logger.info(f"[DEBUG-批量撤回] 模式判断: len(all_numbers)={len(all_numbers)}, has_comma={has_comma}, has_space_sep={has_space_sep}, target_qq={target_qq}")
        logger.info(f"[DEBUG-批量撤回] is_by_count={is_by_count}")

        if is_by_count:
            logger.info(f"[DEBUG-批量撤回] 进入按数量撤回模式")
            # 按数量撤回模式（兼容旧版）：可以重新加载以获取更多历史
            count = all_numbers[0]
            if count <= 0:
                self._mark_no_auto_recall()
                yield event.plain_result("撤回数量必须为正整数。")
                return
            if count > max_count:
                count = max_count

            history = await get_history_snapshot()

            if target_qq:
                filtered_messages = [msg for msg in history if msg[3] == str(target_qq)]
            else:
                filtered_messages = history

            if len(filtered_messages) < count:
                await self._load_history_from_api(event, session_id)
                history = await get_history_snapshot()
                if target_qq:
                    filtered_messages = [msg for msg in history if msg[3] == str(target_qq)]
                else:
                    filtered_messages = history

            if not filtered_messages:
                if self.conf.get("enable_recall_notification", True):
                    self._mark_no_auto_recall()
                    yield event.plain_result("未找到可撤回的消息。")
                return

            success = 0
            failed_msgs = []
            for msg in filtered_messages:
                if success >= count:
                    break
                message_id = msg[0]
                if not message_id:
                    continue
                ok, err = await self._do_recall(event.bot, message_id)
                if ok:
                    success += 1
                    self._remove_message_from_history(session_id, message_id)
                else:
                    if "已撤回" not in err:
                        failed_msgs.append(f"{message_id}({err})")
                    else:
                        self._remove_message_from_history(session_id, message_id)

            if self.conf.get("enable_recall_notification", True):
                result_text = f"已尝试撤回最近 {success} 条消息。"
                if failed_msgs:
                    result_text += f"\n失败: {', '.join(failed_msgs[:5])}"
                self._mark_no_auto_recall()
                yield event.plain_result(result_text)
            event.stop_event()
            return

        else:
            logger.info(f"[DEBUG-批量撤回] 进入按编号撤回模式, numbers={sorted(list(set(all_numbers)))}")
            # 按编号撤回模式：基于发送撤回指令前的消息快照进行编号映射
            # 只获取一次快照，不重新加载，确保编号与用户操作时一致
            numbers = sorted(list(set(all_numbers)))  # 去重并排序
            if len(numbers) > max_count:
                numbers = numbers[:max_count]

            history = await get_history_snapshot()
            logger.info(f"[DEBUG-批量撤回] 历史消息数量: {len(history)}")

            if target_qq:
                # 按指定用户筛选（基于快照，不重新加载）
                target_messages = [msg for msg in history if msg[3] == str(target_qq)]

                success = 0
                failed_msgs = []
                invalid_nums = []
                for num in numbers:
                    idx = num - 1
                    if 0 <= idx < len(target_messages):
                        msg_id = target_messages[idx][0]
                        ok, err = await self._do_recall(event.bot, msg_id)
                        if ok:
                            success += 1
                            self._remove_message_from_history(session_id, msg_id)
                        else:
                            if "已撤回" not in err:
                                failed_msgs.append(f"[{num}]{err}")
                            else:
                                self._remove_message_from_history(session_id, msg_id)
                    else:
                        invalid_nums.append(str(num))

                if self.conf.get("enable_recall_notification", True):
                    result_text = f"✅ 已成功撤回指定用户的 {success} 条消息。"
                    if invalid_nums:
                        result_text += f"\n❌ 无效编号: {', '.join(invalid_nums)}"
                    if failed_msgs:
                        result_text += f"\n⚠️ 失败: {', '.join(failed_msgs[:5])}"
                    self._mark_no_auto_recall()
                    yield event.plain_result(result_text)
                event.stop_event()
                return

            # 按编号撤回消息（所有消息），基于快照
            if not history:
                if self.conf.get("enable_recall_notification", True):
                    self._mark_no_auto_recall()
                    yield event.plain_result("没有可撤回的消息记录，请先发送「消息列表」查看。")
                return

            success = 0
            failed_msgs = []
            invalid_nums = []
            recalled_ids = []

            for num in numbers:
                idx = num - 1
                if 0 <= idx < len(history):
                    msg_id = history[idx][0]
                    if msg_id in recalled_ids:
                        continue
                    recalled_ids.append(msg_id)
                    ok, err = await self._do_recall(event.bot, msg_id)
                    if ok:
                        success += 1
                        self._remove_message_from_history(session_id, msg_id)
                    else:
                        if "已撤回" not in err:
                            failed_msgs.append(f"[{num}]{err}")
                        else:
                            self._remove_message_from_history(session_id, msg_id)
                else:
                    invalid_nums.append(str(num))

            if self.conf.get("enable_recall_notification", True):
                valid_nums_str = ', '.join(str(n) for n in numbers if str(n) not in invalid_nums)
                result_text = f"✅ 已成功撤回 {success} 条消息（编号: {valid_nums_str}）。"
                if invalid_nums:
                    result_text += f"\n❌ 无效编号: {', '.join(invalid_nums)}"
                if failed_msgs:
                    result_text += f"\n⚠️ 失败: {', '.join(failed_msgs[:5])}\n提示：机器人需要管理员权限才能撤回他人消息，且消息需在2分钟内"
                self._mark_no_auto_recall()
                yield event.plain_result(result_text)
            event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("撤回")
    async def recall_reply_command(self, event: AstrMessageEvent):
        """
        撤回引用的消息:引用一条消息并发送「撤回」
        """
        logger.info(f"[DEBUG-撤回] 命令被触发")
        
        if not isinstance(event, AiocqhttpMessageEvent):
            self._mark_no_auto_recall()
            yield event.plain_result("当前平台不支持此功能，仅支持 QQ 协议端。")
            return

        messages = event.get_messages()
        reply_id = None
        
        logger.info(f"[DEBUG-撤回] 消息段数量: {len(messages)}")
        for i, segment in enumerate(messages):
            logger.info(f"[DEBUG-撤回] 段[{i}]: type={type(segment).__name__}")

        for segment in messages:
            if isinstance(segment, Reply):
                reply_id = int(segment.id)
                logger.info(f"[DEBUG-撤回] 找到Reply段, reply_id={reply_id}")
                break

        if not reply_id:
            logger.info(f"[DEBUG-撤回] 未找到Reply段，提示使用方法")
            self._mark_no_auto_recall()
            yield event.plain_result(
                "使用方法：引用一条消息，然后发送「撤回」即可撤回该消息。\n"
                "提示：需要管理员权限"
            )
            return

        session_id = self._get_session_id(event)
        ok, err = await self._do_recall(event.bot, reply_id)
        self._remove_message_from_history(session_id, reply_id)

        if self.conf.get("enable_recall_notification", True):
            self._mark_no_auto_recall()
            if ok:
                logger.info(f"[DEBUG-撤回] 撤回成功, reply_id={reply_id}")
                yield event.plain_result("✅ 已撤回引用的消息。")
            else:
                logger.info(f"[DEBUG-撤回] 撤回失败: {err}")
                yield event.plain_result(f"❌ {err}\n提示：机器人需要管理员权限才能撤回他人消息，且消息需在2分钟内")
        event.stop_event()
