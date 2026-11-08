# Telegram 投资日报与固定 Markdown 入口开发计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
> 本仓库执行时使用可用的 `executing-plans` 技能；按任务执行，在各阶段评审点停止。本文仅为开发计划，不表示已实施或明天已自动生效。不自动提交 Git，不发送真实 Telegram 测试消息。

**Goal:** Telegram 清晰展示 CNN 恐惧与贪婪指数、VIX、系统大盘技术评分、板块评分，以及全部关注个股中符合条件的进场候选；完整明细和 LLM prompt 汇总到固定目录的每日 Markdown。

**Architecture:** 外部指标经有版本、限流及时间戳约束的 adapter 追加到 Parquet，DuckDB 提供 cutoff 查询；现有 canonical 特征、评分及交易门禁生成统一 digest JSON。Telegram 与 Markdown 从同一 digest 渲染，SQLite 继续只承担调度与消息投递状态。

**Tech Stack:** 现有 Python、Pydantic、PyArrow/Parquet、DuckDB、SQLite、exchange-calendars、pytest、Ruff；沿用现有 Telegram transport 和手动 LLM 研究包。

**2026-11-04 授权更新：** 用户已明确要求“继续完成吧，CNN没有就没有吧。然后试着给我发一条今天的结果测试一下”。
因此本轮授权完成 Phase C、沿用 CNN 缺失降级并真实发送一次当日结果测试；
覆盖本文最初仅计划、不发送的范围限制。保留各阶段历史执行记录，不提交 Git。

---

## 一、需求约定与当前差距

- 将用户的“wix”暂按 **VIX** 理解。信号日和计划进场日使用 NYSE 交易日；不能把东京“明天”直接当成订单交易日。
- 本次用户要求开发计划；保持现有工作区改动，仅新增本文。后续实现前重新读取工作区，不能回滚或覆盖当前未提交的自动化工作。
- `operations/scheduling.py::notification_text` 当前只展示运行状态、数量、prompt/报告路径。
- `operations/session_jobs.py::session_notification` 的门禁通知只有 KEEP/DEFER/CANCEL 数量，缺少股票明细。
- `configs/sources/prices.yaml` 已配置 `^VIX` 和 11 个行业 ETF；配置存在不等于当前快照可用，仍须检查实际采集范围、历史长度及数据质量。
- `decision/hierarchy.py` 已有 market/sector/stock 三层诊断及 0–10 的 `reversal_score`，但该分数表达趋势修复状态。
- `strategy/buy_the_dip.py` 的 `CandidateSignal.score` 当前实际是 `relative_return60`，不能直接包装成 0–100 的综合买入分。
- `operations/pipeline.py` 默认只读 `ai_watchlist.yaml` 和 `hedge_overlay.yaml`，其他主题关注池没有全部进入自动日报。
- `decision/workbench.py` 已生成 `daily_index.md`，但按交易日/run_id 分散；详细研究包和 prompt 仍在多个文件中。
- 当前手工新闻模式会保留待审核状态；`execution_eligible` 本身也不能证明 T+30/T+60 等执行门禁已通过。

## 二、面向用户的最终效果

Telegram 日报保持三个固定区块，中文名称为主，股票代码用于识别。以下仅是排版示意，数字均为虚构，不是行情或推荐：

```text
美股日报｜信号日 YYYY-MM-DD → 计划进场日 YYYY-MM-DD

🌐 大盘
CNN 恐惧与贪婪：42/100
VIX：19.6（较前交易日 -0.8）
系统技术分：65/100｜市场状态：谨慎

🏭 板块技术分
科技 XLK 75｜金融 XLF 60｜医疗 XLV 55
……其余行业全部列出……
关注主题：半导体 SMH 80｜软件 IGV 70｜……

🎯 关注股入场筛选
当前门禁通过：暂无
技术条件符合、待新闻/开盘确认：
AAA 75/100｜待新闻审核
BBB 65/100｜待盘中确认
覆盖：关注 N 只；完成评估 M 只；待核验 K 只
```

产品要求：

1. 大盘展示三个独立数值，不把 CNN、VIX 和系统评分混为一个未经验证的综合分。来源时间较旧时在数值旁标“旧值/截至日期”；无有效记录显示“暂不可用”。
2. 板块显示全部配置板块，按分数降序、代码稳定打破同分；缺数据的板块也保留一行。主题分组避免把行业与主题混合排名造成重复统计。
3. 关注个股必须全量评估，不受研究 prompt 的 `max_research_symbols=20` 限制。Telegram 列出所有符合条件的候选，超长时分页，不悄悄只显示 Top N。
4. “当前门禁通过”“技术条件符合、待确认”“暂不适合/无法评估”分开。最后一类只在 Markdown 逐股说明，Telegram 留总数和必要阻断原因。
5. 没有候选必须明确显示“暂无符合条件的入场候选”。价格阻断必须明确显示“数据异常，暂停入场建议”，不能复用旧候选冒充新结果。
6. Telegram 不展示每日路径、prompt 正文、run_id、堆栈。保留影响决策的风险状态和数据缺失提示。
7. 门禁推送采用相同中文状态及股票行，说明本次通过/暂缓/取消了哪些股票；周报保持原业务内容，但同样移除重复路径提示。

### 固定 Markdown 入口

默认使用已有 `data_dir`，对用户提供一个固定目录：

```text
data/reports/daily/
  latest.md                         # 固定入口，指向最近有效的日报视图
  YYYY-MM-DD.md                     # 按 NYSE 信号日归档的阅读版
  YYYY-MM-DD/<run_id>/...           # 保留现有逐次运行 JSON、明细和研究包
```

目录只在运行手册中说明一次，不每天推送。阅读版包含：

- Telegram 同款摘要；数据截止时间、信号日、最早订单日、报告阶段。
- CNN/VIX 原值、前次值、来源、数据日期、抓取与可用时间、stale/quality 状态。
- 大盘/板块/主题技术分及逐项加减分；代理 ETF、样本覆盖、缺失原因。
- 所有关注个股的总分、各项贡献、原始策略分、模型状态、硬规则通过与否、未入选原因。
- 候选的限价参考、跳空限制、止损/目标价和仍待完成的门禁；计划价不描述为实际成交。
- 完整、可复制的 LLM 联网研究 prompt，以及对应结果 schema/导入方式。
- run_id、score_version、输入记录来源及内部明细相对链接，便于复核。

读取最新日报时必须同时显示当前运行状态。若最新尝试阻断，`latest.md` 顶部显示阻断状态与最近成功报告日期，不让上一次成功结果看起来是今天的建议。历史 replay 不覆盖 live 的 latest 入口。

## 三、评分与覆盖范围的首版方案

### 方案选择

| 方案 | 收益 | 代价/限制 |
| --- | --- | --- |
| 只改 Telegram 文案 | 改动最小 | 缺 CNN、缺全部关注池，不能满足本次要求 |
| 复用评分 + 补数据/覆盖 + 统一日报（采用） | 可解释、可测试，保留现有策略语义 | 分数明确限定为技术修复分 |
| 新建多因子综合买入评分 | 可以引入全新权重 | 属于额外策略研究，需要独立校准及 walk-forward 验证，不纳入本版 |

首版展示分命名为“技术分”，明确说明其实际含义是趋势修复分，不是收益率、胜率或独立买入许可。

```python
def display_technical_score(reversal_score: float | None) -> float | None:
    return None if reversal_score is None else round(reversal_score * 10, 1)

def market_technical_score(spy: float | None, qqq: float | None) -> float | None:
    if spy is None or qqq is None:
        return None
    return round((spy + qqq) / 2, 1)
```

- 个股与 ETF：同一 canonical `classify_reversal_context` 的 0–10 分映射为 0–100 分。不得在 renderer 重写指标或规则。
- 大盘：SPY 和 QQQ 技术分等权平均，Markdown 单列两者贡献；缺任一项显示缺失，不偷偷重新分配权重。现有 RED/YELLOW/GREEN 的市场门禁独立保留。
- 明细按趋势状态、5/20 日动量、相对强弱、均线、突破确认、RSI、回撤/连续下跌惩罚，输出已有规则的贡献分；保留 clamp 前后数值及截断差额，使总分可以重算。
- 原始 `CandidateSignal.score` 继续保留，用百分比说明 60 日相对收益；模型 rank 另列，模型未启用就显示未启用，不伪造模型分。
- 首版候选仍按 canonical 策略决定资格。展示时按技术分降序、原始策略分降序、symbol 排序，并明确这是展示顺序；不得因此改变仓位、策略许可或回测交易顺序。
- 新增 `score_version` 与评分配置指纹，禁止用当期横截面 min-max 拉伸导致分数随关注池变化漂移。

行业代理采用已有行情配置的 11 个 ETF：XLK、XLF、XLV、XLY、XLP、XLE、XLI、XLU、XLB、XLRE、XLC。保留现有 SMH/SOXX、IGV、DTCR 等主题代理；ETF 显示名称及具体映射在实现时核对发行方资料后固化。未覆盖的主题可使用该关注池有效股票的等权技术分，必须标注“关注池代理”，并披露有效数/总数；首版要求完整覆盖才发布主题总分，缺失时展示分项与原因，不能声称代表全市场板块。

关注池显式配置：AI 核心、AI satellite、互联网、太空卫星、加密计算、电力能源和防御池，按生命周期与信号日解析、去重，保留多主题标签。`raw_review_watchlist.yaml` 和 `futu_candidates_raw.yaml` 的未验证标识进入覆盖清单，状态为“待标识/上市/数据核验”，不得直接晋升为可交易候选。所有排除项都有原因，满足“我关注的所有个股”可追踪。

## 四、外部数据与时点约束

- VIX 优先复用现有 Yahoo `^VIX` adapter，报告来源如实写 Yahoo，不写成直接向 Cboe 获取；不使用 VIX ETF 价格替代指数。VIX 的指数定义可参阅 [Cboe 官方介绍](https://www.cboe.com/tradable-products/vix)。
- CNN 是新增接入。实施时先核验 [CNN 指标页面](https://www.cnn.com/markets/fear-and-greed) 与实际可访问的数据来源、字段、使用条件和更新时间。本次浏览工具访问页面受 robots 限制，尚未验证可用于生产的接口，不能承诺已有稳定 API。
- 只保存真实返回的数值；CNN 范围应为 0–100，缺失、越界、字段变更、时间无效均拒收。来源的情绪标签有则保留；不自行编造官方标签。
- 如自动来源不可用，可以提供带来源链接、原值、发布时间及本地导入时间的人工导入入口；不得用另一个恐惧指数冒充 CNN。
- 新外部事实必须带 source、source_version、fetched_at_utc、available_at_utc、ingestion_run_id、is_stale、quality_flags，以及 observation/source URL。取回历史值不能追溯设为过去已知；没有可信历史可用时间时按首次实际抓取/导入时间保守处理。
- 价格技术分与行情信号沿用现有价格 cutoff；CNN 展示可以采用报告生成时已知的观察值，但必须保存独立 context cutoff 和时间标签，不将其注入较早信号时刻的策略决策。历史 replay 只读对应 cutoff 前记录。
- CNN 与 VIX 在首版是展示上下文，缺失时只降级相应项；现有关键价格源缺失/stale 仍阻断建议。新增行业代理缺失不得伪装全局成功，也不应无意改变现有策略关键源范围。
- 在现有 budgets/backoff 框架内设置有限调用和超时，失败不提高并发；成功原始记录只追加，不修改删除。

## 五、执行任务与评审点

每项按“先写失败测试 → 单测确认失败 → 最小实现 → 测试通过 → 检查 diff”的顺序执行。以下测试函数名称为计划新增的验收契约，不表示已经存在。不要把一个阶段未完成的功能标记为已上线。

### Phase A：统一数据契约与完整覆盖

#### Task 1：日报契约、关注池及板块配置

**Create:** `src/quant_system/decision/digest.py`、`configs/reports/daily_digest.yaml`、`tests/unit/test_daily_digest.py`

**Modify:** `src/quant_system/universe/config.py`、`src/quant_system/operations/pipeline.py`、`src/quant_system/operations/scheduling.py`、`src/quant_system/operations/session_jobs.py`、`configs/operations/daily.yaml`

1. 建立 typed digest：metadata、market、sectors、coverage、stocks、entry_candidates、pending_candidates、data_health、research、artifacts；未知值使用 null 与明确 reason。
2. 新配置包含启用关注池、仅核验池、ETF/主题映射、score_version 和分页预算。共享 resolver 同时供自动运行、手动入口与报告使用，避免自动模式继续只有两个池。
3. 每个关注标识必须恰好有一个最终覆盖状态；去重保留来源和主题。研究包数量限制不能影响扫描范围。
4. 新加市场/行业代理作为 context-only 输入，不能自动成为个股推荐或关键价格源。
5. 测试覆盖默认所有关注池、重复标的、未来生效/退市记录、raw review 不晋升、超过 20 只仍全量扫描、配置变更使 pipeline 缓存失效。

**Test:** `.venv/bin/python -m pytest tests/unit/test_daily_digest.py tests/unit/test_universe_config.py tests/integration/test_daily_pipeline.py -q`

**Expected:** 覆盖集合与配置一致；现有 pipeline 的本地端到端测试继续通过。

#### Task 2：CNN 接入与 VIX 读数

**Create:** `src/quant_system/domain/market_context.py`、`src/quant_system/ingestion/market_context.py`、`configs/sources/market_context.yaml`、`tests/unit/test_market_context.py`、`tests/fixtures/market_context/`、`tests/integration/test_market_context_storage.py`

**Modify:** `src/quant_system/storage/schemas.py`、`src/quant_system/storage/parquet.py`、`src/quant_system/storage/duckdb.py`、`src/quant_system/operations/pipeline.py`

1. 先完成 CNN 来源可用性检查并记录选定来源/adapter 版本；若失败，完成明确 unavailable 路径和人工导入契约，记录尚未满足自动接入验收，不能伪称完整完成。
2. 定义有完整 provenance 的 market observation schema，CNN 和 VIX 使用明确不同 series_id；VIX 若派生自已有价格记录须保留输入记录引用和版本。
3. 实现有限请求、范围/时间校验、追加存储、DuckDB cutoff 查询及失效规则。指标重采形成新版本，读取同一 observation 截止时最新可见版本。
4. pipeline 增加独立市场上下文阶段，将原始数据签名、source/score/config 版本纳入缓存；live 与 replay 分离。
5. fixtures 验证有效值、403/429/超时、未知字段、缺值、越界、stale、未来记录、首次抓取历史值不可回填过去决策、来源变更。

**Test:** `.venv/bin/python -m pytest tests/unit/test_market_context.py tests/integration/test_market_context_storage.py tests/integration/test_daily_pipeline.py -q`

**Expected:** 本地 fixture → Parquet → DuckDB → 现有特征/scan → JSON 路径通过；CNN 不可用时明确降级且无重试失控。

**Review checkpoint A:** 展示样例 digest JSON、完整关注池覆盖清单、CNN 接入实测结论与来源版本。停止，等待该阶段评审后进入 Phase B。

### Phase B：可解释分数与每日 Markdown

#### Task 3：评分明细与进场状态

**Modify:** `src/quant_system/decision/hierarchy.py`、`src/quant_system/decision/digest.py`、`src/quant_system/decision/premarket.py`、`src/quant_system/decision/gates.py`

**Test:** `tests/unit/test_hierarchy_diagnostics.py`、`tests/unit/test_daily_digest.py`、`tests/unit/test_phase6_gates.py`、`tests/unit/test_buy_the_dip.py`

1. 在 canonical 修复评分中记录每个条件的输入、阈值和贡献，不复制另一套评分公式；旧返回字段兼容。
2. 冻结历史 fixture 的原始分数和阶段，确认新增解释字段不改变结果；测试加减分、clamp、None、NaN/无效数据等边界。
3. 构建上文定义的 0–100 展示分、大盘平均和主题覆盖；保留原始策略分及模型状态。
4. 进场状态必须结合 canonical 候选、价格质量、新闻审核、实际时间、同一 signal/order session 的有效门禁。不可将 premarket 的 `execution_eligible=True` 直接翻译为“现在可进场”。
5. 手动新闻未审核、快照缺失、门禁过期、未来门禁、非当前订单日均留在待确认/不可执行；已知 HIGH 风险按现有规则否决。LLM 输出不能绕过门禁。
6. 验证扩展关注池中高分但硬规则不通过的标的不会进入候选；没有技术分的标的显示缺失原因。

**Test:** `.venv/bin/python -m pytest tests/unit/test_hierarchy_diagnostics.py tests/unit/test_daily_digest.py tests/unit/test_phase6_gates.py tests/unit/test_buy_the_dip.py tests/integration/test_backtest_engine.py -q`

**Expected:** 旧 canonical 信号/排序与回测结果无意外变化，展示评分可逐项重算，门禁状态不会被高分覆盖。

#### Task 4：统一完整 Markdown 与稳定入口

**Create:** `src/quant_system/decision/digest_reports.py`、`tests/unit/test_daily_digest_reports.py`

**Modify:** `src/quant_system/decision/workbench.py`、`src/quant_system/decision/manual_research.py`、`src/quant_system/decision/research.py`、`src/quant_system/operations/pipeline.py`

1. 先生成完整 digest JSON，再从同一对象生成 Markdown，避免两个输出分别计算而不一致。
2. 按第二节内容生成全量日报；维度贡献为真实规则贡献，未知新闻/基本面不得填成虚构分数。
3. 复用已有手工研究 prompt 和审核回填契约，将可复制全文嵌入 Markdown；包含 as-of cutoff、目标、原始分数/缺口、检索问题、来源要求、未知处理、结构化输出与不可覆盖硬规则的约束。
4. 对所有待研究进场候选提供 prompt。超过单包预算时显式分批并嵌入各批内容；广义研究 watchlist 可以保留原有数量限制，但不能漏掉进场候选。
5. 写入不可混淆的 run 级报告后，以临时文件 + 原子替换发布日期文件及 latest；发布前校验 run/session/cutoff 一致。
6. 同日盘前刷新/研究回填/门禁更新保留旧 run 证据并更新阅读版；旧任务迟到、历史 replay、未完成或损坏报告不能覆盖较新的 live 视图。阻断运行发布状态封面，明确旧报告日期。
7. 路径/表格字符安全转义；Markdown 含完整 prompt，内部相对链接可以解析；不包含密钥。

**Test:** `.venv/bin/python -m pytest tests/unit/test_daily_digest_reports.py tests/unit/test_daily_workbench.py tests/integration/test_manual_research.py tests/integration/test_daily_pipeline.py -q`

**Expected:** 单个日期 Markdown 可读全量结果和 prompt，固定入口稳定，失败不会展示旧建议为新建议。

**Review checkpoint B:** 展示正常、暂无候选、新闻待审核、缺失 CNN、关键价格阻断五种 Markdown 和 Telegram 本地预览。停止评审，不实际发送。

### Phase C：Telegram 集成与上线验收

#### Task 5：简明通知、完整分页与门禁更新

**Create:** `src/quant_system/operations/digest_notifications.py`、`tests/unit/test_digest_notifications.py`

**Modify:** `src/quant_system/operations/scheduling.py`、`src/quant_system/operations/session_jobs.py`

**Test:** `tests/unit/test_scheduled_telegram.py`、`tests/unit/test_session_jobs.py`

1. 纯 renderer 接收 digest，输出三块中文日报；正常消息不重复路径/prompt/内部任务信息，失败仍说明暂停建议及简短原因。
2. 市场与全部板块位于首页，候选按完整股票行分页，页头带同一 signal session、run 标识的短显示和页码；同日刷新标注“盘前刷新”等阶段。
3. 复用现有 TelegramClient 的 UTF-16 上限校验，按 UTF-16 单位计算、按行拆分，禁止当前按字节硬截断造成名单丢失；页头也计入长度。
4. SQLite outbox 每页使用稳定 `job/run + part_index` key，记录总页数和各页状态；已成功页不重发。超时 UNKNOWN 不自动重发，未确认的页不得标记已送达，保留可恢复状态及发送次序。
5. 沿用有界 429 backoff、消息次数预算及不泄露 token 的 transport；新增分页不得触发无限重试。
6. 门禁通知列具体股票及当前状态/技术分；和同日 Markdown 使用同一有效快照。周报不改成买入推荐。

**Test:** `.venv/bin/python -m pytest tests/unit/test_digest_notifications.py tests/unit/test_scheduled_telegram.py tests/unit/test_session_jobs.py -q`

**Expected:** 中文/emoji、长名单、空名单、部分发送成功后重启、429、UNKNOWN 均正确；没有名单静默截断，也无每日目录提示。

#### Task 6：端到端验收与运行文档

**Modify:** `tests/integration/test_scheduled_daily.py`、`tests/integration/test_session_automation.py`、`tests/integration/test_daily_pipeline.py`、`docs/daily-runbook.zh.md`、`docs/data-contracts.md`

1. 更新旧测试对“本机 prompt”的断言，改为验证市场三项、板块、候选、缺失状态和不包含目录。
2. 固定本地行情/CNN fixture 跑完整链路：Parquet → DuckDB → canonical features/scan → digest JSON → Markdown → fake Telegram transport；不依赖真实网络或 token。
3. 验证 Telegram 每个数值和状态都对应同一 run 的 JSON/Markdown；所有关注股状态可核对，全部符合条件候选不丢失。
4. 覆盖重复 tick、报告缓存、进程恢复、提前收盘、DST、非交易日、同日盘前刷新与 T+30/T+60。没有可靠盘中数据仍不得认定成交。
5. 运行完整测试及 lint；只在失败或修改后重跑相关检查。文档记录固定入口、分数含义、覆盖池、指标来源与失败行为。

```bash
.venv/bin/python -m pytest tests/integration/test_scheduled_daily.py tests/integration/test_session_automation.py tests/integration/test_daily_pipeline.py -q
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests
git diff --check
```

**Expected:** 定向集成与全套测试、Ruff、diff 检查通过；runtime 报告、数据与凭据不进入提交。

**Review checkpoint C:** 提供最终样例、测试结果、来源实测状态、下一次按交易所日历推算的运行窗口及回退方式。当前请求仅授权制定计划；真实发送和将新配置投入自动推送属于后续实施范围，不在本轮执行。

## 六、完成标准与排期边界

- 三个大盘读数、全部板块和全量关注池评估均进入同一 digest；有效 CNN 来源尚未验通时明确标记该接入未完成。
- Telegram 的股票状态可追溯到 canonical 规则与当前门禁；零候选、数据故障不混淆。
- 全部候选分数和分项明细可重算，两个输出一致；LLM prompt 可直接复制，长候选集分包不漏人。
- 固定目录与 latest 入口完成，不再每日重复提示路径；run 级历史证据保留。
- 无实时新闻或盘中数据时忠实保留待确认状态；不为了让通知“有股票”而放松规则。
- 三个阶段分别保持本地端到端 smoke 路径，按评审点推进。明天是否可以启用取决于后续实现、CNN 来源验证和上述验收；本计划不构成已经完成部署的承诺。

## 执行记录：2026-11-04 / Phase A 评审点

用户已授权开始开发。本次实施 Task 1–2 的本地功能与测试，按评审点 A 停止；
没有进入 Phase B/C，没有发送真实 Telegram 消息，没有创建 Git 提交。

- 默认自动日报和手动 daily-workbench 共用七个生产关注池；显式自定义池保持隔离。
  使用本地生命周期记录核对 2026-11-04 配置：共 249 个去重关注标识，239 个进入扫描范围，
  9 个待核验、1 个 inactive。每行保留配置来源、主题和解析后的标识。
- 新增版本化 digest JSON；Phase A 明确标为 FOUNDATION_NOT_ENTRY_CLASSIFICATION，
  分数与入场分类尚待 Phase B，不能把空 entry_candidates 解读为最终筛选结论。
- ETF/VIX 辅助价格分批采集，与策略批次分开统计失败比例，共享批次请求限额。
  辅助来源失败不会绕过或意外替换原有关键价格阻断。
- CNN raw schema、Parquet 追加写入、DuckDB 截止时点修订查询、有限调用/持久化退避、
  人工导入 CLI 已完成。VIX 读取已有原始价格并保留源记录引用；context cutoff 独立保存。
- CNN 生产接入验收仍未通过：2026-11-04 直接请求候选官方数据域名返回 HTTP 418。
  没有绕过访问限制；默认 provider 保持 none。adapter 版本
  `cnn-graphdata-v1-unverified` 的 fixtures 是明确标注的合成样例，不能冒充真实响应。
  人工导入版本为 `cnn-manual-import-v1`，VIX 派生版本为 `vix-context-v1`。
- 全套回归：383 passed（40.98s）。随后对共享请求 guard 与质量阻断处理补充验证，
  新增及相关定向回归：46 passed（16.54s）；新增三项测试包含在这 46 项内。
- 本地 smoke 已通过 fixture → Parquet → DuckDB → canonical features/scan → JSON。
  25 只个股的额外测试证明全量扫描不会被研究列表 20 只上限截断。
- 已更新数据契约及运行手册。`latest.md`、日期阅读版和新版 Telegram 尚未实施，
  现有 run 级报告继续保存。

评审产物（运行数据，未提交）：

- `data/reports/digest-phase-a/23e7d782-b3fc-4f20-bff2-3f597634ba1e/watchlist_coverage.json`
- `data/reports/digest-phase-a/23e7d782-b3fc-4f20-bff2-3f597634ba1e/sample_digest.json`
- `data/reports/digest-phase-a/23e7d782-b3fc-4f20-bff2-3f597634ba1e/review_summary.json`

sample_digest 来自隔离合成行情/CNN fixture，不是当前行情或入场建议。CNN 来源限制
是尚未完成的验收项；本地降级路径可以继续支持后续评分/Markdown 开发。

## 执行记录：2026-11-04 / Phase B 评审点

用户通过“继续开发”授权继续。本次完成 Task 3–4，按评审点 B 停止，未进入
Phase C；没有更换实际 Telegram 通知、没有发送测试消息、没有创建 Git 提交。

- canonical 趋势修复评分新增逐项输入、条件、贡献、截断前总分与截断调整，冻结
  fixture 确认原有分数与阶段保持兼容。个股和 ETF 映射为 0–100 技术分；SPY/QQQ
  等权平均，缺项不重分权；主题关注池完整覆盖才显示总分。保留原始相对收益和模型状态。
- typed digest 升级为 schema 2；全量股票保留价格质量、canonical 规则检查、新闻与
  模型状态、未入选原因和计划。T+30/T+60 必须匹配同一 canonical run / signal /
  order session，重新读取并评估快照，拒绝未来、过期、丢失或不匹配的证据。
- 完整 Markdown 包含市场/ETF/股票贡献明细、来源时点、计划价格和研究 prompt。
  Prompt v2 每批最多 20 只，45 只 fixture 验证 20/20/5 全覆盖；导入按股票合并批次，
  保留风险证据，兼容旧 v1 包并如实保留来源版本；LLM 研究不能放行门禁。
- 固定目录 `data/reports/daily/` 新增 `latest.md` 和 `YYYY-MM-DD.md`。
  run 级 digest JSON/Markdown 不覆盖；同日研究回填与门禁更新生成新版本。
  live 发布先保存完整证据，再逐文件原子替换阅读入口；manifest 保留 checksum 和
  最近成功报告链接。历史 replay、旧计划迟到更新不会覆盖较新的 live 视图。
  阻断尝试发布暂停封面，同一时间戳冲突时阻断优先；其他完全同序冲突保留先发布者。
- 本地端到端验证覆盖 fixture → append-only Parquet → DuckDB → canonical 特征/scan
  → digest JSON → Markdown → 本地文本预览，以及有效门禁更新、过期撤回、迟到任务、
  多批研究回填、历史隔离、写入失败、相对链接和转义。
- 全套回归：**412 passed（46.59s）**；随后统一价格 provenance 的 UTC 表示并修正
  缺值/主题代理显示，相关定向回归：**8 passed（1.71s）**。Ruff 与 diff 检查通过。
- 五种预览均为隔离合成数据：正常（1 个合成门禁通过）、暂无候选、新闻待审核、
  CNN 缺失、关键价格阻断。只包含 1 只 fixture 个股及 15 个 ETF / 关注主题以便查看，
  不冒充真实全市场行情或当前投资建议；生产关注池配置沿用 Phase A 的完整范围。
- CNN 自动源仍未通过生产验收，默认 provider 仍为 none。本阶段未增加来源访问或
  绕过限制；没有人工导入的真实有效值时继续明确降级。

评审入口（运行数据，已被 Git 忽略）：

`data/reports/digest-phase-b/bcda4fd1-bfa5-4a5f-90cf-342cf7e2fd9c/README.md`

该入口链接五种完整 Markdown 和五种 Telegram 本地预览；同目录的
`review_summary.json` 保存各场景的状态、候选数量和文件路径。
待评审后的 Phase C 才接入正式 Telegram 三块布局、UTF-16 完整分页、outbox 恢复
及最终上线验收。静态报告仅描述评估时点，并列出门禁有效期限，不持续宣称可执行。

## 执行记录：2026-11-04 / Phase C 完成与真实测试

用户已授权完成开发并发送今日测试，明确接受 CNN 缺失。Task 5–6 已完成；
没有创建 Git 提交，保留工作区其他既有改动。

- 新增统一 Telegram renderer：大盘、板块/主题、入场候选三块布局，日期、短 run ID
  与完整页码；已接入收盘、盘前和门禁任务。中文主题名称与 Markdown 共用；正常
  通知不再包含本机目录或研究 prompt。旧 foundation / 不匹配信号日不会发布为当前建议。
- 按 UTF-16 长度、完整行分页，默认 3800 单位；180 只候选测试无遗漏，所有行业代理
  位于标准首页。门禁消息的具体股票、技术分与检查状态来自相同 digest 修订。
- outbox 采用兼容迁移与整组事务入队，逐页记录总数、次序、回执及期限；每次至多
  5 次发送尝试。修正旧投递倒序；SENT 不重发，UNKNOWN 与中断的 SENDING 不自动重发，
  同组后续页等待。目的地级 429 冷却持久化，重试耗尽/页过期也不绕过 Retry-After。
  已过期门禁通知不发送；旧单条消息记录不会在升级后复制发送。
- 实跑发现 VIX 中的非 NYSE 交易日导致整个辅助价格批次抛出 NotSessionError。
  已用固定 Good Friday 多标的 fixture 复现并修复，Yahoo source_version 升为
  `yfinance-<installed-version>/adapter-4`。仅过滤应用交易日历外的返回行并记录警告，
  成功原始记录继续 append-only，未提高调用额度或并发。
- 本地端到端验证包含合成 CNN / VIX → Parquet → DuckDB → canonical scan → digest /
  Markdown → fake Telegram，以及日历/DST/提前收盘、分页恢复、429、UNKNOWN 和迁移。
  **完整测试 425 passed（52.36s）**；Ruff 与 `git diff --check` 通过。
- 真实行情：信号日 **2026-11-03**，计划进场日 **2026-11-04**。覆盖 249 个标识，
  239 个有效关注标的完成评估、9 个待核验、1 个 inactive；技术分缺失股票/板块均为 0。
  VIX **20.83**，系统技术分 **0/100**，当前入场候选与待确认候选均为 0。
  CNN 显示暂不可用，按用户要求不作为剩余开发阻断项。
- 已在 **2026-11-04 03:43:04 UTC / 12:43:04 Asia/Tokyo** 发送 **1 条**真实测试消息；
  Telegram 状态 **SENT**，message_id **57**，attempts **1**，未发送其他 outbox 组。
  报告 run_id：`af81e377-6391-4d9d-a1c8-f909b5a7b2f5`。
- 已核对注册的 LaunchAgent 指向当前项目 Python、工作目录与配置；下次调度直接读取
  新版代码。下一次盘前刷新 **11 月 4 日 13:30 UTC / 22:30 东京**；下一次收盘日报
  **11 月 4 日 22:00 UTC / 11 月 5 日 07:00 东京**。实际时刻继续由 NYSE 日历推算。
- 暂停/恢复方式已写入运行手册：`automation uninstall` / `automation install`；
  原始事实与历史报告不删除。CNN provider 保持 none，人工导入仍可使用。

本机运行产物（Git 忽略）：

- 完整报告：`data/reports/daily/2026-11-03/af81e377-6391-4d9d-a1c8-f909b5a7b2f5/digest.md`
- 固定阅读入口：`data/reports/daily/latest.md`
- 测试文本与回执：`data/reports/telegram-test/94ba35a2-18d7-4d64-86ce-944d9cb2c06b/`
  下的 `telegram_preview.json`、`telegram_receipt.json` 与 `pipeline_result.json`。

Phase A–C 的当前用户范围已完成；自动 CNN 来源如以后需要，再单独验证。
