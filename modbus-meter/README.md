# Modbus TCP 电表试采与用电对账接口

新电表接入能耗库前的单台 Modbus TCP 电表试采服务，以及月末核对总表/支路表
用电账的 CSV 对账接口。

- 试采：提交测点的保持寄存器起始地址（从 0 算）与字数（1 或 2），并给出设备
  的可读区间（含两端）与单次读取上限；接口自动规划读段、按方案读表，返回读段
  与无符号计数（双字按高字在前拼接）。
- 对账：上传网关导出的总表 CSV 与至少一份支路表 CSV（各表计数单位和倍率相同），
  按所有表都有读数的相邻时刻划段，输出各段起止秒与
  “总表增量 - 各支路增量之和”的差额。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

- API：<http://localhost:8000>，Swagger UI：<http://localhost:8000/docs>
- 模拟电表：`localhost:1502`（容器内 `meter:502`），保持寄存器 0..99

在 Swagger 中打开 `POST /read` → Try it out → 直接 Execute 即可对模拟表试采
（请求体默认指向容器内的模拟表）。对账则在 `POST /reconcile` 中分别选择
总表与至少一份支路表 CSV（仓库 `samples/` 下提供了样例）。

## 试采接口

### `POST /read`

请求体：

| 字段 | 说明 |
| --- | --- |
| `points` | 测点列表，`address` 为保持寄存器起始地址（从 0 算），`words` 为字数（1 或 2） |
| `range_start` / `range_end` | 设备可读区间，含两端 |
| `max_read` | 单次读取寄存器字数上限 |
| `host` / `port` / `slave_id` / `timeout` | 设备连接参数，默认指向 Compose 中的模拟表 |

读段规划规则：

1. 每次读取一段连续寄存器，读段整体落在可读区间内，字数不超过 `max_read`；
2. 每个测点必须被某一次读取完整覆盖（不允许跨段拼接）；
3. 先使请求次数最少；次数相同时使读取寄存器总数最少
   （按地址排序后动态规划求解，读段紧贴测点，不多读无用寄存器）。

响应（200）：

- `request_count` / `total_registers`：实际请求次数与读取寄存器总数；
- `segments`：各读段的起止地址（含）、字数、覆盖的测点下标、原始寄存器值；
- `points`：按请求下标返回的测点值，`value` 为无符号计数，
  双字按高字在前（big-endian）拼接。

错误：

- `422`：参数校验失败或无可行方案（测点超出可读区间、字数超过单次上限等），
  `detail` 为中文原因；
- `502`：连接失败或 Modbus 读取失败（含设备异常响应），**不返回部分读数**。

## 对账接口

### `POST /reconcile`

`multipart/form-data` 上传：

| 字段 | 说明 |
| --- | --- |
| `main_csv` | 总表 CSV，必填 |
| `branch_csvs` | 支路表 CSV，可重复该字段上传多份，至少 1 份 |

（为便于脚本调用，也兼容 `main_file` / `main` 与 `branch_files` / `branches`
等字段名；Swagger 中展示的是 `main_csv` / `branch_csvs`。）

#### CSV 格式

每份 CSV **无表头**，每行三列：

```csv
UTC采样秒,高字,低字
```

（若首行是常见表头，例如 `timestamp,high,low` 或 `ts,hi,lo`，会自动跳过。）

- 采样秒为非负整数，同一文件内必须**严格递增**；
- 高字、低字均为 `0..65535` 的 16 位无符号整数；
- 计数按试采接口相同的规则解码：`(高字 << 16) | 低字`；
- 各表的计数单位和倍率必须一致。

#### 样例

`samples/main.csv`（总表）、`samples/branch_a.csv`、`samples/branch_b.csv`
（两份支路表）刻意采用了**错开的采样时刻**，且 `branch_b.csv` 在中途出现
计数下降：

| 表 | 采样时刻（UTC 秒） |
| --- | --- |
| main | 1767225600, 1767225630, 1767225660, 1767225720 |
| branch_a | 1767225600, 1767225660, 1767225690, 1767225720 |
| branch_b | 1767225600, 1767225610, 1767225660, 1767225690, 1767225720 |

三份表共同有读数的时刻为 `1767225600 / 1767225660 / 1767225720`，
因此划出两段。`branch_b` 在第二段的错开采样 `1767225690` 上从 40200 降到
40100（随后在段尾又回到 40250）；按“段内相邻读数”检查仍会判为异常，
所以第二段只列异常文件名、不给出差额。

生成报告：

```bash
curl -sS -X POST http://localhost:8000/reconcile \
  -F 'main_csv=@samples/main.csv;type=text/csv' \
  -F 'branch_csvs=@samples/branch_a.csv;type=text/csv' \
  -F 'branch_csvs=@samples/branch_b.csv;type=text/csv' \
  -o reconciliation.csv
```

报告内容（与 `samples/expected_reconciliation.csv` 一致）：

```csv
segment_start_utc,segment_end_utc,difference,abnormal_files
1767225600,1767225660,20,
1767225660,1767225720,,branch_b.csv
```

#### 对账规则

1. 只使用**所有表都有读数**的采样时刻（交集），以其中相邻两个时刻划一段；
2. 每段差额 = 总表增量 - 各支路表增量之和；
3. 对每份表从段首到段尾检查相邻读数（包括只在个别表中出现的错开采样），
   任一段内出现计数下降，就把该段标为异常：`difference` 留空，
   `abnormal_files` 列出异常表文件名（多份用 `;` 分隔）；
4. 共同采样时刻不足 2 个时无法形成任何一段，返回 `422`，`detail` 为
   “无法对账”。

报告列：

| 列 | 说明 |
| --- | --- |
| `segment_start_utc` | 段起始 UTC 秒 |
| `segment_end_utc` | 段结束 UTC 秒 |
| `difference` | 总表增量 - 各支路增量之和；异常段为空 |
| `abnormal_files` | 该段内出现计数下降的表文件名；正常段为空 |

#### 错误与流式处理

- 任一行不是 3 列整数、寄存器字超出 `0..65535`，或采样秒未严格递增：
  返回 `422`，`detail` 同时给出**文件名和行号**，例如
  `文件 main.csv，第 2 行：采样秒未严格递增：上一行 1，本行 1`；
- 先逐行读完并校验**所有**文件，再生成报告；出现上述错误时**不发送部分报告**；
- 对账时逐行读入输入、逐段写入报告，不把整份输入或报告放入内存；
- 报告完整生成后才开始发送（`text/csv`，下载文件名 `reconciliation.csv`）。

## 模拟电表数据

`simulator/meter_sim.py`：保持寄存器 0..99，可读区间 `[0, 99]`。

| 地址 | 值 | 说明 |
| --- | --- | --- |
| 0..1 | 0x0001, 0x86A0 | 32 位无符号 100000（高字在前） |
| 2..3 | 0x0000, 0x0064 | 32 位无符号 100 |
| 10..13 | 1234, 5678, 65535, 42 | 单字演示值 |
| 其余 | `(addr * 37 + 11) & 0xFFFF` | 确定性填充，便于人工核对 |

## 本地开发（无 Docker）

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
METER_PORT=1502 .venv/bin/python -m simulator.meter_sim &
METER_HOST=127.0.0.1 METER_PORT=1502 .venv/bin/uvicorn app.main:app --port 8000
.venv/bin/pytest            # 规划 + 试采集成 + 对账集成测试
```

环境变量：`METER_HOST` / `METER_PORT` / `METER_SLAVE_ID` / `METER_TIMEOUT`
为 API 请求体连接参数的默认值（Compose 中已注入）。

## 目录结构

```
app/main.py        FastAPI：/read 试采与 /reconcile 对账
app/planner.py     读段规划（动态规划：次数最少 -> 寄存器总数最少）
app/reconcile.py   CSV 流式校验、多路归并划段、逐段写报告
app/registers.py   高字在前的无符号计数解码（试采/对账共用）
samples/           错开采样且中途计数下降的对账样例
simulator/         pymodbus 内存模拟电表
tests/             规划、试采集成与对账测试（无需 Docker）
docker-compose.yml api + meter 两个服务
```
