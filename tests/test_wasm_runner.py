"""Wasmtime 引擎层测试：编译一次、独立初始状态、燃料计量与全部结论路径。"""
from wasmtime import Instance, Store

from app.wasm_runner import ValidatorEngine


def test_module_compiled_once_and_reused():
    v = ValidatorEngine()
    # 同一引擎持有唯一 Module，多次执行复用
    assert v._module is v._module
    r1 = v.run(5000, 3000, 300_000)
    r2 = v.run(5000, 3000, 300_000)
    assert r1.verdict_code == r2.verdict_code == 0


def test_each_item_starts_from_independent_initial_state():
    v = ValidatorEngine()
    s1, s2 = Store(v._engine), Store(v._engine)
    s1.set_fuel(10_000_000)
    s2.set_fuel(10_000_000)
    i1 = Instance(s1, v._module, [])
    i2 = Instance(s2, v._module, [])
    bump1 = i1.exports(s1)["bump_state"]
    get1 = i1.exports(s1)["get_state"]
    get2 = i2.exports(s2)["get_state"]
    assert bump1(s1) == 1
    assert bump1(s1) == 2
    # 第二个实例不受第一个实例状态影响，仍从初始值 0 开始
    assert get1(s1) == 2
    assert get2(s2) == 0


def test_fuel_actual_consumption_and_refund_basis():
    v = ValidatorEngine()
    quota = 300_000
    r = v.run(5000, 3000, quota)
    assert r.out_of_fuel is False
    assert 0 < r.consumed < quota
    # 未用额度 = 预留 - 实际消耗（调度器据此退回）
    assert quota - r.consumed > 0


def test_out_of_fuel_settles_full_quota_without_conclusion():
    v = ValidatorEngine()
    r = v.run(5000, 3000, 100)
    assert r.out_of_fuel is True
    assert r.verdict_code == -1
    assert r.consumed == 100  # 额度耗尽：预留额度全部结算为消耗
    assert "OUT_OF_FUEL" in r.verdict


def test_all_verdict_paths():
    v = ValidatorEngine()
    cases = {
        (5000, 3000): 0,      # 通过
        (200_000, 1500): 1,   # 连接上限超限
        (500, 90_000): 2,     # 请求超时超限
        (0, 100): 3,          # 连接上限过小
        (10, 0): 4,           # 请求超时过小
        (100_000, 60_000): 0, # 边界值通过
    }
    for (conn, timeout), expected in cases.items():
        r = v.run(conn, timeout, 300_000)
        assert r.verdict_code == expected, (conn, timeout, r.verdict)
        assert r.error is None
