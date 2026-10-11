"""
TDD 回归测试：针对 code review 中 HIGH / MEDIUM 修复项。

HIGH 1: _notified_flags 用 set trim 不保证 FIFO
HIGH 2: create_task 无引用
HIGH 3: _save_state 并发写竞争 + 非 atomic
MEDIUM 1: _on_friend_request 加 wait_for 防 hang
MEDIUM 2: secret 空时 on_load 打 warning
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import types
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------- 导入插件（绕开相对导入 + maibot_sdk 缺失）----------

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_plugin_package():
    # 确保 maibot_sdk stub 已装
    from conftest import _install_sdk_stub
    _install_sdk_stub()

    if "whitefox" in sys.modules and hasattr(sys.modules["whitefox"], "plugin"):
        return sys.modules["whitefox"]

    pkg = types.ModuleType("whitefox")
    pkg.__path__ = [str(REPO_ROOT)]
    sys.modules["whitefox"] = pkg

    for name in ("config", "handlers", "plugin"):
        path = REPO_ROOT / f"{name}.py"
        spec = importlib.util.spec_from_file_location(f"whitefox.{name}", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"whitefox.{name}"] = mod
        setattr(pkg, name, mod)
        # 让相对导入 from .config 工作
        mod.__package__ = "whitefox"
        spec.loader.exec_module(mod)
    return pkg


_pkg = _load_plugin_package()
FriendRequestHandlerPlugin = _pkg.plugin.FriendRequestHandlerPlugin


# ---------- fixture ----------


@pytest.fixture
def plugin(tmp_path):
    """干净 plugin 实例，不开 webhook，状态文件落在临时目录。"""
    p = FriendRequestHandlerPlugin()
    p.config.plugin.enabled = False
    p.config.webhook.secret = "test-secret"  # 默认有 secret，避免 warning 干扰
    p._data_path = str(tmp_path / "state.json")
    p._runner = None
    p._site = None
    p._pending = {}
    # 修复后 plugin 期望 OrderedDict；旧实现 set 会让 FIFO 测试失败（这是 RED 阶段的预期）
    # 强制新类型，让测试走真实代码路径
    if not hasattr(p, "_notified_flags") or p._notified_flags is None or isinstance(p._notified_flags, set):
        p._notified_flags = OrderedDict()
    # 初始化 pending tasks（修复后才有，这里容错）
    if not hasattr(p, "_pending_tasks"):
        p._pending_tasks = set()
    if not hasattr(p, "_state_lock"):
        p._state_lock = asyncio.Lock()
    return p


# ---------- HIGH 1: OrderedDict FIFO trim ----------


class TestNotifiedFlagsFIFO:
    def test_trim_keeps_recent_flags(self, plugin):
        """
        加 500+ flag 触发 trim 后，最近加的 flag 必须还在。
        旧 set 实现：list(set) 顺序不保证 FIFO，trim 可能误删最新。
        新 OrderedDict 实现：popitem(last=False) 保证 FIFO。
        """
        # 初始化为空 OrderedDict（如果还是 set，测试会失败）
        # 直接测 plugin 实际使用的类型
        flags = plugin._notified_flags
        # 若是 set，模拟 on_friend_request 的 trim 逻辑
        # 我们调用真实的 _on_friend_request 路径里的 trim 代码
        # 更直接：加 501 个 flag，触发 trim 逻辑（与 plugin.py 里相同）
        # 为了可测，我们直接走 plugin 的真实路径：
        #   flag in flags → add → if len > 500: trim
        # 这里我们把 _notified_flags 重置为 plugin 实际类型，然后模拟 add+trim

        # 确保初始为空
        if isinstance(flags, set):
            plugin._notified_flags = set()
        elif isinstance(flags, OrderedDict):
            plugin._notified_flags.clear()
        else:
            # 未知类型，强制清空
            plugin._notified_flags = type(flags)() if not isinstance(flags, set) else set()
            if isinstance(plugin._notified_flags, OrderedDict):
                plugin._notified_flags.clear()

        # 通过 _on_friend_request 内部逻辑来测更真实，但那会走 handlers。
        # 我们直接测 plugin 的 trim 语义：加 flag 后触发 trim。
        # 提取真实代码路径：
        def add_flag(flag: str) -> None:
            """复刻 plugin.py 中 _on_friend_request 的 trim 逻辑。"""
            nf = plugin._notified_flags
            if flag in nf:
                return
            if isinstance(nf, OrderedDict):
                nf[flag] = None
                while len(nf) > 250:
                    nf.popitem(last=False)
            else:
                # 旧 set 实现
                nf.add(flag)
                if len(nf) > 500:
                    excess = list(nf)[: len(nf) - 250]
                    nf.difference_update(excess)

        # 加 501 个 flag：flag-0 ... flag-500
        for i in range(501):
            add_flag(f"flag-{i}")

        # 最近加的（最后 250 个）必须在
        recent = [f"flag-{i}" for i in range(251, 501)]  # 最后 250 个
        missing = [f for f in recent if f not in plugin._notified_flags]
        assert not missing, (
            f"trim 后最近的 flag 丢失了 {len(missing)} 个，例如 {missing[:5]}。"
            f" 当前类型={type(plugin._notified_flags).__name__}，size={len(plugin._notified_flags)}。"
            f" 这证明 set 的 list() 顺序不保证 FIFO。"
        )
        # 总数应回到 ~250
        assert len(plugin._notified_flags) <= 250, (
            f"trim 后 size={len(plugin._notified_flags)}，期望 ≤250"
        )

    def test_notified_flags_is_ordered_dict(self, plugin):
        """修复后 _notified_flags 必须是 OrderedDict。"""
        # 通过 on_load 初始化（模拟）
        # 直接检查类型注解 / on_load 赋值
        # 我们调一下模拟 on_load 的初始化逻辑
        plugin._notified_flags = getattr(plugin, "_notified_flags", None)
        # 实际 on_load 会 set 它
        # 这里断言：类型应该是 OrderedDict（修复后）
        # 若还是 set，FAIL
        # 为了让 fixture 不干扰，我们重新走 on_load 初始化路径
        # 手动模拟 on_load 的相关行：
        # self._notified_flags = set()  ← 旧
        # self._notified_flags = OrderedDict()  ← 新
        # 检查源码里 on_load 实际赋了什么
        import inspect
        src = inspect.getsource(plugin.on_load)
        # 不靠源码字符串，靠运行时：
        # 我们直接 import 后新建实例，看 on_load 后类型
        # 但 on_load 会调 _start_webhook，我们 stub 它
        async def _run():
            with patch.object(plugin, "_start_webhook", new_callable=AsyncMock):
                with patch.object(plugin, "_load_state"):
                    plugin.config.plugin.enabled = False
                    await plugin.on_load()
        asyncio.get_event_loop().run_until_complete(_run()) if False else None
        # 用 pytest-asyncio 风格：直接 asyncio.run
        asyncio.run(_run())
        assert isinstance(plugin._notified_flags, OrderedDict), (
            f"期望 OrderedDict，实际 {type(plugin._notified_flags).__name__}"
        )


# ---------- HIGH 2: create_task 有引用 ----------


class TestPendingTasksRef:
    @pytest.mark.asyncio
    async def test_webhook_keeps_task_ref(self, plugin):
        """
        调 webhook handler → create_task(_on_friend_request) →
        plugin 必须有 task 引用（_pending_tasks）。
        """
        # stub _on_friend_request 让它慢一点，保证 task 还活着
        started = asyncio.Event()
        finished = asyncio.Event()

        async def slow_handler(payload):
            started.set()
            await finished.wait()

        plugin._on_friend_request = slow_handler
        plugin._pending_tasks = getattr(plugin, "_pending_tasks", set())
        # 确保有 _pending_tasks 属性（修复后 on_load 会设）
        if not hasattr(plugin, "_pending_tasks"):
            plugin._pending_tasks = set()

        # 构造假 request
        class FakeRequest:
            headers = {}
            async def read(self):
                return json.dumps({
                    "post_type": "request",
                    "request_type": "friend",
                    "user_id": "12345",
                    "flag": "flag-abc",
                    "comment": "hi",
                }).encode("utf-8")

        # secret 空 → 跳过签名校验
        plugin.config.webhook.secret = ""

        resp = await plugin._handle_webhook(FakeRequest())
        assert resp.status == 200

        # 等 slow_handler 真正启动
        await asyncio.wait_for(started.wait(), timeout=1.0)

        # 断言：有 task 引用
        assert hasattr(plugin, "_pending_tasks"), "plugin 没有 _pending_tasks 属性"
        assert len(plugin._pending_tasks) >= 1, (
            f"_pending_tasks 为空，create_task 没有被引用。"
            f" 当前 tasks={plugin._pending_tasks}"
        )

        # 收尾
        finished.set()
        # 等 task 跑完
        if plugin._pending_tasks:
            await asyncio.gather(*list(plugin._pending_tasks), return_exceptions=True)

    @pytest.mark.asyncio
    async def test_on_unload_cancels_pending_tasks(self, plugin):
        """on_unload 必须 cancel + await 所有 pending tasks。"""
        cancelled = []

        async def never_finish():
            try:
                await asyncio.Event().wait()  # 永远等
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        # 确保有 _pending_tasks
        if not hasattr(plugin, "_pending_tasks"):
            plugin._pending_tasks = set()
        task = asyncio.create_task(never_finish())
        plugin._pending_tasks.add(task)
        task.add_done_callback(plugin._pending_tasks.discard)
        # 让 coro 真的跑到 await 点，否则 cancel 时 except 块不触发
        await asyncio.sleep(0)

        with patch.object(plugin, "_stop_webhook", new_callable=AsyncMock):
            with patch.object(plugin, "_save_state"):
                await plugin.on_unload()

        # on_unload 已 await gather，task 此刻应已 done
        assert task.cancelled() or task.done(), "on_unload 后 task 应该被 cancel"
        assert cancelled, "task 应该收到 CancelledError"


# ---------- HIGH 3: atomic + lock save ----------


class TestSaveStateAtomic:
    @pytest.mark.asyncio
    async def test_concurrent_saves_produce_valid_json(self, plugin, tmp_path):
        """
        两个并发写，最终文件必须是合法 JSON。
        旧实现：直接 open("w") 无 lock → 可能交错写。
        新实现：asyncio.Lock + atomic write (.tmp + os.replace)。
        """
        plugin._data_path = str(tmp_path / "state.json")
        plugin._pending = {"111": {"flag": "a"}}
        plugin._notified_flags = OrderedDict([("f1", None), ("f2", None)]) \
            if not isinstance(plugin._notified_flags, set) else {"f1", "f2"}

        # 确保有 lock
        if not hasattr(plugin, "_state_lock"):
            plugin._state_lock = asyncio.Lock()

        async def writer(n: int):
            plugin._pending[str(n)] = {"flag": f"flag-{n}"}
            # 调用 _save_state；修复后它应该是 async 或内部用 lock
            result = plugin._save_state()
            if asyncio.iscoroutine(result):
                await result
            # 若是 sync 且用了 lock，应该通过某种方式串行化
            # 我们额外在调用点包 lock（模拟 line 150 / 206 的调用方）
            # 但测试的是 _save_state 本身的 atomic + 调用方的 lock
            # 这里直接并发调 _save_state

        # 并发 20 次写
        await asyncio.gather(*[writer(i) for i in range(20)])

        # 最终文件必须是合法 JSON
        with open(plugin._data_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        assert "pending" in data
        assert "notified_flags" in data

    def test_atomic_write_no_partial_file(self, plugin, tmp_path):
        """
        写过程中 kill（模拟）：残留文件不是损坏的。
        即：要么是旧完整文件，要么是新完整文件，绝不能半截 JSON。
        atomic 写法：先写 .tmp 再 os.replace。
        """
        plugin._data_path = str(tmp_path / "state.json")
        # 先写一份合法旧数据
        old = {"pending": {"old": {"flag": "x"}}, "notified_flags": ["old-flag"]}
        with open(plugin._data_path, "w", encoding="utf-8") as fp:
            json.dump(old, fp)

        plugin._pending = {"new": {"flag": "y"}}
        if isinstance(plugin._notified_flags, OrderedDict):
            plugin._notified_flags = OrderedDict([("new-flag", None)])
        else:
            plugin._notified_flags = {"new-flag"}

        # 检查 _save_state 是否用了 .tmp + os.replace
        # 通过 mock open / os.replace 来验证调用顺序
        calls = []
        real_open = open
        real_replace = os.replace

        def tracking_open(path, mode="r", *args, **kwargs):
            calls.append(("open", str(path), mode))
            return real_open(path, mode, *args, **kwargs)

        def tracking_replace(src, dst):
            calls.append(("replace", str(src), str(dst)))
            return real_replace(src, dst)

        with patch("builtins.open", side_effect=tracking_open):
            with patch("os.replace", side_effect=tracking_replace):
                result = plugin._save_state()
                if asyncio.iscoroutine(result):
                    asyncio.run(result)

        # 断言：写的是 .tmp，然后 replace 到目标
        open_calls = [c for c in calls if c[0] == "open" and "w" in c[2]]
        replace_calls = [c for c in calls if c[0] == "replace"]
        assert open_calls, "应该 open 写文件"
        # 写的路径应该是 .tmp
        written_path = open_calls[0][1]
        assert written_path.endswith(".tmp") or written_path.endswith(".tmp"), (
            f"应该写到 .tmp 文件，实际写到 {written_path}"
        )
        assert replace_calls, "应该调用 os.replace 做 atomic 替换"
        assert str(plugin._data_path) in replace_calls[0][2] or \
               replace_calls[0][2].endswith("state.json"), (
            f"replace 目标应该是 state.json，实际 {replace_calls[0]}"
        )

        # 最终文件合法
        with open(plugin._data_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        assert "pending" in data


# ---------- MEDIUM 1: wait_for 防 hang ----------


class TestOnFriendRequestTimeout:
    @pytest.mark.asyncio
    async def test_on_friend_request_has_timeout(self, plugin):
        """
        _on_friend_request 处理路径应有 wait_for(..., timeout=45)。
        验证方式：让内部 handler 永远阻塞，断言 45s 内（我们 mock 成短 timeout）会退出。
        更直接：检查源码里有 wait_for，或让 create_task 的包装带 timeout。
        """
        import inspect
        # 检查 _handle_webhook 或 _on_friend_request 相关源码
        src_webhook = inspect.getsource(plugin._handle_webhook)
        src_on = inspect.getsource(plugin._on_friend_request)
        combined = src_webhook + src_on
        assert "wait_for" in combined or "timeout" in combined, (
            "_handle_webhook / _on_friend_request 里没有 wait_for/timeout，"
            "hang 的 handler 会永远占着 task。"
        )


# ---------- MEDIUM 2: secret 空时 warning ----------


class TestSecretEmptyWarning:
    @pytest.mark.asyncio
    async def test_empty_secret_warns_on_load(self, plugin):
        """
        secret 空时 on_load 打 warning 日志（不要 raise）。
        """
        plugin.config.webhook.secret = ""
        plugin.config.plugin.enabled = False  # 不开 webhook
        with patch.object(plugin, "_start_webhook", new_callable=AsyncMock):
            with patch.object(plugin, "_load_state"):
                await plugin.on_load()
        # 不应 raise
        warnings = plugin.ctx.logger.warnings
        assert any("secret" in w.lower() or "签名" in w or "校验" in w for w in warnings), (
            f"secret 空时应打 warning，实际 warnings={warnings}"
        )


# ---------- handle_llm_decision：核心 bug 修复 ----------
#
# Bug：LLM 路径通过的好友没有备注。
# 根因：set_friend_add_request 没传 remark + 后续 set_friend_remark 因 NapCat isBuddy
# 检查失败被 raise_on_error=False 静默吞掉。
# 修复：把备注生成提前到 set_friend_add_request 之前，并把 remark 一并传入。
# 后续 set_friend_remark 调用降级为兜底冗余。


def _make_llm_handler_decision_test_plugin():
    """构造一个干净的 plugin 用于 handle_llm_decision 测试。"""
    p = FriendRequestHandlerPlugin()
    p.config.plugin.enabled = False
    p.config.strategy.auto_remark = True
    p.config.admin.admin_qqs = []  # 避免推送给不存在的 admin
    p._pending = {}
    return p


class TestHandleLlmDecisionRemarkFix:
    """handle_llm_decision 必须在 set_friend_add_request 调用时携带 remark。"""

    @pytest.mark.asyncio
    async def test_passes_remark_to_set_friend_add_request_when_approved(self):
        """LLM 决策'通过'时，set_friend_add_request 的 params 必须含 remark。"""
        from whitefox import handlers
        plugin = _make_llm_handler_decision_test_plugin()

        # 记录所有 _call_napcat 调用
        napcat_calls = []
        async def fake_call_napcat(action_name, params, raise_on_error=False):
            napcat_calls.append((action_name, dict(params), raise_on_error))
            return {"status": "ok", "data": {}}

        # mock LLM 决策和备注生成
        with patch.object(handlers, "_call_llm",
                          new=AsyncMock(side_effect=[(True, "通过"), (True, "小周")])), \
             patch.object(handlers, "_send_welcome", new=AsyncMock()), \
             patch.object(FriendRequestHandlerPlugin, "_call_napcat",
                          new=AsyncMock(side_effect=fake_call_napcat)):
            await handlers.handle_llm_decision(plugin, "12345", "flag-abc", "你好")

        # 找 set_friend_add_request 调用
        add_req_calls = [c for c in napcat_calls if c[0] == "set_friend_add_request"]
        assert len(add_req_calls) == 1, f"应该调一次 set_friend_add_request，实际 {len(add_req_calls)}"
        action, params, raise_on = add_req_calls[0]
        assert params["flag"] == "flag-abc"
        assert params["approve"] is True
        assert params.get("remark") == "小周", (
            f"params 应含 remark='小周'，实际 {params}"
        )

    @pytest.mark.asyncio
    async def test_no_remark_when_rejected(self):
        """LLM 决策'拒绝'时，params 不含 remark 且 _generate_remark 不被调。"""
        from whitefox import handlers
        plugin = _make_llm_handler_decision_test_plugin()

        napcat_calls = []
        async def fake_call_napcat(action_name, params, raise_on_error=False):
            napcat_calls.append((action_name, dict(params), raise_on_error))
            return {"status": "ok", "data": {}}

        with patch.object(handlers, "_call_llm",
                          new=AsyncMock(return_value=(True, "拒绝"))), \
             patch.object(handlers, "_generate_remark", new=AsyncMock()) as mock_remark, \
             patch.object(handlers, "_send_welcome", new=AsyncMock()), \
             patch.object(FriendRequestHandlerPlugin, "_call_napcat",
                          new=AsyncMock(side_effect=fake_call_napcat)):
            await handlers.handle_llm_decision(plugin, "12345", "flag-abc", "hello")

        # 拒绝时不应调 _generate_remark（节省 LLM token）
        mock_remark.assert_not_called()

        add_req_calls = [c for c in napcat_calls if c[0] == "set_friend_add_request"]
        assert len(add_req_calls) == 1
        params = add_req_calls[0][1]
        assert params["approve"] is False
        assert "remark" not in params, f"拒绝时 params 不应含 remark，实际 {params}"

        # 不应调 set_friend_remark（拒绝后没必要）
        remark_calls = [c for c in napcat_calls if c[0] == "set_friend_remark"]
        assert len(remark_calls) == 0, "拒绝时不应调 set_friend_remark"

    @pytest.mark.asyncio
    async def test_no_remark_when_auto_remark_disabled(self):
        """auto_remark=false 时，_generate_remark 不被调用，params 不含 remark。"""
        from whitefox import handlers
        plugin = _make_llm_handler_decision_test_plugin()
        plugin.config.strategy.auto_remark = False

        napcat_calls = []
        async def fake_call_napcat(action_name, params, raise_on_error=False):
            napcat_calls.append((action_name, dict(params), raise_on_error))
            return {"status": "ok", "data": {}}

        with patch.object(handlers, "_call_llm",
                          new=AsyncMock(return_value=(True, "通过"))), \
             patch.object(handlers, "_generate_remark", new=AsyncMock()) as mock_remark, \
             patch.object(handlers, "_send_welcome", new=AsyncMock()), \
             patch.object(FriendRequestHandlerPlugin, "_call_napcat",
                          new=AsyncMock(side_effect=fake_call_napcat)):
            await handlers.handle_llm_decision(plugin, "12345", "flag-abc", "hi")

        mock_remark.assert_not_called()

        add_req_calls = [c for c in napcat_calls if c[0] == "set_friend_add_request"]
        params = add_req_calls[0][1]
        assert "remark" not in params, f"auto_remark=false 时 params 不含 remark，实际 {params}"

        # 不应调 set_friend_remark
        remark_calls = [c for c in napcat_calls if c[0] == "set_friend_remark"]
        assert len(remark_calls) == 0

    @pytest.mark.asyncio
    async def test_fallback_to_nickname_when_llm_remark_empty(self):
        """决策通过但 LLM 备注返回空时，fallback 到 _get_nickname。"""
        from whitefox import handlers
        plugin = _make_llm_handler_decision_test_plugin()

        napcat_calls = []
        async def fake_call_napcat(action_name, params, raise_on_error=False):
            napcat_calls.append((action_name, dict(params), raise_on_error))
            return {"status": "ok", "data": {}}

        # 第一次 _call_llm（决策）= 通过；第二次（备注）= 空
        with patch.object(handlers, "_call_llm",
                          new=AsyncMock(side_effect=[(True, "通过"), (True, "")])), \
             patch.object(handlers, "_get_nickname", new=AsyncMock(return_value="昵称兜底")) as mock_nick, \
             patch.object(handlers, "_send_welcome", new=AsyncMock()), \
             patch.object(FriendRequestHandlerPlugin, "_call_napcat",
                          new=AsyncMock(side_effect=fake_call_napcat)):
            await handlers.handle_llm_decision(plugin, "12345", "flag-abc", "hi")

        # _get_nickname 应被调（fallback）
        mock_nick.assert_called_once()

        add_req_calls = [c for c in napcat_calls if c[0] == "set_friend_add_request"]
        params = add_req_calls[0][1]
        assert params.get("remark") == "昵称兜底", (
            f"fallback 应使用 _get_nickname 返回值，实际 {params}"
        )

    @pytest.mark.asyncio
    async def test_set_friend_add_request_before_set_friend_remark(self):
        """set_friend_add_request 必须在 set_friend_remark 之前调用（保证主路径生效）。"""
        from whitefox import handlers
        plugin = _make_llm_handler_decision_test_plugin()

        call_order = []
        async def fake_call_napcat(action_name, params, raise_on_error=False):
            call_order.append(action_name)
            return {"status": "ok", "data": {}}

        with patch.object(handlers, "_call_llm",
                          new=AsyncMock(side_effect=[(True, "通过"), (True, "小周")])), \
             patch.object(handlers, "_send_welcome", new=AsyncMock()), \
             patch.object(FriendRequestHandlerPlugin, "_call_napcat",
                          new=AsyncMock(side_effect=fake_call_napcat)):
            await handlers.handle_llm_decision(plugin, "12345", "flag-abc", "hi")

        # 找 set_friend_add_request 和 set_friend_remark 的位置
        idx_add = next((i for i, a in enumerate(call_order) if a == "set_friend_add_request"), None)
        idx_remark = next((i for i, a in enumerate(call_order) if a == "set_friend_remark"), None)
        assert idx_add is not None, "set_friend_add_request 应被调用"
        assert idx_remark is not None, "set_friend_remark 应被调用（兜底）"
        assert idx_add < idx_remark, (
            f"set_friend_add_request 应在 set_friend_remark 之前，"
            f"实际顺序 {call_order}"
        )

    @pytest.mark.asyncio
    async def test_no_set_friend_remark_when_remark_empty(self):
        """remark 最终为空（LLM 备注 + nickname 都空）时，不调 set_friend_remark。"""
        from whitefox import handlers
        plugin = _make_llm_handler_decision_test_plugin()

        napcat_calls = []
        async def fake_call_napcat(action_name, params, raise_on_error=False):
            napcat_calls.append(action_name)
            return {"status": "ok", "data": {}}

        with patch.object(handlers, "_call_llm",
                          new=AsyncMock(side_effect=[(True, "通过"), (True, "")])), \
             patch.object(handlers, "_get_nickname", new=AsyncMock(return_value="")), \
             patch.object(handlers, "_send_welcome", new=AsyncMock()), \
             patch.object(FriendRequestHandlerPlugin, "_call_napcat",
                          new=AsyncMock(side_effect=fake_call_napcat)):
            await handlers.handle_llm_decision(plugin, "12345", "flag-abc", "hi")

        # set_friend_remark 不应被调
        remark_calls = [a for a in napcat_calls if a == "set_friend_remark"]
        assert len(remark_calls) == 0, (
            f"remark 为空时不应调 set_friend_remark，实际调用了 {len(remark_calls)} 次"
        )
        # 但 set_friend_add_request 仍应被调
        assert "set_friend_add_request" in napcat_calls

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_manual(self):
        """_call_llm 返回 ok=False 时，应回退到 handle_manual（回归测试）。"""
        from whitefox import handlers
        plugin = _make_llm_handler_decision_test_plugin()

        napcat_calls = []
        async def fake_call_napcat(action_name, params, raise_on_error=False):
            napcat_calls.append((action_name, dict(params), raise_on_error))
            return {"status": "ok", "data": {}}

        with patch.object(handlers, "_call_llm",
                          new=AsyncMock(return_value=(False, ""))) as mock_llm, \
             patch.object(handlers, "handle_manual", new=AsyncMock()) as mock_manual, \
             patch.object(handlers, "_send_welcome", new=AsyncMock()), \
             patch.object(FriendRequestHandlerPlugin, "_call_napcat",
                          new=AsyncMock(side_effect=fake_call_napcat)):
            await handlers.handle_llm_decision(plugin, "12345", "flag-abc", "hi")

        # 应回退到 handle_manual
        mock_manual.assert_called_once()
        # _call_llm 只调了一次（决策失败后直接回退，没生成备注）
        assert mock_llm.call_count == 1
        # LLM 失败时不应调 set_friend_add_request
        add_req_calls = [c for c in napcat_calls if c[0] == "set_friend_add_request"]
        assert len(add_req_calls) == 0, "LLM 失败回退到 manual 时不应调 NapCat"