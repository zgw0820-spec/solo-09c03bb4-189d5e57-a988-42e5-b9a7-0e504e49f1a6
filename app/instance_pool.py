"""Wasmtime Store / Instance 复用池。

进程内最多保留 ``MAX_INSTANCES`` 套 Store + Instance；一套实例同一时刻只
借给一项校验使用。空闲实例在下次校验前复位：

- 线性内存写回实例创建时保存的初始快照；
- 所有导出的可变全局恢复为创建时的初始值。

复位是宿主侧操作（memory.write / global.set_value），不消耗 Wasm 燃料；
调用方在复位完成后才为本次校验设置燃料额度，因此复位开销不计入批次预算。

若校验触发 Wasm 陷阱（含 OUT_OF_FUEL）或复位失败，实例被淘汰，绝不回到
空闲池；后续校验会创建/取得干净的实例。池统计（新建、复用、淘汰）按进程
累计，供接口与网页展示。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from wasmtime import Engine, Global, Instance, Memory, Module, Store

MAX_INSTANCES = 2


class ResetError(RuntimeError):
    """实例无法恢复到新实例的初始状态。"""


@dataclass
class Slot:
    """一套可复用的 Store + Instance 及其实例初始状态快照。"""

    store: Store
    instance: Instance
    exports: dict[str, Any]
    memories: list[tuple[Memory, bytes]]
    mutable_globals: list[tuple[Global, Any]]


@dataclass
class _Stats:
    created: int = 0       # 实际新建的 Store + Instance 套数
    reused: int = 0        # 从空闲池借出并复用的次数
    evicted: int = 0       # 因陷阱/复位失败淘汰的套数
    reset_failures: int = 0
    resets: int = 0        # 成功复位次数（每次执行前都会复位）
    runs: int = 0          # 实际进入 Wasm 校验执行的次数
    max_in_use: int = 0    # 同时在用（已借出）套数的历史最高值


class InstancePool:
    """有界实例池，线程安全；最多同时保留 ``max_instances`` 套。"""

    def __init__(
        self,
        engine: Engine,
        module: Module,
        max_instances: int = MAX_INSTANCES,
    ) -> None:
        if max_instances <= 0:
            raise ValueError("max_instances 必须为正整数")
        self._engine = engine
        self._module = module
        self._max_instances = max_instances
        self._cv = threading.Condition(threading.Lock())
        self._idle: list[Slot] = []
        self._live = 0   # 空闲 + 借出中
        self._stats = _Stats()

    # ---- 借出 / 归还 / 淘汰 -------------------------------------------------
    def acquire(self) -> tuple[Slot, bool]:
        """借出一套实例；返回 (slot, 是否本次新建)。

        无空闲实例且未达到上限时新建；达到上限且全部借出时阻塞等待。
        """
        with self._cv:
            while True:
                if self._idle:
                    slot = self._idle.pop()
                    self._stats.reused += 1
                    self._note_in_use()
                    return slot, False
                if self._live < self._max_instances:
                    self._live += 1
                    break
                self._cv.wait()

        try:
            slot = self._create_slot()
        except Exception:
            with self._cv:
                self._live -= 1
                self._cv.notify()
            raise

        with self._cv:
            self._stats.created += 1
            self._note_in_use()
            return slot, True

    def _note_in_use(self) -> None:
        in_use = self._live - len(self._idle)
        self._stats.max_in_use = max(self._stats.max_in_use, in_use)

    def release(self, slot: Slot) -> None:
        """仅把复位/执行均正常的实例放回空闲池。"""
        with self._cv:
            self._idle.append(slot)
            self._cv.notify()

    def discard(self, slot: Slot, *, reset_failure: bool = False) -> None:
        """淘汰实例（陷阱或复位失败），不再复用。"""
        try:
            slot.store.close()
        except Exception:  # pragma: no cover - 关闭失败不影响淘汰语义
            pass
        with self._cv:
            self._live -= 1
            self._stats.evicted += 1
            if reset_failure:
                self._stats.reset_failures += 1
            self._cv.notify()

    # ---- 复位与计数 ---------------------------------------------------------
    def reset(self, slot: Slot) -> None:
        """把线性内存和导出的可变全局恢复到新实例的初始值。"""
        store = slot.store
        for memory, snapshot in slot.memories:
            if memory.data_len(store) != len(snapshot):
                raise ResetError("线性内存大小与初始快照不一致")
            written = memory.write(store, snapshot, 0)
            if written != len(snapshot):
                raise ResetError("线性内存未能完整写回初始快照")
        for global_, initial in slot.mutable_globals:
            global_.set_value(store, initial)
            if global_.value(store) != initial:
                raise ResetError("可变全局未能恢复为初始值")
        with self._cv:
            self._stats.resets += 1

    def record_run(self) -> None:
        with self._cv:
            self._stats.runs += 1

    # ---- 观测 ---------------------------------------------------------------
    def stats_snapshot(self) -> dict:
        with self._cv:
            return {
                "max_instances": self._max_instances,
                "created": self._stats.created,
                "reused": self._stats.reused,
                "evicted": self._stats.evicted,
                "reset_failures": self._stats.reset_failures,
                "resets": self._stats.resets,
                "runs": self._stats.runs,
                "live": self._live,
                "idle": len(self._idle),
                "in_use": self._live - len(self._idle),
                "max_in_use": self._stats.max_in_use,
            }

    # ---- 内部 ---------------------------------------------------------------
    def _create_slot(self) -> Slot:
        store = Store(self._engine)
        instance = Instance(store, self._module, [])
        exports = dict(instance.exports(store))
        memories = [
            (value, bytes(value.read(store)))
            for value in exports.values()
            if isinstance(value, Memory)
        ]
        mutable_globals = [
            (value, value.value(store))
            for value in exports.values()
            if isinstance(value, Global) and value.type(store).mutable
        ]
        return Slot(
            store=store,
            instance=instance,
            exports=exports,
            memories=memories,
            mutable_globals=mutable_globals,
        )
