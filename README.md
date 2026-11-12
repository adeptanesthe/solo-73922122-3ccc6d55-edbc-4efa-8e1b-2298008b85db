# 公交运营统计代填报与客流补充隐藏 API

本服务在保留原有「站间客流补充隐藏」接口的基础上，新增**月度线路里程代填报**能力：公交运营统计员通过 HTTP API 提交某月核准的线路里程，服务以 Playwright 浏览器登录监管门户、逐行保存草稿并最终提交；请求与回执持久化在 SQLite，支持断流/重启后续办。

## 功能总览

### 代填报（新增）

- 接收 `YYYY-MM` 月份与 **1～20 条唯一线路 ID + 非负整数运营公里数**。
- 门户地址与演示账户由配置固定（环境变量），API 调用方无法覆盖；**只能通过网页（Playwright）操作门户**，没有任何门户直连特权。
- 浏览器流程：登录 → 打开当月页面 → 仅对请求中的线路新增/逐行保存草稿 → 提交前读取**完整草稿**并逐条核对 → 最终提交。
- **只填写请求中的线路，不删除、不改写其他线路**（演示门户草稿夹预置了额外线路用于验证）。
- 门户**每月仅能提交一次、提交后不可修改**；服务据此做幂等与冲突判定。
- **同月同内容可重试**（返回同一任务，续办）；**同月异内容拒绝**（HTTP 409）。
- **同一时刻只运行一个填报任务**，其余请求落库排队，FIFO 处理。
- **断流/重启可续办**：续办先查看该月「已提交页」——内容一致才保存回执并报成功；内容不同报冲突；尚未提交才继续填报。

### 客流补充隐藏（原接口，完全保留）

`/suppress` 等原有接口、字段别名与行为保持不变，详见下文「原接口说明」。

## 快速启动（Docker Compose）

```bash
docker compose up --build -d
```

启动两个容器：

| 服务 | 容器 | 说明 |
| --- | --- | --- |
| `portal` | bus-regulator-portal | 本地监管门户演示站（仅网页，stdlib 实现，端口 8010 仅容器内） |
| `api` | bus-filing-api | FastAPI + Playwright(Chromium) + SQLite，对外端口 8000 |

- Swagger UI：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>
- 健康检查：<http://localhost:8000/health>

停止与清理：

```bash
docker compose down            # 停止
docker compose down -v         # 同时删除 SQLite 数据卷
```

修改宿主机端口：

```bash
HOST_PORT=18080 docker compose up --build -d
```

镜像以非 root 用户运行，带容器健康检查；SQLite 数据保存在命名卷 `filing-data`（容器内 `/data/filing.db`），重启不丢任务。

## 代填报 API

所有接口均在 `/docs` 提供可交互文档。

### 1. 提交代填报请求

`POST /filing/tasks` → `202 Accepted`

```json
{
  "month": "2026-11",
  "lines": [
    {"line_id": "L-100", "kilometres": 5200},
    {"line_id": "L-101", "kilometres": 3380}
  ]
}
```

| 字段 | 规则 |
| --- | --- |
| `month` | `YYYY-MM`，月份必须 01～12 |
| `lines` | 1～20 条；`line_id` 非空且唯一；`kilometres` 为非负整数 |

请求先落库（状态 `accepted`），再由唯一后台 worker 驱动浏览器处理。响应：

```json
{
  "task_id": "8c2f9a1e…",
  "month": "2026-11",
  "state": "accepted",
  "lines": [ … ],
  "content_hash": "…",
  "attempt_count": 0,
  "receipt": null,
  "error": null,
  "created_at": "…",
  "updated_at": "…"
}
```

任务状态：`accepted`（排队）→ `running`（浏览器处理中）→ `success` / `conflict`（终态）；遇到可恢复的门户/网络错误为 `failed`，可续办。

- **同月同内容**：返回同一个 `task_id`（幂等重试），不新建任务。
- **同月异内容**：返回 `409 content_conflict`，拒绝受理。
- 校验失败：`422`。

### 2. 查询状态与回执

- `GET /filing/tasks`：全部任务，可选 `?month=2026-11` 按月过滤。
- `GET /filing/tasks/{task_id}`：单任务状态；成功时 `receipt` 含门户完整行数据与核对结论。不存在返回 `404`。

回执示例（注意 `portal_rows` 同时包含请求线路与门户既有的额外线路，证明未删除其他线路）：

```json
{
  "state": "success",
  "receipt": {
    "receipt_id": "1f3d9b6e…",
    "month": "2026-11",
    "submitted": true,
    "portal_rows": {
      "L-100": 5200,
      "L-101": 3380,
      "L-SEED-01": 120,
      "L-SEED-02": 88
    },
    "requested_lines_verified": true,
    "resumed": false,
    "note": "最终提交成功",
    "confirmed_at": "2026-11-12T01:30:00+00:00"
  }
}
```

### 3. 续办

`POST /filing/tasks/{task_id}/resume`

进程重启、逐行保存或最终提交后断流时调用。续办严格按以下顺序：

1. **先打开该月已提交页**：
   - 已提交且**每条请求线路/里程完全一致** → 保存回执，返回 `success`（`receipt.resumed=true`）。
   - 已提交但内容**不一致** → 返回 `conflict`，不改门户（门户已不可修改）。
2. **尚未提交** → 继续未完成的逐行保存，提交前再次完整复核草稿，再最终提交。

## 断流与重启的正确性设计

- 每个请求在任何浏览器动作之前就已写入 SQLite；任务状态、尝试次数、错误与回执都持久化。
- 门户的「保存本行草稿」与「最终提交」都是**先落服务端效果、再回响应**。演示门户可注入断流：响应体写到一半即切断底层连接，模拟「已生效但回执断流」。
- 浏览器端对每个步骤做传输级重试，并在重试时**重读门户实际状态**：
  - 行保存后以重定向回的月份页面复核该值，未生效则重存；
  - 最终提交后若发现页面已是「已提交」，直接读取已提交内容核对，而不是再次提交。
- 进程异常退出会把任务留在 `running`；worker 启动时自动将其重置为 `accepted`（`recover_stale_running`），随后续办。
- 单任务锁保证全进程同一时刻只有一个浏览器任务，避免并发重复提交。

### 演示门户的故障注入接口（仅演示/测试用）

| 接口 | 作用 |
| --- | --- |
| `POST /sim/reset` | 清空会话与各月数据 |
| `POST /sim/fail?action=save&after=N` | 第 N 次「保存草稿」在生效后断流 |
| `POST /sim/fail?action=submit&after=1` | 「最终提交」生效后断流 |
| `GET /sim/state` | 查看各月草稿/已提交内容与注入状态 |

## 本地开发与测试

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
python -m playwright install chromium   # 需具备 Chromium 系统依赖

# 仅单元/HTTP 测试（无需浏览器）
python -m unittest discover -s tests -v

# 含真实 Chromium 的端到端测试（需要可运行的浏览器）
RUN_PLAYWRIGHT_TESTS=1 python -m unittest tests.test_filing_e2e -v
```

测试分层：

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_filing_models.py` | 请求模型与校验规则 |
| `tests/test_filing_db.py` | SQLite 持久化、内容哈希、幂等/冲突、状态机 |
| `tests/test_portal_app.py` | 演示门户 HTTP 行为、每月一次提交、断流注入 |
| `tests/test_filing_api.py` | FastAPI 路由、状态码、409/422 映射（假 worker） |
| `tests/test_filing_e2e.py` | 真实 Playwright + 门户 + SQLite 全链路与续办 |
| `tests/test_solver.py`、`tests/test_api.py` | 原有客流隐藏功能回归 |

本地手动跑起门户与 API：

```bash
python -m app.portal_app                     # 演示门户，默认 :8010
PORTAL_BASE_URL=http://127.0.0.1:8010 \
FILING_DB_PATH=./filing.db \
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORTAL_BASE_URL` | `http://portal:8010` | 门户地址（配置固定，API 不可覆盖） |
| `PORTAL_USERNAME` / `PORTAL_PASSWORD` | `demo` / `demo-pass` | 演示账户 |
| `FILING_DB_PATH` | `/data/filing.db` | SQLite 文件路径 |
| `PLAYWRIGHT_HEADLESS` | `1` | 是否无头运行 Chromium |
| `BROWSER_TIMEOUT_MS` | `20000` | 浏览器单步超时 |
| `STEP_MAX_ATTEMPTS` | `4` | 单步断流重试次数 |
| `PORTAL_SEED_LINES` | `L-SEED-01:120,L-SEED-02:88` | 演示门户草稿夹预置的额外线路 |

## 目录结构

```text
app/
  main.py                 FastAPI 应用、生命周期、原客流隐藏接口
  solver.py               客流补充隐藏求解器（原有）
  portal_app.py           本地监管门户演示站（stdlib，含断流注入）
  filing/
    config.py             固定门户地址/账户与运行配置
    models.py             填报请求/响应模型与校验
    db.py                 SQLite 持久化、内容哈希、幂等与冲突
    browser_client.py     Playwright 门户客户端（登录/读页/逐行保存/提交）
    worker.py             单任务 worker、续办状态机、冲突判定、回执
    api.py                /filing 路由
tests/                    单元、HTTP、门户与 Playwright E2E 测试
examples/                 客流隐藏与代填报请求/响应示例
Dockerfile                API 镜像（含 Chromium 与系统依赖）
Dockerfile.portal         轻量门户镜像
docker-compose.yml        portal + api 两服务与数据卷
```

---

## 原接口说明：客流补充隐藏

FastAPI 服务：发布站间客流矩阵时保留真实行列合计，先隐藏 `1..k-1` 人的小客流单元，再以**最少格数**补充隐藏其他单元，使每个先隐藏单元在符合公开值及行列合计的所有非负整数矩阵中至少有两个可行值。发布矩阵用 `null` 表示隐藏格；响应同时给出每个先隐藏单元的最小、最大可行值。

`POST /suppress`

```json
{
  "row_stations": ["人民广场", "陆家嘴"],
  "column_stations": ["静安寺", "徐家汇", "世纪大道"],
  "matrix": [[1, 2, 3], [4, 5, 6]],
  "k": 2
}
```

| 字段 | 说明 |
| --- | --- |
| `row_stations` | 行站名，2～3 个，非空 |
| `column_stations` | 列站名，2～4 个，非空 |
| `matrix` | 2～3 行、2～4 列整数矩阵，每格 `0..10000` |
| `k` | 整数阈值，`k >= 2`；值为 `1..k-1` 的单元为先隐藏单元 |

字段别名（保持兼容）：`row_stations/row_names/rowLabels/rows`、`column_stations/col_station/column_names/cols`、`matrix/flow_matrix/flows/data`、`k/threshold`。兼容路由 `/protect`、`/api/v1/suppress`、`/api/suppress` 均保留。校验失败或无解返回 HTTP `422`。求解规则与无解示例见 git 历史与 `examples/` 下原始示例。
