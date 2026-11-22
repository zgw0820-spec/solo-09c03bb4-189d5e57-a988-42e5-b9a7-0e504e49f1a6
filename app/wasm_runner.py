"""Wasmtime 运行封装。

同一校验器模块在进程启动时只编译一次；校验执行复用空闲的 Store/Instance
（进程内至多两套，每套同一时刻只借给一项校验），每次执行前把线性内存与
导出的可变全局复位到新实例的初始值。以 fuel（指令额度）计量实际消耗；
复位在设置燃料额度之前完成，不占用批次预算。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wasmtime import Config, Engine, Module, Trap, TrapCode

from .instance_pool import MAX_INSTANCES, InstancePool

_WAT_PATH = Path(__file__).with_name("validator.wat")

# 校验器结论码 -> 含义
VERDICTS = {
    0: "通过：连接上限与请求超时均在允许窗口内",
    1: "不通过：连接上限超过允许最大值 100000",
    2: "不通过：请求超时超过允许最大值 60000 毫秒",
    3: "不通过：连接上限低于允许最小值 1",
    4: "不通过：请求超时低于允许最小值 1 毫秒",
}


@dataclass
class RunResult:
    verdict_code: int
    verdict: str
    consumed: int          # 实际指令消耗（燃料）
    budget: int            # 该项预留的指令额度
    out_of_fuel: bool      # 是否因指令额度不足中断
    error: str | None      # 其它报错信息（正常结束为 None）


class ValidatorEngine:
    """持有唯一的已编译 Module 与实例池，负责按项执行校验。"""

    def __init__(self, max_instances: int = MAX_INSTANCES) -> None:
        config = Config()
        config.consume_fuel = True
        self._engine = Engine(config)
        # 同一校验器只编译一次
        self._module = Module(self._engine, _WAT_PATH.read_text(encoding="utf-8"))
        # 进程内至多保留 max_instances 套 Store + Instance，供各项轮流复用
        self._pool = InstancePool(self._engine, self._module, max_instances)

    @property
    def pool(self) -> InstancePool:
        return self._pool

    def stats(self) -> dict:
        """本进程实例池的累计新建/复用/淘汰次数等。"""
        return self._pool.stats_snapshot()

    def run(self, conn_limit: int, timeout_ms: int, fuel_budget: int) -> RunResult:
        # 借出一套实例；一套同一时刻只供一项校验使用
        slot, _created = self._pool.acquire()

        # 每次执行前恢复到新实例的初始状态（不消耗燃料，先于设置额度）
        try:
            self._pool.reset(slot)
        except Exception as exc:
            self._pool.discard(slot, reset_failure=True)
            error = f"实例复位失败，已淘汰：{exc}"
            return RunResult(
                verdict_code=-1,
                verdict=f"未得出结论：{error}",
                consumed=fuel_budget,  # 执行报错按全额预留结算
                budget=fuel_budget,
                out_of_fuel=False,
                error=error,
            )

        store = slot.store
        store.set_fuel(fuel_budget)
        validate = slot.exports["validate"]
        self._pool.record_run()

        out_of_fuel = False
        error: str | None = None
        verdict_code: int | None = None
        evict = False
        try:
            verdict_code = int(validate(store, conn_limit, timeout_ms))
        except Trap as exc:
            # 任何 Wasm 陷阱都说明实例状态不可信，淘汰后由后续项用干净实例
            evict = True
            if exc.trap_code == TrapCode.OUT_OF_FUEL:
                out_of_fuel = True
                error = "指令额度耗尽，校验器在得出结论前被中断"
            else:
                error = f"Wasm 陷阱：{exc}"
        except Exception as exc:  # pragma: no cover - 防御性兜底
            evict = True
            error = f"校验器执行异常：{exc}"

        try:
            remaining = store.get_fuel()
            consumed = max(0, fuel_budget - remaining)
        except Exception as exc:  # pragma: no cover - 防御性兜底
            evict = True
            consumed = fuel_budget
            error = error or f"读取剩余燃料失败：{exc}"

        if evict:
            self._pool.discard(slot)
        else:
            # 执行正常：内存/全局即使已被写脏，下次借出前也会复位
            self._pool.release(slot)

        if out_of_fuel:
            verdict = "未得出结论：指令额度不足（OUT_OF_FUEL）"
        elif error is not None:
            verdict = f"未得出结论：{error}"
        else:
            verdict = VERDICTS.get(verdict_code, f"未知结论码：{verdict_code}")

        return RunResult(
            verdict_code=-1 if verdict_code is None else verdict_code,
            verdict=verdict,
            consumed=consumed,
            budget=fuel_budget,
            out_of_fuel=out_of_fuel,
            error=error,
        )
