# 焰谷热程变点研究档案

焰谷工业烘焙研究组的 16 位工艺分析员把独立批次温度序列整理为可复查的离线巡查假设。上游提供采集文件 `yg-thermal-1-202611.csv`，本系统替代人工在表格中按固定分钟切段、目测波动和反复调罚项的工作，下游由负责人审阅后供现场巡查员下载 CSV。人工固定分钟切分忽略真实位置、对罚项不敏感、把波动直接解释成工艺变更，是要解决的三个具体问题。

四个固定 seed 合成批次各 300 点：均值阶跃、平滑漂移、纯噪声、恒定对照，共 1200 点。A 批 min_size12 / max_segments8 / β80 的实际结果是 3 段、断点 90 和 200、RSS 356.09341455693374、总目标 516.0934145569337。这里没有在线告警或原因识别。

## 启动和账号

```sh
cp .env.example .env
docker compose up --build -d --wait
docker compose exec -T backend python -m scripts.seed_demo
```

|入口|地址|
|---|---|
|浏览器|http://localhost:18228|
|API / Swagger|http://localhost:19228/docs|
|PostgreSQL|127.0.0.1:17728|

|账号|密码|页面和权限|典型动作|
|---|---|---|---|
|admin@yangu.demo|Yangu2026!|全部页面、写入与审阅|确认或退回巡查说明|
|analyst@yangu.demo|Yangu2026!|全部页面、底稿和研究写入|导入、修订观测、排队分段、写说明；审批403|
|viewer@yangu.demo|Yangu2026!|读取底稿、结果、说明和审计|下载已确认 CSV；所有写入403|

首次后端启动自动 seed。脚本按自然键幂等，重复运行不新增、不覆盖已有底稿和人工说明。`.env.example` 列出端口、数据库、JWT_SECRET、SEED_DEMO；部署须设置自己的密码和 JWT secret。源码无外部平台令牌。

恰好五个服务 frontend / backend / worker / db / redis，各有健康检查。Nginx `/api/` 代理移除前缀，与后端路由一致；前端探针访问 127.0.0.1。backend 等待数据库和 Redis，worker 再等待 backend；Celery 通过 Redis 真正排队计算。共享 artifacts 卷保存运行 JSON。停止清除本题数据：`docker compose down -v --remove-orphans`。

## 一天的工作剧本

1. 09:00，分析员陈禾打开“热程首页”，先读离线模型的解释边界。
2. 到“温度底稿”选择 `YG-2411-A 阶跃焙程`，核对 `yg-thermal-1-202611.csv`。每 30 秒一点；A 批原记录从 2026-11-10 09:00:00 UTC 开始，页面按浏览器时区显示。
3. 核对温度、连续序号和带时区时间。页面允许导入带 points 数组的 JSON（每批 ≤600 点），也可修订单点；已有运行仍保留当时序列。大文件走“CSV 断点续传”：分块并发上传，断网或重启后按服务端缺失块续传，结束声明（点数 + 整文件 SHA-256）与连续序号、时间、温度全部核对通过后，批次才原子进入底稿。
4. 在“分段配置”预先设 min_size=12、max_segments=8、当前罚项 β=80，路径 `[0,20,80,320,1280]`，建立研究。
5. 真实队列执行精确 DP。结果显示段区间 1–90、91–200、201–300；断点 90 表示第 90 个点仍在左段，第 91 点开始右段。
6. 展开各预设罚项的段数、断点和目标，并对照相邻段均值差。路径反映参数选择敏感性，不能当断点置信区间。
7. 到“巡查审阅”选择成功运行，提交包含全部分段快照的巡查假设和现场核对行动，不认定波动原因。
8. 负责人赵砚登录确认或退回；巡查观察员吴川只能下载已确认 CSV。导出含指纹、均温、RSS、区间与人工意见。
9. 在“研究留痕”核对导入、点修订、任务、审阅和导出记录；HTTP 响应含 X-Request-ID。

## 算法、坐标和失败态

给定温度 y、最小段长 m 和段数上界 Kmax，段成本 `C(a,b)=sum(y[a:b]^2)-sum(y[a:b])^2/(b-a)`，数组采用左闭右开 `[a,b)`。精确 DP 为 `D[k,b]=min_a(D[k-1,a]+C(a,b))`，只访问满足最小段长的前切点。可行 K 上界为 `min(Kmax,floor(N/m))`；Kmax 只是上界，即使 Kmax>N 仍可求解。只有 m>N 才没有任何合法段。N≤600、Kmax≤12。

选择目标 `D[K,N]+β*(K-1)`；首段不受罚，β 的单位与 RSS 一样是温度平方。给定多个预先输入的 β（以及当前 β），复用同一个 DP 的最优 RSS frontier，不为每个 β 重建 DP。结果保存 dp_builds=1、每个 K 的最优 RSS 和所有路径分段。时间复杂度 O(Kmax N²)，状态 O(Kmax N)。

累计成本先减去首个观测，再除以最大中心化绝对偏差，在 NumPy longdouble 中累计一、二阶矩；均值、RSS 和罚项目标换回原单位。零方差的 scale 取1，恒温合法，β=0 也选择一段。中心化/缩放防止大偏移小方差的直接相减消去，但不能恢复输入 double 已经丢失的精度。

平局使用相对 64×longdouble eps 的舍入容差。总目标相同优先更少段；固定 K 在相同数值成本时取更早的最后前切点，递归沿用这一规则。没有随机切点。longdouble 精度依平台变化，本次容器为33位精度。精确指动态规划遍历全部可行状态，而非声称无限精度算术。

断点 b 定义为左侧累计样本数。图中线在序号 b 与 b+1 之间；结果表显示起始序号 a+1、结束序号 b（均含），CSV 一致。相邻均值差为右段均值减左段均值，不附带显著性或因果解释。

输入每批1–600点，序号从1连续唯一，带时区时间按序号严格递增，温度有限且绝对值≤1e12；重复时间、跳号、倒序、无时区、非有限值、负罚项、重复单一罚项路径和非法参数均拒绝。worker 独立重验，历史坏任务会落 failed，不生成伪分段。指纹包含批次元数据、按序号排序的全部点字段、配置和算法版本，对行序稳定、字段修改敏感。

## CSV 大文件断点续传

JSON 粘贴通道只适合 ≤600 点的小批；逗号分隔文件走独立的续传通道，单文件上限 200MiB、最多 100000 点、20000 块。

- **字节级流式解析（RFC 4180）**：解析器每次只吃一个分块，引号字段可含换行、逗号与转义引号，CRLF/LF/CR 都支持，自动跳过唯一 UTF-8 BOM 与单行三列表头。解析状态（模式、当前半条记录、未拼完的 UTF-8 字节）是 JSON 原生类型，随分块落库；在半个字符、半个引号字段或半条记录处中断，重启后用同样字节序列即可继续。暂存额外内存只与当前半条记录有关（单记录 65536 字符上限），不随文件总大小增长。
- **分块交错与断点续传**：客户端按固定 `chunk_size` 切分并并发上传，分块可乱序到达；服务端只按字节顺序消费连续前缀，未到前缀的块先落库不解析。进度以服务端缺失块集合为准，前端另在 localStorage 记录未完成任务，换标签页或重启都能续。
- **并发重传与冲突**：同段重传字节一致则幂等、不重复计入；同段字节不一致或字节范围与已存块重叠，保留原进度并把整个上传置为 `quarantined`，返回 409 冲突。应用级会话锁与 PostgreSQL `SELECT … FOR UPDATE` 行锁双重串行化临界区。
- **结束声明与原子发布**：`finish` 提交点数与整文件 SHA-256。服务端流式重算摘要、核对连续字节范围、解析收尾（暴露未闭合引号/坏 UTF-8）、连续序号、带时区严格递增时间与有限温度，全部通过后在**单事务**内创建 `published` 批次并搬运全部温度点；任一项失败则回滚并隔离，不产生半成品底稿。结束与发布路径用 `yield_per` 游标分批，不一次性载入全文件。
- **隔离与读取面**：损坏批次停留 `quarantined`，页面展示隔离原因且不阻塞其他批次；只有 `published` 批次出现在 `/batches`、`/points` 和分段运行创建路径，研究端永远读不到未发布或已隔离数据。分块、暂存点、解析状态与 `upload.created/quarantined/published` 审计记录在发布后保留备查。

独立同方差的片段误差是工作近似。平滑漂移也可能切成阶跃；β 路径不是置信区间，分段不是在线警报，均值差不是因果归因。观察过程、测量误差和现场条件必须另行核对。

## 工程结构与开发

|层|技术与职责|
|---|---|
|前端|React18 / TypeScript / Vite6.4.6，SVG按真实序列画图，Work Sans+Noto Serif SC|
|后端|Python3.11 / FastAPI / SQLAlchemy，JWT三角色、审计、错误边界|
|计算|NumPy longdouble，精确DP和单frontier罚项路径，Celery5.4/Kombu5.4.2|
|存储|PostgreSQL16 / Redis7，业务五实体 + User/AuditLog，CSV 续传三表（CsvUpload/CsvUploadChunk/CsvStagedPoint）|

75 个有职责的 Python/TS/TSX/CSS 文件，不含空包、依赖或构建产物。五个业务实体为 ThermalBatch → TemperaturePoint → SegmentationRun → Segment → InspectionNote，贯穿 ORM、schema、服务、路由、前端API/store和六个业务页面。后端 `src/{routes,controllers,services,models,schemas,middlewares,utils,constants,config}`；前端 `src/{api,stores,types,components/common,hooks,pages,router,utils,constants}`。

共享枚举 Role、RunStatus、NoteStatus 位于后端 `src/constants/enums.py` 与前端 `src/constants/enums.ts`（前端另含 UploadStatus）。RequestID/Error 两个中间件负责追踪与统一错误；ThermalChart、RunSummary、RunTable 在多页复用，另有 PenaltyPath、SegmentTable、UploadPanel 和 common 组件。useAsync/useHash/useResumableUpload 与 format 工具处理交互和格式。说明冻结 segment_snapshot，底稿修订不能改写已批准导出。

后端测试在 `backend/tests/`（pytest + FastAPI TestClient + SQLite 内存文件库，无需 Postgres/Redis）：覆盖流式解析器的任意字节切分与状态 JSON 往返、半字符/半记录恢复、分块幂等与并发重传、重叠字节冲突隔离、损坏批次隔离不阻塞他人、结束声明/摘要核对、原子发布与研究读取面过滤、暂存内存有界等 20 项。

本地前端开发 `cd frontend && npm ci && npm run dev`；`npm run build` 执行 tsc 和 Vite。后端安装 requirements.txt，设置 DATABASE_URL/REDIS_URL/JWT_SECRET 后运行 uvicorn；数据库须先就绪。迁移目录提供初始表结构说明，当前使用 ORM metadata 创建表，没有生产版本迁移链。许可证见 LICENSE（MIT）。

测试脚本、关键 JSON、CSV、截图、日志和完整验收记录位于批次 `test-artifacts/gb-0024/`，其中 TESTING.md 是实际证据索引。测试的 12 点 UI 导入与 4 点手算批次是额外测试输入，不能冒充原始1200点 fixture。未验证生产并发、外部真实设备、时间不等间隔下的持续时间加权或超600点。这里按样本个数约束段长，不按分钟加权。字体断网会回退本地衬线/无衬线字体。
