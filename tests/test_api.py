"""FastAPI 端到端测试：提交、轮询、并行结算、预算不足、额度耗尽未结论。"""
import asyncio
import time

import pytest

httpx = pytest.importorskip("httpx")

from app.main import app  # noqa: E402


async def _post_batch(client, payload):
    r = await client.post("/api/batches", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


async def _wait_done(client, batch_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = await client.get(f"/api/batches/{batch_id}")
        assert r.status_code == 200
        body = r.json()
        if body["done"]:
            return body
        await asyncio.sleep(0.05)
    raise AssertionError("批次未在预期时间内结束")


def _payload(n, per_item_fuel, total_budget):
    return {
        "per_item_fuel": per_item_fuel,
        "total_budget": total_budget,
        "configs": [
            {
                "env_name": f"env-{i}",
                "conn_limit": 5000 + i,
                "timeout_ms": 3000 + i,
            }
            for i in range(n)
        ],
    }


def test_api_with_lifespan():
    async def scenario():
        transport = httpx.ASGITransport(app=app)
        # 显式驱动 FastAPI lifespan，确保校验器编译一次、调度器启动
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=transport, base_url="http://t"
            ) as client:
                body = await _post_batch(client, _payload(4, 300_000, 5_000_000))
                assert body["max_parallel"] == 2
                final = await _wait_done(client, body["id"])

                assert final["done"]
                assert all(i["status"] == "passed" for i in final["items"])
                assert all(0 < i["consumed"] < 300_000 for i in final["items"])
                # 账本守恒：总预算 = 已结算消耗 + 剩余预算
                assert final["total_budget"] == (
                    final["settled_consumed"] + final["remaining_budget"]
                )
                assert final["reserved"] == 0
                # 本进程只用两套以内的 Store/Instance，其余校验均复用
                inst = final["instances"]
                assert inst["max_instances"] == 2
                assert inst["created"] <= 2
                assert inst["created"] + inst["reused"] == inst["runs"] == 4
                assert inst["in_use"] == 0

                page = await client.get("/")
                assert page.status_code == 200
                assert "Wasm".encode() in page.content
                assert "实例".encode() in page.content

                standalone = await client.get("/api/instances")
                assert standalone.status_code == 200
                assert standalone.json()["created"] == inst["created"]

    asyncio.run(scenario())


def test_api_parallel_reuse_across_many_items():
    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=transport, base_url="http://t"
            ) as client:
                # 20 项串行/并行混合执行；进程只新建至多 2 套实例并反复复用
                body = await _post_batch(client, _payload(20, 300_000, 20_000_000))
                final = await _wait_done(client, body["id"], timeout=20)
                assert all(i["status"] == "passed" for i in final["items"])
                inst = final["instances"]
                # 进程内至多两套；首发两项可能并发新建，也可能因先后完成而只新建一套
                assert 1 <= inst["created"] <= 2
                assert inst["reused"] >= 18
                assert inst["created"] + inst["reused"] == inst["runs"] == 20
                assert inst["evicted"] == 0
                # 复位开销不进入批次账本：账本仍严格守恒
                assert final["total_budget"] == (
                    final["settled_consumed"] + final["remaining_budget"]
                )

    asyncio.run(scenario())


def test_api_budget_shortfall():
    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=transport, base_url="http://t"
            ) as client:
                # 预算只够 2 项启动（2 * 20000）；4 项中后 2 项在余额不足且
                # 无在跑项可等待时标记为未检
                payload = _payload(4, 20_000, 40_000)
                body = await _post_batch(client, payload)
                final = await _wait_done(client, body["id"])

                statuses = [i["status"] for i in final["items"]]
                assert statuses.count("passed") == 2
                assert statuses.count("skipped") == 2
                for item in final["items"]:
                    if item["status"] == "skipped":
                        assert "预算不足" in item["reason"]
                        assert item["verdict"].startswith("未检")
                assert final["total_budget"] == (
                    final["settled_consumed"] + final["remaining_budget"]
                )

    asyncio.run(scenario())


def test_api_budget_allows_only_one_then_skips_rest():
    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=transport, base_url="http://t"
            ) as client:
                # 预算恰好只够 1 项，3 项中其余 2 项未检
                payload = _payload(3, 20_000, 20_000)
                body = await _post_batch(client, payload)
                final = await _wait_done(client, body["id"])

                statuses = [i["status"] for i in final["items"]]
                assert statuses.count("passed") == 1
                assert statuses.count("skipped") == 2
                assert final["remaining_budget"] < 20_000

    asyncio.run(scenario())


def test_api_out_of_fuel_item_is_no_conclusion_but_settled():
    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=transport, base_url="http://t"
            ) as client:
                # 单项额度 500，低于一次校验所需（约 1.8 万）→ 每项 OUT_OF_FUEL
                body = await _post_batch(client, _payload(3, 500, 10_000))
                final = await _wait_done(client, body["id"])

                assert all(i["status"] == "no_conclusion" for i in final["items"])
                assert all(i["consumed"] == 500 for i in final["items"])
                assert all("OUT_OF_FUEL" in i["verdict"] for i in final["items"])
                # 报错/耗尽也照结算：3 * 500 全部扣减
                assert final["settled_consumed"] == 1500
                assert final["remaining_budget"] == 10_000 - 1500
                # 每项陷阱都会淘汰实例；后续项用干净实例继续，全部得出结论
                inst = final["instances"]
                assert inst["evicted"] == 3
                assert inst["created"] + inst["reused"] == inst["runs"] == 3

    asyncio.run(scenario())


def test_api_trap_evicts_then_clean_instance_continues():
    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=transport, base_url="http://t"
            ) as client:
                # 先耗尽：实例被淘汰；再提交正常批量，用干净实例继续校验
                first = await _post_batch(client, _payload(2, 500, 10_000))
                await _wait_done(client, first["id"])
                st1 = (await client.get("/api/instances")).json()
                assert st1["evicted"] == 2

                second = await _post_batch(client, _payload(2, 300_000, 1_000_000))
                final = await _wait_done(client, second["id"])
                assert all(i["status"] == "passed" for i in final["items"])
                st2 = final["instances"]
                assert st2["evicted"] == 2
                assert st2["created"] + st2["reused"] == st2["runs"] == 4

    asyncio.run(scenario())


def test_api_validation_rejects_bad_input():
    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=transport, base_url="http://t"
            ) as client:
                bad = {
                    "per_item_fuel": 0,
                    "total_budget": 100,
                    "configs": [
                        {"env_name": "x", "conn_limit": 1, "timeout_ms": 1}
                    ],
                }
                assert (await client.post("/api/batches", json=bad)).status_code == 422

                missing = {
                    "per_item_fuel": 100,
                    "total_budget": 100,
                    "configs": [],
                }
                assert (
                    await client.post("/api/batches", json=missing)
                ).status_code == 422

                assert (await client.get("/api/batches/nonexistent")).status_code == 404

    asyncio.run(scenario())
