"""实例池测试：至多两套、独占借用、复位、淘汰与并行等价性。"""
import threading
from concurrent.futures import ThreadPoolExecutor

from wasmtime import Store

from app.wasm_runner import ValidatorEngine


def _fresh_result(engine: ValidatorEngine, conn: int, timeout: int, fuel: int):
    """逐项新建 Store + Instance 的参考结果（用于等价性对照）。"""
    from wasmtime import Instance, Trap, TrapCode

    store = Store(engine._engine)
    store.set_fuel(fuel)
    instance = Instance(store, engine._module, [])
    validate = instance.exports(store)["validate"]
    code = -1
    out_of_fuel = False
    try:
        code = int(validate(store, conn, timeout))
    except Trap as exc:
        out_of_fuel = exc.trap_code == TrapCode.OUT_OF_FUEL
    consumed = fuel - store.get_fuel()
    return code, consumed, out_of_fuel


def test_pool_keeps_at_most_two_and_reuses_idle_slots():
    v = ValidatorEngine()
    for _ in range(10):
        assert v.run(5000, 3000, 300_000).verdict_code == 0

    st = v.stats()
    assert st["max_instances"] == 2
    assert st["created"] == 1          # 串行校验只需一套实例
    assert st["created"] + st["reused"] == st["runs"] == 10
    assert st["idle"] == 1 and st["in_use"] == 0
    assert st["evicted"] == 0


def test_slot_is_exclusive_until_released():
    v = ValidatorEngine()
    slot1, created1 = v._pool.acquire()
    slot2, created2 = v._pool.acquire()
    assert created1 and created2
    assert slot1 is not slot2

    acquired = threading.Event()

    def third_borrow():
        v._pool.acquire()
        acquired.set()

    t = threading.Thread(target=third_borrow, daemon=True)
    t.start()
    # 两套都借出时，第三项借不到实例（进程内最多两套）
    assert not acquired.wait(0.2)
    assert v.stats()["in_use"] == 2

    v._pool.release(slot1)
    assert acquired.wait(2)
    assert v.stats()["max_in_use"] == 2
    t.join(timeout=2)


def test_reset_restores_memory_and_mutable_globals():
    v = ValidatorEngine()
    v.run(12345, 6789, 300_000)  # 写脏内存并自增 check_count

    slot, _ = v._pool.acquire()
    exports = slot.exports
    get_state = exports["get_state"]
    assert get_state(slot.store) == 1
    assert bytes(exports["memory"].read(slot.store, 0, 24)) != bytes(24)

    v._pool.reset(slot)
    assert get_state(slot.store) == 0
    assert bytes(exports["memory"].read(slot.store)) == bytes(65536)
    v._pool.release(slot)


def test_reset_does_not_touch_fuel_or_charge_budget():
    v = ValidatorEngine()
    slot, _ = v._pool.acquire()
    slot.store.set_fuel(1234)
    v._pool.reset(slot)
    # 复位是宿主侧操作，不消耗也不改写燃料；燃料在复位后才按本次额度设置
    assert slot.store.get_fuel() == 1234
    v._pool.release(slot)

    quota = 300_000
    first = v.run(5000, 3000, quota)
    second = v.run(5000, 3000, quota)
    assert first.consumed == second.consumed
    assert v.stats()["resets"] == 3


def test_reset_restores_nonzero_initial_memory_and_global_values():
    # 自定义模块：初始内存含数据段、可变全局初始值非 0，验证复位到“新实例”的值
    from wasmtime import Config, Engine, Module

    from app.instance_pool import InstancePool

    engine = Engine(Config())
    module = Module(
        engine,
        """
        (module
          (memory (export "memory") 1)
          (data (i32.const 16) "\\aa\\bb\\cc")
          (global $g (export "g") (mut i64) (i64.const 42))
          (global $k (export "k") (mut i32) (i32.const 7))
          (func (export "dirty")
            (i64.store (i32.const 0) (i64.const -1))
            (global.set $g (i64.const 999))
            (global.set $k (i32.const 999))))
        """,
    )
    pool = InstancePool(engine, module, 1)
    slot, created = pool.acquire()
    assert created
    dirty = slot.exports["dirty"]
    dirty(slot.store)

    pool.reset(slot)
    memory = slot.exports["memory"]
    assert bytes(memory.read(slot.store, 0, 8)) == bytes(8)
    assert bytes(memory.read(slot.store, 16, 19)) == b"\xaa\xbb\xcc"
    assert slot.exports["g"].value(slot.store) == 42
    assert slot.exports["k"].value(slot.store) == 7
    pool.release(slot)


def test_out_of_fuel_trap_evicts_and_next_run_gets_clean_instance():
    v = ValidatorEngine()
    bad = v.run(5000, 3000, 500)
    assert bad.out_of_fuel and bad.consumed == 500 and bad.verdict_code == -1

    st = v.stats()
    assert st["evicted"] == 1
    assert st["idle"] == 0 and st["in_use"] == 0

    good = v.run(5000, 3000, 300_000)
    assert good.verdict_code == 0 and good.error is None
    # 陷阱后的实例已淘汰，下一项用新建的干净实例
    assert v.stats()["created"] == 2


def test_non_fuel_trap_evicts_and_settles_actual_consumption():
    from wasmtime import Module

    from app.instance_pool import InstancePool

    v = ValidatorEngine()
    trap_module = Module(
        v._engine,
        """
        (module
          (memory (export "memory") 1)
          (global $g (export "check_count") (mut i64) (i64.const 0))
          (func (export "validate") (param i64) (param i64) (result i64)
            (unreachable)))
        """,
    )
    v._module = trap_module
    v._pool = InstancePool(v._engine, trap_module, 2)

    quota = 300_000
    first = v.run(5000, 3000, quota)
    assert first.verdict_code == -1
    assert "Wasm 陷阱" in first.error
    assert 0 < first.consumed < quota      # 非额度耗尽，按实际已耗燃料结算
    assert v.stats()["evicted"] == 1

    second = v.run(5000, 3000, quota)
    assert "Wasm 陷阱" in second.error
    assert v.stats()["created"] == 2       # 淘汰后用新建的干净实例继续
    assert v.stats()["evicted"] == 2


def test_reset_failure_evicts_and_keeps_settlement(monkeypatch):
    v = ValidatorEngine()
    quota = 300_000
    real_reset = v._pool.reset
    calls = {"n": 0}

    def flaky_reset(slot):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("模拟复位失败")
        real_reset(slot)

    monkeypatch.setattr(v._pool, "reset", flaky_reset)

    first = v.run(5000, 3000, quota)
    assert first.verdict_code == -1
    assert first.consumed == quota       # 报错照常按全额预留结算
    assert "复位失败" in first.error
    st = v.stats()
    assert st["evicted"] == 1 and st["reset_failures"] == 1

    second = v.run(5000, 3000, quota)
    assert second.verdict_code == 0 and second.error is None
    assert v.stats()["created"] == 2


def test_pooled_results_match_fresh_instances_across_fuel_boundary():
    """相同配置与额度：结论、实际消耗、耗尽边界均与逐项新建一致。"""
    v = ValidatorEngine()
    configs = [
        (5000, 3000),
        (200_000, 1500),
        (500, 90_000),
        (0, 100),
        (10, 0),
        (100_000, 60_000),
    ]
    fuels = [500, 1000, 18_000, 18_010, 18_020, 18_021, 18_030, 18_042, 18_043, 30_000]

    for cfg in configs:
        for fuel in fuels:
            expected = _fresh_result(v, *cfg, fuel)
            got = v.run(*cfg, fuel)
            assert (got.verdict_code, got.consumed, got.out_of_fuel) == expected, (
                cfg,
                fuel,
                expected,
                got,
            )


def test_parallel_borrows_stay_within_two_and_match_fresh():
    v = ValidatorEngine()
    cases = [(5000 + i, 3000 + i, 300_000) for i in range(12)] + [
        (200_000, 1500, 300_000),
        (5000, 3000, 500),
    ]
    expected = [_fresh_result(v, *c) for c in cases]

    with ThreadPoolExecutor(max_workers=8) as pool:
        got = list(pool.map(lambda c: v.run(*c), cases))

    assert [(r.verdict_code, r.consumed, r.out_of_fuel) for r in got] == expected
    st = v.stats()
    assert st["created"] <= 2 + 1        # OUT_OF_FUEL 的实例淘汰后补建一套
    assert st["created"] + st["reused"] == st["runs"] == len(cases)
    assert st["in_use"] == 0
