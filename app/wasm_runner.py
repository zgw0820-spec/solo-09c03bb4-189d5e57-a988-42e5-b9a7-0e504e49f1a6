"""Wasmtime 运行封装。

同一校验器模块在进程启动时只编译一次；每次校验新建独立 Store/Instance，
各项从独立的初始状态开始，并以 fuel（指令额度）计量实际消耗。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wasmtime import Config, Engine, Instance, Module, Store, Trap, TrapCode

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
    """持有唯一的已编译 Module，负责按项执行校验。"""

    def __init__(self) -> None:
        config = Config()
        config.consume_fuel = True
        self._engine = Engine(config)
        # 同一校验器只编译一次
        self._module = Module(self._engine, _WAT_PATH.read_text(encoding="utf-8"))

    def run(self, conn_limit: int, timeout_ms: int, fuel_budget: int) -> RunResult:
        # 每项从独立的初始状态开始（独立 Store + 独立 Instance）
        store = Store(self._engine)
        store.set_fuel(fuel_budget)
        instance = Instance(store, self._module, [])
        exports = instance.exports(store)
        validate = exports["validate"]

        out_of_fuel = False
        error: str | None = None
        verdict_code: int | None = None
        try:
            verdict_code = int(validate(store, conn_limit, timeout_ms))
        except Trap as exc:
            if exc.trap_code == TrapCode.OUT_OF_FUEL:
                out_of_fuel = True
                error = "指令额度耗尽，校验器在得出结论前被中断"
            else:
                error = f"Wasm 陷阱：{exc}"
        except Exception as exc:  # pragma: no cover - 防御性兜底
            error = f"校验器执行异常：{exc}"

        remaining = store.get_fuel()
        consumed = fuel_budget - remaining

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
