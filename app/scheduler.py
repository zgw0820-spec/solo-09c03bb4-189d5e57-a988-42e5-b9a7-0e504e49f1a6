"""批量校验调度器。

规则：
- 最多并行两项（MAX_PARALLEL = 2），由 2 worker 的线程池承载 Wasm 同步调用；
- 启动一项前必须同时满足：有空位、且整批剩余预算 >= 该项指令额度；
- 启动时按单项额度预留（冻结）足额指令预算；
- 每项结束后按实际消耗扣减预算，未用额度退回剩余预算，报错同样结算；
- 余量不足且无在跑项可等待时，剩余配置标记为“未检”。
"""
from __future__ import annotations

import asyncio
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum

from .wasm_runner import RunResult, ValidatorEngine

MAX_PARALLEL = 2


class ItemStatus(str, Enum):
    QUEUED = "queued"        # 排队中
    RUNNING = "running"      # 校验中
    PASSED = "passed"        # 校验通过
    FAILED = "failed"        # 校验不通过（Wasm 给出结论）
    NO_CONCLUSION = "no_conclusion"  # 未得出结论（额度耗尽/执行报错）
    SKIPPED = "skipped"      # 未检（预算不足）


@dataclass
class BatchItem:
    id: str
    env_name: str
    conn_limit: int
    timeout_ms: int
    fuel_quota: int
    status: ItemStatus = ItemStatus.QUEUED
    verdict: str = ""
    verdict_code: int | None = None
    consumed: int = 0
    reason: str = ""


@dataclass
class Batch:
    id: str
    total_budget: int
    remaining_budget: int
    items: list[BatchItem]
    done: bool = False
    settled_consumed: int = 0          # 已结算项的实际消耗合计
    reserved: int = 0                  # 在跑项已预留（冻结）额度合计
    history: list[dict] = field(default_factory=list)


@dataclass
class _Job:
    batch_id: str
    item: BatchItem


class Scheduler:
    def __init__(self, validator: ValidatorEngine) -> None:
        self._validator = validator
        self._executor = ThreadPoolExecutor(
            max_workers=MAX_PARALLEL, thread_name_prefix="wasm-validate"
        )
        self._queue: deque[_Job] = deque()
        self._batches: dict[str, Batch] = {}
        self._cond = asyncio.Condition()
        self._running_count = 0
        self._tasks: set[asyncio.Task] = set()
        self._dispatcher_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._dispatcher_task is None:
            self._dispatcher_task = asyncio.create_task(self._dispatcher())

    async def shutdown(self) -> None:
        if self._dispatcher_task is not None:
            self._dispatcher_task.cancel()
            await asyncio.gather(self._dispatcher_task, return_exceptions=True)
            self._dispatcher_task = None
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def submit_batch(
        self,
        items_spec: list[dict],
        per_item_fuel: int,
        total_budget: int,
    ) -> str:
        if per_item_fuel <= 0:
            raise ValueError("单项指令额度必须为正整数")
        if total_budget <= 0:
            raise ValueError("整批预算必须为正整数")
        if not items_spec:
            raise ValueError("至少提交一套环境配置")

        batch_id = uuid.uuid4().hex[:12]
        items = [
            BatchItem(
                id=uuid.uuid4().hex[:8],
                env_name=str(spec.get("env_name") or f"env-{idx + 1}"),
                conn_limit=int(spec["conn_limit"]),
                timeout_ms=int(spec["timeout_ms"]),
                fuel_quota=per_item_fuel,
            )
            for idx, spec in enumerate(items_spec)
        ]
        batch = Batch(
            id=batch_id,
            total_budget=total_budget,
            remaining_budget=total_budget,
            items=items,
        )
        self._batches[batch_id] = batch
        for item in items:
            self._queue.append(_Job(batch_id, item))
        # 在事件循环线程内安全唤醒派发器
        asyncio.get_running_loop().call_soon_threadsafe(self._schedule_notify)
        return batch_id

    def _schedule_notify(self) -> None:
        asyncio.ensure_future(self._notify())

    async def _notify(self) -> None:
        async with self._cond:
            self._cond.notify_all()

    def get_batch(self, batch_id: str) -> Batch:
        if batch_id not in self._batches:
            raise KeyError(batch_id)
        return self._batches[batch_id]

    async def _dispatcher(self) -> None:
        """单派发协程：每次被唤醒时尽量把空位填满（至多 2 项并行）。"""
        while True:
            async with self._cond:
                await self._cond.wait_for(lambda: len(self._queue) > 0)

                while self._queue and self._running_count < MAX_PARALLEL:
                    job = self._queue[0]
                    batch = self._batches[job.batch_id]
                    item = job.item

                    if batch.remaining_budget < item.fuel_quota:
                        if self._running_count == 0:
                            # 余量不足，且没有在跑项可结算释放预算 → 本批剩余未检
                            self._drain_batch_as_skipped(batch)
                            continue  # 队列可能还有其它批次的工作，重新评估
                        # 还有在跑项，等其结算退回额度后再试
                        break

                    # 有空位且余量够下一项：预留足额单项额度并启动
                    self._queue.popleft()
                    batch.remaining_budget -= item.fuel_quota
                    batch.reserved += item.fuel_quota
                    self._running_count += 1
                    item.status = ItemStatus.RUNNING
                    item.reason = ""
                    task = asyncio.create_task(self._run_job(job))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)

                # 能启动的都已启动，等待新提交或在跑项结算事件
                await self._cond.wait()

    async def _run_job(self, job: _Job) -> None:
        batch = self._batches[job.batch_id]
        item = job.item
        loop = asyncio.get_running_loop()
        try:
            result: RunResult | None = await loop.run_in_executor(
                self._executor,
                self._validator.run,
                item.conn_limit,
                item.timeout_ms,
                item.fuel_quota,
            )
            fatal_error = None
        except Exception as exc:  # pragma: no cover - 防御性兜底
            result = None
            fatal_error = str(exc)

        async with self._cond:
            batch.reserved -= item.fuel_quota
            self._running_count -= 1

            if result is None:
                # 报错也照结算：预留额度全部计为实际消耗
                item.status = ItemStatus.NO_CONCLUSION
                item.verdict = f"未得出结论：执行报错 {fatal_error}"
                item.consumed = item.fuel_quota
                item.reason = fatal_error or "执行报错"
            else:
                item.consumed = result.consumed
                item.verdict_code = result.verdict_code
                item.verdict = result.verdict
                if result.out_of_fuel or result.error is not None:
                    item.status = ItemStatus.NO_CONCLUSION
                    item.reason = result.error or "指令额度不足"
                elif result.verdict_code == 0:
                    item.status = ItemStatus.PASSED
                else:
                    item.status = ItemStatus.FAILED

            # 按实际消耗扣减，未用额度退回剩余预算
            unused = item.fuel_quota - item.consumed
            batch.remaining_budget += unused
            batch.settled_consumed += item.consumed
            batch.history.append(
                {
                    "item_id": item.id,
                    "env_name": item.env_name,
                    "quota": item.fuel_quota,
                    "consumed": item.consumed,
                    "returned": unused,
                    "status": item.status.value,
                }
            )
            if self._batch_is_finished(batch):
                batch.done = True
            self._cond.notify_all()

    def _drain_batch_as_skipped(self, batch: Batch) -> None:
        """余量不足且无在跑项：队列中属于本批的剩余项全部标为未检。"""
        remaining: deque[_Job] = deque()
        while self._queue:
            job = self._queue.popleft()
            if job.batch_id == batch.id:
                job.item.status = ItemStatus.SKIPPED
                job.item.verdict = "未检：整批剩余预算不足，且无在跑项可结算"
                job.item.reason = "预算不足"
            else:
                remaining.append(job)
        self._queue = remaining
        if self._batch_is_finished(batch):
            batch.done = True

    def _batch_is_finished(self, batch: Batch) -> bool:
        return all(
            item.status
            not in (ItemStatus.QUEUED, ItemStatus.RUNNING)
            for item in batch.items
        )
