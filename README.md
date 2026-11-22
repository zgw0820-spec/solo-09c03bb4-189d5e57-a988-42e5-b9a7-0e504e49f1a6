# 代理换版环境配置 Wasm 校验服务

代理换版前，逐套核对各环境的**连接上限**与**请求超时**。内置一个 Wasm 校验器，
由 Wasmtime 执行；FastAPI 网页支持一次提交多套环境配置，并填写**单项指令额度**
与**整批指令预算**。

## 功能与规则

| 需求 | 实现 |
| --- | --- |
| Wasmtime 运行内置 Wasm 校验器 | `app/validator.wat`（WAT 文本格式，由 Wasmtime 编译），封装在 `app/wasm_runner.py` |
| 同一校验器只编译一次 | 进程 lifespan 中构造一个 `ValidatorEngine`，内部持有唯一 `Module`，所有任务复用 |
| 各项从独立初始状态开始 | 每次校验新建独立 `Store` + `Instance`（测试 `test_each_item_starts_from_independent_initial_state` 证明可变全局互不影响） |
| 最多并行两项 | 2 worker 线程池 + 单派发协程，`MAX_PARALLEL = 2`，高频轮询实测最大在跑数 = 2 |
| 给在跑每项预留足额指令预算 | 启动前校验 `剩余预算 >= 单项额度`，启动时立即冻结（预留）该额度 |
| 有空位且余量够才启动 | 派发器同时检查并行空位与批次剩余预算 |
| 结束后按实际消耗扣减、退回未用额度 | 以 Wasmtime fuel 计量：`实际消耗 = 预留额度 - 结束时剩余 fuel`，未用部分退回批次余额 |
| 报错也照此结算 | 执行异常按全额预留结算并标记“未得出结论”；fuel 耗尽（`OUT_OF_FUEL`）同样按已耗 fuel 全额结算 |
| 余量不足则等在跑项结算 | 仍有在跑项时排队等待其结算退回额度后再试 |
| 最终不足时剩余配置标为未检 | 无在跑项且余额 < 单项额度时，队列中本批剩余项全部置为 `skipped`，网页显示“未检”及原因 |
| 网页逐项显示结论/原因与指令消耗 | `app/static/index.html`：状态徽章、结论文案、未结论原因、预留额度、实际消耗，400ms 轮询 |
| Docker Compose 启动 | 见下文 |

### 校验器结论码

`validate(conn_limit, timeout_ms) -> i64`

- `0` 通过（连接上限 1–100000，请求超时 1–60000 毫秒，边界值通过）
- `1` 连接上限超限；`2` 请求超时超限；`3` 连接上限过小；`4` 请求超时过小
- 每次校验含固定 2000 轮处理循环，正常一次约消耗 **18,030–18,042 fuel**，消耗稳定可观测

## 目录结构

```
app/
  validator.wat     内置 Wasm 校验器（WAT）
  wasm_runner.py    Wasmtime 封装：编译一次、独立 Store、fuel 结算
  scheduler.py      批量调度：并行上限 2、预留/结算/退回、预算不足标未检
  main.py           FastAPI 入口与 REST API
  static/index.html 提交与结果网页
tests/              pytest 测试（14 项）
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```

## 用 Docker Compose 启动

```bash
docker compose up --build
# 打开 http://localhost:8000
```

## 本地运行（无 Docker 时）

```bash
python3 -m venv --without-pip .venv          # 本机无 ensurepip 时的引导方式
.venv/bin/python <pip-wheel>/pip install -r requirements-dev.txt
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
.venv/bin/python -m pytest -q
```

## API

- `POST /api/batches`：`{per_item_fuel, total_budget, configs:[{env_name, conn_limit, timeout_ms}]}`
- `GET /api/batches/{id}`：批次状态，含逐项 `status/verdict/reason/fuel_quota/consumed`
  与批次账本 `total_budget / remaining_budget / reserved / settled_consumed`（恒满足
  `总预算 = 已结算消耗 + 剩余预算`）
- 交互式文档：`GET /docs`

## 实测结果（本机 uvicorn 实跑，非仅单测）

1. **并行结算**：6 套配置（含 5 种结论路径），高频轮询观测最大在跑项数 = **2**；
   6 项全部结算，单项实际消耗 18,030–18,042 fuel，未用额度全部退回，账本守恒。
2. **预算不足**：单项额度 20,000、整批预算 40,000、4 套配置 → 前 2 项并行通过，
   余额 3,916 < 20,000 且无在跑项，后 2 项标记**未检（预算不足）**，消耗为 0，账本守恒。
3. **额度耗尽**：单项额度 500（低于一次校验所需）→ 2 项均为**未得出结论（OUT_OF_FUEL）**，
   各按 500 fuel 全额结算，批次余额 10,000 − 1,000 = 9,000。
4. 输入校验：非法额度返回 422，未知批次返回 404，首页返回 200。
5. `pytest -q`：**14 passed**。

## 待办 / 已知限制

- 当前交付环境未安装 Docker daemon，`docker compose up --build` 仅做了 YAML 与
  Dockerfile 配置核对，**未在本机实际构建镜像运行**；需在有 Docker 的环境验证一次。
- 批次状态保存在进程内存，重启后历史批次丢失；如需持久化可接 SQLite/Redis。
- 校验窗口（1–100000 / 1–60000ms）与固定工作量目前写死在 WAT 中，后续可改为导入。
- 线程池承载同步 Wasm 调用，并行度固定为 2；若后续要更高并行度，可改异步 binding 或调参。
