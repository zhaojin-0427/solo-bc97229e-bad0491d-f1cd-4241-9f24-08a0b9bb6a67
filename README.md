# 高程网格分块仓库（Elevation Grid Warehouse）

水利研究所测绘高程网格的分批接收与按窗口取数系统。Flask 网页 + SQLite 索引 +
本地 64×64 不可变块文件，面向最多 **10 万行 × 10 万列** 的逻辑网格，
单次上传 / 窗口均不超过 **256×256**。

## 1. 需求与实现对照

| 需求 | 实现方式 |
| --- | --- |
| 10 万×10 万网格，不预分配 | 网格尺寸仅作越界边界存于 `meta`；只有至少含一个已填格的块才落盘，初始存储为零 |
| 上传 ≤256×256 无表头整数厘米 CSV + 从 0 起的左上行列 | `elevation/parsing.py` 流式解析、字节数硬上限；拒绝表头/空值/非整数/超长/不等长行 |
| SQLite 保存索引 | `blocks(br,bc)` 主键点查、`commits` 提交历史、`obsolete_files` 回收清单、`meta` 元信息 |
| 本地文件按 64×64 存块 | `elevation/blockstore.py`：固定 4096 格位图 + int32 数组 + CRC32 的二进制块 |
| 重叠格等值复用 | 写事务内逐格比对，等值不产生新版本；整份等值上传不重写任何块 |
| 异值或越界整次拒绝 | 冲突抛 `409`、越界抛 `422`，事务回滚 + 已写新块文件清理，无任何部分更新 |
| 并发上传同样遵守 | 全部写事务 `BEGIN IMMEDIATE` 取全库写锁，上传在 SQLite 层串行化（`busy_timeout` 排队） |
| 跨块上传一起可见，不覆盖他人已成功格 | 一次上传的所有块改动在单个事务提交；冲突先于提交检测 |
| 不把整张网格/全部已填索引载入内存 | 索引只走主键点查与窗口范围扫描；块按需读取；窗口输出矩阵 ≤256×256 |
| 窗口读取只访问相交块 | 仅对窗口覆盖的块坐标范围做 SQL 范围扫描，响应头返回实际访问块数 |
| 预览 / 下载 ≤256×256 窗口 CSV，未填格为空 | `/window` HTML 表格预览（未填格显示 `·`）；`/download` 导出 CSV（未填格为空字段） |
| 每次读取来自同一完整提交状态 | 读事务延迟 `BEGIN` 固定 WAL 快照，窗口内全部块查询落在同一已提交版本 |
| 写盘失败/中断不留部分更新，重启后数据可读 | 块写 tmp + fsync + 原子 `os.replace`；事务与文件两阶段；启动恢复清理 tmp/孤儿/旧版本 |

## 2. 目录结构

```
.
├── app.py                  # Flask 生产入口（create_app 工厂，gunicorn --factory）
├── elevation/
│   ├── config.py           # 全部约束与环境变量配置
│   ├── blockstore.py       # 64×64 块文件格式、原子写、CRC 校验、启动垃圾回收
│   ├── database.py         # SQLite schema、WAL、读写事务、崩溃恢复
│   ├── parsing.py          # 无表头整数 CSV 流式解析与窗口 CSV 渲染
│   ├── service.py          # 事务化导入 / 快照窗口取数（核心业务，与框架无关）
│   ├── handlers.py         # 框架无关的 HTTP 请求处理
│   ├── http_types.py       # Request/Response 抽象
│   ├── templates.py        # 网页 HTML（无模板引擎依赖）
│   └── wsgi_stdlib.py      # 零依赖 WSGI 入口（仅标准库，离线演示用）
├── tests/                  # 61 个单元/端到端测试（含最小 Flask 契约桩）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── run_local.sh            # 零依赖本地启动
└── run_tests.sh
```

## 3. 快速启动

### 方式 A：Docker Compose（推荐，生产形态）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

数据持久化在命名卷 `elevation-data`（容器内 `/data`）。
健康检查：`GET /health`；状态：`GET /api/stats`。

### 方式 B：无 Docker / 离线环境（仅 Python 标准库）

核心引擎与演示服务器不依赖任何第三方库：

```bash
./run_local.sh
# 默认 http://0.0.0.0:8000，数据目录 ./data（可用 ELEV_DATA_DIR 覆盖）
```

> 注意：`run_local.sh` 以 `python3 -I` 隔离模式运行，避免当前目录下
> 被投放的同名模块（如 `json.py`）被解释器加载。

## 4. Web 入口与 HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 首页：仓库状态、上传表单、窗口表单、最近提交 |
| POST | `/upload` | multipart 表单：`csv` 文件 + `row_offset` + `col_offset`；成功 303 回首页 |
| GET | `/window?row=&col=&rows=&cols=` | 窗口 HTML 预览（≤256×256） |
| GET | `/download?row=&col=&rows=&cols=` | 窗口 CSV 下载，未填格为空字段 |
| GET | `/sample.csv` | 示例 CSV |
| GET | `/api/stats` | JSON 统计（已填格、块数、提交数、当前快照） |
| GET | `/health` | 健康检查 |

错误状态码：`400` 解析/参数错误、`409` 异值冲突、`413` 请求体过大、
`422` 越界、`503` 写锁排队超时、`500` 存储故障（均已回滚）。

## 5. 关键设计：为什么不会出现部分更新

一次上传分两个阶段：

1. **文件阶段（可重试、无副作用）**：在单个 `BEGIN IMMEDIATE` 事务内，
   逐块读取旧版本 → 冲突检测 → 合并后把**新版本块写到唯一名新文件**
   （tmp 中 fsync 后原子 rename 进 blocks 目录）。旧文件保持不动。
2. **提交阶段（原子切换）**：所有新块就绪后，在同一事务内 UPSERT `blocks`
   指针、登记旧文件到 `obsolete_files`、写 `commits`，一次 `COMMIT` 生效。

任一阶段失败：SQLite `ROLLBACK` + 本次新块文件全部删除，磁盘与索引回到上传前。
进程崩溃时：未 rename 的 tmp 文件、已 rename 但无索引引用的孤儿块、
被替换的旧版本块，均在下次启动 `initialize()` 时识别并回收；
索引引用的块文件丢失会直接报完整性错误，绝不静默返回错误数据。

## 6. 配置（环境变量）

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `ELEV_GRID_ROWS` / `ELEV_GRID_COLS` | 100000 | 逻辑网格行列上限（变更后与旧数据目录不兼容会拒绝启动） |
| `ELEV_MAX_UPLOAD_ROWS/COLS` | 256 | 单次上传上限 |
| `ELEV_MAX_WINDOW_ROWS/COLS` | 256 | 窗口上限 |
| `ELEV_MAX_BODY_BYTES` | 16777216 | 请求体字节上限 |
| `ELEV_BUSY_TIMEOUT_MS` | 30000 | 写锁排队超时 |
| `ELEV_DATA_DIR` | `/data` | SQLite、块文件、tmp 的根目录 |
| `ELEV_HOST` / `ELEV_PORT` | 0.0.0.0 / 8000 | 零依赖服务器监听地址 |

## 7. 运行测试

```bash
./run_tests.sh
```

61 个测试覆盖：块编解码/CRC、CSV 规则、跨块原子提交、等值复用与异值整次拒绝、
多线程并发（不相交/同格等值/同格异值）、快照隔离、重启持久化、
崩溃垃圾回收、写盘与提交失败回滚、配置漂移防护、完整 HTTP 端到端流程、
Flask 入口接线（契约桩）。

## 8. 块文件格式

每个 `.blk` 文件固定 16 910 字节，小端：

```
magic "EBLK" | version u16 | br u32 | bc u32
| bitmap 512B（4096 位，1=已填，显式写入的 0 与未填格可区分）
| values 4096 × int32（厘米高程）
| crc32 u32（覆盖前面全部字节）
```

文件名 `b{br}-{bc}-{uuid32}.blk`，多进程/多线程并发写也不会碰撞；
块不可变，更新即换新文件，旧版本在无引用后于下次重启回收。
