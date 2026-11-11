"""Issue #4 回归测试：不依赖 AstrBot / aiocqhttp 运行环境。

用最小 stub 复刻以下链路：
装饰钩子 -> (原生 TTS/装饰) -> RespondStage 的 bot.send_* -> 发送回执。
"""
import asyncio
import functools
import importlib.util
import logging
import sys
import time
import types
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _module(name, **attrs):
    mod = types.ModuleType(name)
    mod.__path__ = []
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


class _ActionFailed(Exception):
    def __init__(self, *args, retcode=None, **kwargs):
        super().__init__(*args)
        self.retcode = retcode


class _Plain:
    def __init__(self, text):
        self.text = text


class _At:
    def __init__(self, qq=None, name=None):
        self.qq = qq
        self.name = name


class _Reply:
    def __init__(self, id=None):
        self.id = id


class _AstrMessageEvent:
    pass


class _AiocqhttpMessageEvent(_AstrMessageEvent):
    pass


class _Star:
    def __init__(self, context=None):
        self.context = context


class _NoopFilter:
    class PlatformAdapterType:
        ALL = "all"

    class PermissionType:
        ADMIN = "admin"

    def __getattr__(self, _name):
        def decorator(*_args, **_kwargs):
            def wrap(func):
                return func

            return wrap

        return decorator


def _install_stubs():
    _module("aiocqhttp")
    _module("aiocqhttp.exceptions", ActionFailed=_ActionFailed)
    _module("astrbot")
    _module("astrbot.api", logger=logging.getLogger("batchrecall-test"))
    _module("astrbot.api.event", AstrMessageEvent=_AstrMessageEvent, filter=_NoopFilter())
    _module("astrbot.api.star", Context=object, Star=_Star, register=lambda *a, **k: None)
    _module("astrbot.core")
    _module("astrbot.core.message")
    _module("astrbot.core.message.components", At=_At, Plain=_Plain, Reply=_Reply)
    _module("astrbot.core.platform")
    _module("astrbot.core.platform.sources")
    _module("astrbot.core.platform.sources.aiocqhttp")
    _module(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event",
        AiocqhttpMessageEvent=_AiocqhttpMessageEvent,
    )


_install_stubs()
_spec = importlib.util.spec_from_file_location("batchrecall_main", PLUGIN_DIR / "main.py")
main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(main)


class _FakeResult:
    def __init__(self, chain):
        self.chain = chain


class _FakeEvent(_AiocqhttpMessageEvent):
    def __init__(self, bot, group_id=123, sender_id=456, self_id=999):
        self.bot = bot
        self._group_id = group_id
        self._sender_id = sender_id
        self._self_id = self_id
        self.result = _FakeResult([_Plain("你好")])

    def get_result(self):
        return self.result

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def get_self_id(self):
        return self._self_id

    def get_self_name(self):
        return "机器人"

    def is_admin(self):
        return False

    async def send(self, message):
        """复刻 RespondStage -> AiocqhttpMessageEvent.send -> bot.send_*。"""
        if self._group_id:
            return await self.bot.send_group_msg(
                group_id=self._group_id, message=message
            )
        return await self.bot.send_private_msg(
            user_id=self._sender_id, message=message
        )


class _FakeBot:
    """复刻 aiocqhttp CQHttp：动态 bot.send_xxx -> self.call_action。"""

    def __init__(self):
        self.calls = []
        self.next_message_id = 1001
        self.fixed_message_id = None
        self.fail_actions = set()

    def __getattr__(self, action):
        if action.startswith("_"):
            raise AttributeError(action)
        return functools.partial(self.call_action, action)

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if action in self.fail_actions:
            raise RuntimeError("send failed")
        if action.endswith("msg_history"):
            return {"messages": []}
        if action in {"send_group_msg", "send_private_msg", "send_msg"}:
            message_id = self.fixed_message_id
            if message_id is None:
                message_id = self.next_message_id
                self.next_message_id += 1
            return {"message_id": message_id}
        return {}


class _FakeEchoEvent(_AiocqhttpMessageEvent):
    """协议端回显的机器人自身消息事件（on_message 路径）。"""

    def __init__(self, bot, group_id, message_id, self_id=999):
        self.bot = bot
        self._group_id = group_id
        self._sender_id = self_id
        self._self_id = self_id
        self.message_obj = types.SimpleNamespace(
            raw_message={
                "message_id": message_id,
                "message": [{"type": "text", "data": {"text": "你好"}}],
                "sender": {"user_id": self_id, "nickname": "机器人"},
                "time": int(time.time()),
            }
        )

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def get_self_id(self):
        return self._self_id

    def get_messages(self):
        return [_Plain("你好")]

    def is_admin(self):
        return False


def _config(**overrides):
    conf = {
        "recall_time": 60,
        "enable_private_recall": True,
        "enable_admin_private_recall": False,
        "enable_group_recall": True,
        "skip_video_recall": False,
        "group_whitelist": [],
        "enable_recall_notification": True,
        "max_message_history": 50,
        "enable_history_window_recall": False,
        "history_scan_lookback_seconds": 0,
    }
    conf.update(overrides)
    return conf


def _text_message(text="你好"):
    return [{"type": "text", "data": {"text": text}}]


class _ErrorCapture(logging.Handler):
    """捕获被插件内部 try/except 吞掉的 ERROR 日志，防止测试假通过。"""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


class SendObserverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._log_capture = _ErrorCapture()
        self._test_logger = logging.getLogger("batchrecall-test")
        self._test_logger.addHandler(self._log_capture)
        self.plugin = main.BatchRecall(object(), _config())
        self.plugin.HISTORY_SETTLE_DELAY = 0
        self.bot = _FakeBot()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self._test_logger.removeHandler(self._log_capture)
        self.assertEqual(
            self._log_capture.records,
            [],
            f"插件内部出现被吞掉的异常: {self._log_capture.records}",
        )

    def _history_ids(self, session_id):
        return [entry[0] for entry in self.plugin.message_history.get(session_id, [])]

    async def test_1_decorating_hook_keeps_chain_and_does_not_send(self):
        event = _FakeEvent(self.bot)
        await self.plugin.intercept_and_recall(event)

        self.assertEqual(len(event.result.chain), 1)
        self.assertIsInstance(event.result.chain[0], _Plain)
        self.assertEqual(self.bot.calls, [])
        self.assertIsNotNone(self.plugin._send_context.get())

    async def test_2_tts_runs_and_only_final_audio_is_recorded(self):
        event = _FakeEvent(self.bot)
        await self.plugin.intercept_and_recall(event)

        audio_requests = []

        async def mini_decorate_stage(ev):
            """复刻上游 ResultDecorateStage：装饰钩子后 chain 非空才执行 TTS。"""
            result = ev.get_result()
            if not result.chain:
                return False
            for comp in result.chain:
                if isinstance(comp, _Plain) and len(comp.text) > 1:
                    audio_requests.append(comp.text)
            result.chain = [{"type": "record"}]
            return True

        self.assertTrue(await mini_decorate_stage(event))
        self.assertEqual(audio_requests, ["你好"])

        # RespondStage 只发送 TTS 后的最终消息一次。
        ret = await self.bot.send_group_msg(
            group_id=123, message=[{"type": "record", "data": {"file": "a.silk"}}]
        )
        message_id = ret["message_id"]

        send_actions = [a for a, _ in self.bot.calls if a == "send_group_msg"]
        self.assertEqual(len(send_actions), 1)
        self.assertEqual(self._history_ids("123"), [message_id])
        self.assertIn(("123", message_id), self.plugin._scheduled_recall_ids)

    async def test_3_observer_is_idempotent_and_restored(self):
        event = _FakeEvent(self.bot)
        await self.plugin.intercept_and_recall(event)
        wrapper = self.bot.call_action
        await self.plugin.intercept_and_recall(_FakeEvent(self.bot))

        self.assertIs(self.bot.call_action, wrapper)
        self.assertEqual(len(self.plugin._send_observers), 1)

        await self.plugin.terminate()
        self.assertNotIn("call_action", self.bot.__dict__)
        self.assertEqual(self.plugin._send_observers, [])
        # 恢复后动态发送仍然可用，且不再写历史。
        ret = await self.bot.send_group_msg(group_id=123, message=_text_message())
        self.assertEqual(ret["message_id"], 1001)
        self.assertEqual(self._history_ids("123"), [])

    async def test_4_failed_send_creates_no_history(self):
        event = _FakeEvent(self.bot)
        await self.plugin.intercept_and_recall(event)
        self.bot.fail_actions.add("send_group_msg")

        with self.assertRaises(RuntimeError):
            await self.bot.send_group_msg(group_id=123, message=_text_message())

        self.assertEqual(self._history_ids("123"), [])
        self.assertEqual(self.plugin._scheduled_recall_ids, {})

    async def test_5_auto_recall_uses_real_receipt_id(self):
        scheduled = []
        original_schedule = self.plugin._schedule_auto_recall

        def spy(bot, message_id, session_id, scheduled_at=None):
            scheduled.append((message_id, session_id))
            return original_schedule(bot, message_id, session_id, scheduled_at)

        self.plugin._schedule_auto_recall = spy
        event = _FakeEvent(self.bot)
        await self.plugin.intercept_and_recall(event)
        ret = await self.bot.send_group_msg(group_id=123, message=_text_message())
        message_id = ret["message_id"]

        # 自动撤回调度用的就是原生发送回执里的 ID。
        self.assertIn((message_id, "123"), scheduled)
        self.assertIn(("123", message_id), self.plugin._scheduled_recall_ids)
        self.assertIn(message_id, self._history_ids("123"))

    async def test_6_concurrent_sessions_are_isolated(self):
        async def flow(group_id):
            event = _FakeEvent(self.bot, group_id=group_id)
            await self.plugin.intercept_and_recall(event)
            await asyncio.sleep(0)
            ret = await self.bot.send_group_msg(
                group_id=group_id, message=_text_message()
            )
            return ret["message_id"]

        first, second = await asyncio.gather(flow(1), flow(2))
        self.assertNotEqual(first, second)
        self.assertIn(first, self._history_ids("1"))
        self.assertNotIn(first, self._history_ids("2"))
        self.assertIn(second, self._history_ids("2"))
        self.assertNotIn(second, self._history_ids("1"))

    async def test_7_command_reply_recorded_but_not_recalled(self):
        self.plugin._mark_no_auto_recall()
        event = _FakeEvent(self.bot)
        await self.plugin.intercept_and_recall(event)
        self.assertTrue(self.plugin._has_pending_no_auto_recall_echo("123"))

        ret = await self.bot.send_group_msg(group_id=123, message=_text_message())
        message_id = ret["message_id"]

        self.assertIn(message_id, self._history_ids("123"))
        self.assertNotIn(("123", message_id), self.plugin._scheduled_recall_ids)
        self.assertTrue(self.plugin._is_no_auto_recall_message("123", message_id))
        self.assertFalse(self.plugin._has_pending_no_auto_recall_echo("123"))

    async def test_8_non_send_actions_are_transparent(self):
        event = _FakeEvent(self.bot)
        await self.plugin.intercept_and_recall(event)
        await self.bot.delete_msg(message_id=42)
        await self.bot.get_group_msg_history(group_id=123, count=30)

        self.assertEqual(self._history_ids("123"), [])
        self.assertEqual(
            [action for action, _ in self.bot.calls],
            ["delete_msg", "get_group_msg_history"],
        )

    async def test_9_other_session_send_is_not_misattributed(self):
        event = _FakeEvent(self.bot, group_id=123)
        await self.plugin.intercept_and_recall(event)
        await self.bot.send_group_msg(group_id=999, message=_text_message())

        self.assertEqual(self._history_ids("123"), [])
        self.assertEqual(self._history_ids("999"), [])

    async def test_10_event_send_chain_is_observed(self):
        event = _FakeEvent(self.bot)
        await self.plugin.intercept_and_recall(event)

        ret = await event.send(_text_message())
        self.assertIn(ret["message_id"], self._history_ids("123"))
        self.assertIn(("123", ret["message_id"]), self.plugin._scheduled_recall_ids)

    async def test_11_private_multi_segment_sends_each_recorded_once(self):
        event = _FakeEvent(self.bot, group_id=None, sender_id=456)
        await self.plugin.intercept_and_recall(event)

        first = await event.send(_text_message("第一段"))
        second = await event.send(_text_message("第二段"))

        history_ids = self._history_ids("456")
        self.assertEqual(len(history_ids), 2)
        self.assertEqual(set(history_ids), {first["message_id"], second["message_id"]})
        for message_id in (first["message_id"], second["message_id"]):
            self.assertIn(("456", message_id), self.plugin._scheduled_recall_ids)
        # 历史补录后台任务只启动一次。
        self.assertTrue(self.plugin._send_context.get().history_task_started)

    async def test_12_no_context_send_is_not_recorded(self):
        # 插件钩子未执行（例如会话禁用了本插件）时，同会话发送不应误记。
        await self.bot.send_group_msg(group_id=123, message=_text_message())
        self.assertEqual(self._history_ids("123"), [])

    async def test_13_expired_context_discarded_and_pending_released(self):
        self.plugin._mark_no_auto_recall()
        event = _FakeEvent(self.bot)
        await self.plugin.intercept_and_recall(event)
        self.assertTrue(self.plugin._has_pending_no_auto_recall_echo("123"))

        ctx = self.plugin._send_context.get()
        ctx.expires_at = time.monotonic() - 1
        await event.send(_text_message())

        self.assertEqual(self._history_ids("123"), [])
        self.assertFalse(self.plugin._has_pending_no_auto_recall_echo("123"))

    async def test_14_duplicate_message_id_recorded_once(self):
        self.bot.fixed_message_id = 3000
        event = _FakeEvent(self.bot)
        await self.plugin.intercept_and_recall(event)
        await event.send(_text_message("第一段"))
        await event.send(_text_message("第二段"))

        self.assertEqual(self._history_ids("123"), [3000])
        self.assertEqual(list(self.plugin._scheduled_recall_ids), [("123", 3000)])

    async def test_15_reload_takes_over_stale_observer(self):
        stale = main.BatchRecall(object(), _config())
        await stale.intercept_and_recall(_FakeEvent(self.bot))
        stale_wrapper = self.bot.call_action

        # 旧实例未卸载，新实例接管：不应出现多层包装。
        await self.plugin.intercept_and_recall(_FakeEvent(self.bot))
        self.assertIsNot(self.bot.call_action, stale_wrapper)
        observer = self.plugin._send_observers[0]
        self.assertFalse(getattr(observer.original, "_batchrecall_observer", False))

        ret = await self.bot.send_group_msg(group_id=123, message=_text_message())
        self.assertIn(ret["message_id"], self._history_ids("123"))

        await stale.terminate()  # 旧实例卸载不能破坏新观察器
        self.assertEqual(len(self.plugin._send_observers), 1)
        self.assertTrue(getattr(self.bot.call_action, "_batchrecall_observer", False))

    async def test_16_earlier_decorating_plugin_send_is_not_captured(self):
        # priority 更高的装饰钩子先发送时，本插件尚未建立上下文。
        early = await self.bot.send_group_msg(
            group_id=123, message=_text_message("其它插件")
        )
        event = _FakeEvent(self.bot)
        await self.plugin.intercept_and_recall(event)
        final = await event.send(_text_message("最终回复"))

        ids = self._history_ids("123")
        self.assertNotIn(early["message_id"], ids)
        self.assertIn(final["message_id"], ids)

    async def test_17_echo_and_receipt_do_not_double_schedule(self):
        self.bot.fixed_message_id = 4000
        event = _FakeEvent(self.bot)
        await self.plugin.intercept_and_recall(event)

        # 协议端先回显机器人自身消息，随后发送回执到达。
        async for _ in self.plugin.on_message(
            _FakeEchoEvent(self.bot, 123, 4000)
        ):
            pass
        # 确认回显路径确实执行过（先记录 + 先调度）。
        self.assertEqual(self._history_ids("123"), [4000])
        self.assertEqual(list(self.plugin._scheduled_recall_ids), [("123", 4000)])

        await event.send(_text_message())
        # 回执到达后不重复记录、不重复入队。
        self.assertEqual(self._history_ids("123"), [4000])
        self.assertEqual(list(self.plugin._scheduled_recall_ids), [("123", 4000)])

    async def test_18_command_reply_echo_is_exempt(self):
        self.plugin._mark_no_auto_recall()
        self.bot.fixed_message_id = 5000
        event = _FakeEvent(self.bot)
        await self.plugin.intercept_and_recall(event)

        # 回显早于发送回执到达：pending 预留必须阻止误入队。
        async for _ in self.plugin.on_message(
            _FakeEchoEvent(self.bot, 123, 5000)
        ):
            pass
        self.assertNotIn(("123", 5000), self.plugin._scheduled_recall_ids)

        await event.send(_text_message())
        self.assertIn(5000, self._history_ids("123"))
        self.assertNotIn(("123", 5000), self.plugin._scheduled_recall_ids)
        self.assertFalse(self.plugin._has_pending_no_auto_recall_echo("123"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
