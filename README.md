# 代理换版环境配置 Wasm 校验服务

代理换版前，逐套核对各环境的**连接上限**与**请求超时**。内置一个 Wasm 校验器，
由 Wasmtime 执行；FastAPI 网页支持一次提交多套环境配置，并填写**单项指令额度**
与**整批指令预算**。

## 功能与规则

| 需求 | 实现 |
| --- | --- |
| Wasmtime 运行内置 Wasm 校验器 | `app/validator.wat`（WAT 文本格式，由 Wasmtime 编译），封装在 `app/wasm_runner.py` |
| 同一校验器只编译一次 | 进程 lifespan 中构造一个 `ValidatorEngine`，内部持有唯一 `Module`，所有任务复用 |
| 复用空闲 Store/Instance | `app/instance_pool.py`：进程内至多保留 **2 套**，一套同一时刻只借给一项校验（借出阻塞/归还唤醒） |
| 每项从独立初始状态开始 | 复用的实例在**每次执行前**把线性内存写回创建时快照、把导出的可变全局恢复为创建时初值；复位在设置 fuel 之前完成，宿主侧操作不耗燃料，因此不计入批次预算 |
| 陷阱/复位失败即淘汰 | 任何 Wasm 陷阱（含 `OUT_OF_FUEL`）或复位失败都会关闭并淘汰该实例，不回空闲池；已得结论与该项结算照常保留，后续项用干净实例继续 |
| 最多并行两项 | 2 worker 线程池 + 单派发协程，`MAX_PARALLEL = 2`，高频轮询实测最大在跑数 = 2、实例池最大在用数 = 2 |
| 给在跑每项预留足额指令预算 | 启动前校验 `剩余预算 >= 单项额度`，启动时立即冻结（预留）该额度 |
| 有空位且余量够才启动 | 派发器同时检查并行空位与批次剩余预算 |
| 结束后按实际消耗扣减、退回未用额度 | 以 Wasmtime fuel 计量：`实际消耗 = 预留额度 - 结束时剩余 fuel`，未用部分退回批次余额 |
| 报错也照此结算 | 执行异常按全额预留结算并标记“未得出结论”；fuel 耗尽（`OUT_OF_FUEL`）同样按已耗 fuel 全额结算 |
| 余量不足则等在跑项结算 | 仍有在跑项时排队等待其结算退回额度后再试 |
| 最终不足时剩余配置标为未检 | 无在跑项且余额 < 单项额度时，队列中本批剩余项全部置为 `skipped`，网页显示“未检”及原因 |
| 网页逐项显示结论/原因与指令消耗 | `app/static/index.html`：状态徽章、结论文案、未结论原因、预留额度、实际消耗，400ms 轮询 |
| 网页查看本进程实例池计数 | 结果面板显示实例上限、**实际新建**、**复用次数**、淘汰（含复位失败）、复位次数、当前在用/空闲；另有 `GET /api/instances` |
| Docker Compose 启动 | 见下文 |

### 校验器结论码

`validate(conn_limit, timeout_ms) -> i64`

- `0` 通过（连接上限 1–100000，请求超时 1–60000 毫秒，边界值通过）
- `1` 连接上限超限；`2` 请求超时超限；`3` 连接上限过小；`4` 请求超时过小
- 每次校验含固定 2000 轮处理循环，正常一次约消耗 **18,030–18,042 fuel**，消耗稳定可观测

## 目录结构

```
app/
  validator.wat     内置 Wasm 校验器（WAT；导出 memory 与可变全局 check_count）
  instance_pool.py  Store/Instance 复用池：至多 2 套、独占借用、复位快照、淘汰与计数
  wasm_runner.py    Wasmtime 封装：编译一次、借池执行、复位、fuel 结算
  scheduler.py      批量调度：并行上限 2、预留/结算/退回、预算不足标未检
  main.py           FastAPI 入口与 REST API
  static/index.html 提交与结果网页（含实例池新建/复用计数）
tests/              pytest 测试（26 项）
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
- `GET /api/batches/{id}`：批次状态，含逐项 `status/verdict/reason/fuel_quota/consumed`、
  批次账本 `total_budget / remaining_budget / reserved / settled_consumed`（恒满足
  `总预算 = 已结算消耗 + 剩余预算`），以及本进程实例池累计统计 `instances`
- `GET /api/instances`：本进程实例池累计统计
  `max_instances / created / reused / evicted / reset_failures / resets / runs / live / idle / in_use`
- 交互式文档：`GET /docs`

## 实测结果（本机 uvicorn 实跑，非仅单测）

同一进程内依次提交以下批次：

1. **并行复用**：20 套配置 → 高频轮询观测最大在跑项数 = **2**、实例池历史最大在用
   = **2**；本进程实际**新建 2 套、复用 18 次**（`created=2, reused=18, runs=20`），
   2 套常驻空闲，账本守恒。
2. **全结论路径**：6 套配置覆盖全部 5 种结论 → 通过 18,042 / 超上限 18,030 /
   超时超限 18,034 / 下限过小 18,038 / 超时过小 18,041 / 边界通过 18,042，
   全部复用已有 2 套实例（`reused` 累计到 24），逐项消耗与逐项新建时一致。
3. **预算不足**：单项额度 20,000、整批预算 40,000、4 套配置 → 前 2 项并行（通过/不通过），
   余额 3,928 < 20,000 且无在跑项，后 2 项标记**未检（预算不足）**，消耗 0，账本守恒。
4. **额度耗尽**：单项额度 500（低于一次校验所需）→ 2 项均为**未得出结论（OUT_OF_FUEL）**，
   每项陷阱后淘汰实例（`evicted=2`），各按 500 fuel 全额结算，余额 10,000 − 1,000 = 9,000；
   已得出的结论与结算不受影响。
5. **淘汰后继续**：紧接着提交 2 套正常额度配置 → 用干净实例全部得出正确结论
   （通过/超上限），`created` 因补建升到 4，`in_use` 归 0。
6. 输入校验：非法额度返回 422，未知批次返回 404，首页返回 200。
7. `pytest -q`：**26 passed**（含实例池独占借用、复位到非零初值、陷阱淘汰、
   复位失败淘汰但照常结算、以及“池化 vs 逐项新建”在 6 种配置 × 10 档燃料
   （含耗尽边界 18,020/18,021、18,042/18,043）下结论/实际消耗/耗尽判定逐一相等）。
8. 网页与 `GET /api/instances` 均展示上述计数（新建、复用、淘汰、复位、在用/空闲、历史最大在用）。

## 待办 / 已知限制

- 当前交付环境未安装 Docker daemon，`docker compose up --build` 仅做了 YAML 与
  Dockerfile 配置核对，**未在本机实际构建镜像运行**；需在有 Docker 的环境验证一次。
- 批次状态保存在进程内存，重启后历史批次丢失；如需持久化可接 SQLite/Redis。
- 校验窗口（1–100000 / 1–60000ms）与固定工作量目前写死在 WAT 中，后续可改为导入。
- 线程池承载同步 Wasm 调用，并行度固定为 2，与实例池上限 2 对齐；若提高线程数而
  不增大实例池，多出的线程会在借实例处等待（池满时阻塞）。
- 复位方式为“宿主写回内存快照 + 设置全局”，对当前校验器（1 页内存、单个可变全局）
  足够；若未来校验器显著增长内存/全局数量，可改为重建实例或分代策略。
