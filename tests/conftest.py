"""
测试辅助：在不依赖 MaiBot SDK 的环境下导入 plugin / handlers / config。
把 maibot_sdk 桩模块塞进 sys.modules，再让 plugin 代码按 SDK 契约运行。
"""

from __future__ import annotations

import sys
import types
import pytest
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


CONFIG_RELOAD_SCOPE_SELF = "self"


class _Field:
    def __init__(self, default: Any = None, default_factory: Optional[Callable[[], Any]] = None, **metadata: Any):
        self.default = default
        self.default_factory = default_factory
        self.metadata = metadata

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, instance, owner):
        if instance is None:
            return self
        if self.name not in instance.__dict__:
            if self.default_factory is not None:
                instance.__dict__[self.name] = self.default_factory()
            else:
                instance.__dict__[self.name] = self.default
        return instance.__dict__[self.name]


def Field(default: Any = None, default_factory: Optional[Callable[[], Any]] = None, **metadata: Any):
    return _Field(default=default, default_factory=default_factory, **metadata)


class PluginConfigBase:
    def __init__(self, **kwargs: Any):
        for key, value in kwargs.items():
            setattr(self, key, value)


class _Command:
    def __init__(self, name: str, description: str = "", pattern: str = ""):
        self.name = name
        self.description = description
        self.pattern = pattern

    def __call__(self, func):
        func.__command__ = self
        return func


Command = _Command


@dataclass
class _Logger:
    infos: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    debugs: list = field(default_factory=list)

    def _record(self, level: str, msg: str):
        getattr(self, level + "s").append(msg)

    def info(self, msg: str, *args, **kwargs):
        self._record("info", msg % args if args else msg)

    def warning(self, msg: str, *args, **kwargs):
        self._record("warning", msg % args if args else msg)

    def error(self, msg: str, *args, **kwargs):
        self._record("error", msg % args if args else msg)

    def debug(self, msg: str, *args, **kwargs):
        self._record("debug", msg % args if args else msg)


@dataclass
class _LLM:
    async def generate(self, *args, **kwargs):
        return {"success": True, "response": "通过"}


@dataclass
class _Api:
    async def call(self, *args, **kwargs):
        return {"status": "ok", "data": {}}


@dataclass
class _SendText:
    async def text(self, text: str, stream_id: str):
        return None


@dataclass
class _Send:
    text: Any = field(default_factory=_SendText)


@dataclass
class _Context:
    logger: _Logger = field(default_factory=_Logger)
    llm: _LLM = field(default_factory=_LLM)
    api: _Api = field(default_factory=_Api)
    send: _Send = field(default_factory=_Send)


class MaiBotPlugin:
    config_model: type = PluginConfigBase

    def __init__(self):
        self.ctx = _Context()
        self.config = self.config_model()


def _install_sdk_stub() -> None:
    if "maibot_sdk" in sys.modules:
        return
    mod = types.ModuleType("maibot_sdk")
    mod.CONFIG_RELOAD_SCOPE_SELF = CONFIG_RELOAD_SCOPE_SELF
    mod.Field = Field
    mod.PluginConfigBase = PluginConfigBase
    mod.Command = Command
    mod.MaiBotPlugin = MaiBotPlugin
    sys.modules["maibot_sdk"] = mod


# 立刻在模块加载时安装 stub，确保 from plugin import ... 之前 maibot_sdk 已存在
_install_sdk_stub()


@pytest.fixture
def plugin():
    """构造一个干净 plugin 实例用于单测（不开 webhook）。"""
    # 把仓库根目录放进 sys.path，import 时以包内模块身份
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # 已通过 pytest rootdir 自动加入 cwd
    from plugin import FriendRequestHandlerPlugin
    p = FriendRequestHandlerPlugin()
    # 关掉 webhook，避免真起监听
    p.config.plugin.enabled = False
    p._data_path = os.path.join(repo_root, "data", "test_state.json")
    # 清理上一次单测残留
    if os.path.exists(p._data_path):
        os.remove(p._data_path)
    return p


import os  # noqa: E402