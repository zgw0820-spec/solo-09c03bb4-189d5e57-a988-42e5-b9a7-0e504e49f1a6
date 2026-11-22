"""调度器测试：并行上限、燃料预留/结算/退回、预算不足标未检。"""
import asyncio

from app.scheduler import MAX_PARALLEL, ItemStatus, Scheduler
from tests.support import SlowValidator


def _specs(n: int) -> list[dict]:
    return [
        {"env_name": f"env-{i}", "conn_limit": 5000, "timeout_ms": 3000}
        for i in range(n)
    ]


async def _wait_done(scheduler: Scheduler, batch_id: str, timeout: float = 10) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if scheduler.get_batch(batch_id).done:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("批次未在预期时间内结束")


def test_parallel_cap_is_two(slow_validator: SlowValidator):
    async def scenario():
        scheduler = Scheduler(slow_validator)
        await scheduler.start()
        try:
            bid = scheduler.submit_batch(
                _specs(6), per_item_fuel=300_000, total_budget=10_000_000
            )
            await _wait_done(scheduler, bid)
            return scheduler.get_batch(bid)
        finally:
            await scheduler.shutdown()

    batch = asyncio.run(scenario())
    assert slow_validator.max_concurrent == MAX_PARALLEL == 2
    assert all(it.status == ItemStatus.PASSED for it in batch.items)


def test_parallel_settlement_refunds_unused_fuel(slow_validator: SlowValidator):
    quota = 300_000
    n = 5

    async def scenario():
        scheduler = Scheduler(slow_validator)
        await scheduler.start()
        try:
            bid = scheduler.submit_batch(
                _specs(n), per_item_fuel=quota, total_budget=10_000_000
            )
            await _wait_done(scheduler, bid)
            return scheduler.get_batch(bid)
        finally:
            await scheduler.shutdown()

    batch = asyncio.run(scenario())
    assert batch.done
    assert all(it.status == ItemStatus.PASSED for it in batch.items)
    consumed = [it.consumed for it in batch.items]
    # 每项按实际消耗结算，实际消耗小于预留额度
    assert all(0 < c < quota for c in consumed)
    assert batch.reserved == 0
    # 账本守恒：总预算 = 已结算实际消耗 + 剩余预算
    assert batch.total_budget == batch.settled_consumed + batch.remaining_budget
    assert batch.settled_consumed == sum(consumed)
    # history 记录每项的预留/消耗/退回
    assert len(batch.history) == n
    assert all(h["returned"] == h["quota"] - h["consumed"] for h in batch.history)


def test_budget_shortfall_marks_rest_skipped(slow_validator: SlowValidator):
    n = 5
    quota = 300_000
    # 预算只够 1 项启动；实际消耗约 1.8 万，退回后余额仍远小于下一项额度
    total_budget = 300_000

    async def scenario():
        scheduler = Scheduler(slow_validator)
        await scheduler.start()
        try:
            bid = scheduler.submit_batch(
                _specs(n), per_item_fuel=quota, total_budget=total_budget
            )
            await _wait_done(scheduler, bid)
            return scheduler.get_batch(bid)
        finally:
            await scheduler.shutdown()

    batch = asyncio.run(scenario())
    statuses = [it.status for it in batch.items]
    assert statuses.count(ItemStatus.PASSED) == 1
    assert statuses.count(ItemStatus.SKIPPED) == n - 1
    assert all("预算不足" in it.reason for it in batch.items if it.status == ItemStatus.SKIPPED)
    # 账本仍守恒，剩余余额不足以再启动任何一项
    assert batch.total_budget == batch.settled_consumed + batch.remaining_budget
    assert batch.remaining_budget < quota


def test_budget_below_one_quota_skips_everything(slow_validator: SlowValidator):
    async def scenario():
        scheduler = Scheduler(slow_validator)
        await scheduler.start()
        try:
            bid = scheduler.submit_batch(
                _specs(3), per_item_fuel=300_000, total_budget=100_000
            )
            await _wait_done(scheduler, bid)
            return scheduler.get_batch(bid)
        finally:
            await scheduler.shutdown()

    batch = asyncio.run(scenario())
    assert all(it.status == ItemStatus.SKIPPED for it in batch.items)
    assert batch.settled_consumed == 0
    assert batch.remaining_budget == 100_000
