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

                page = await client.get("/")
                assert page.status_code == 200
                assert "Wasm".encode() in page.content

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
