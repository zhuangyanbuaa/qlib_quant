# 每日运行手册

这份手册是本地量化决策辅助系统的每日手动流程。系统只生成报告、候选列表、风险上下文、新闻研究 prompt 和持仓检查结果；不会连接券商，也不会自动下单。

所有 `--date` 都使用 NYSE 交易日，格式为 `YYYY-MM-DD`。如果不传 `--date`，大多数日常命令会自动使用交易所日历里的最近一个已完成美股交易日。

如果当前 shell 里没有 `quant` 命令，可以用：

```bash
uv run quant ...
```

或者直接使用一键脚本：

```bash
python scripts/run_daily_workbench.py
```

## 每日推荐顺序

### 使用 Codex / ChatGPT 人工研究新闻

可以让程序完成量化评分，再把生成的 prompt 交给具备实时联网能力的会话运行。
人工模式不构造新闻/SEC provider，也不需要新闻 key；价格更新仍使用现有来源。
本地已知 HIGH 新闻继续否决，其他新闻覆盖保留待审核状态，原始分数和 signal ID
不变。研究结果不会自动放行候选或形成 paper 入场。

**第一步：运行价格更新、评分和研究包生成。**

```bash
python scripts/run_daily_pipeline.py --manual-news
```

仅使用已有数据生成报告时：

```bash
python scripts/run_daily_workbench.py --manual-news
```

历史流程验证使用 `run_daily_pipeline.py --manual-news --replay --date <交易日>`，
不联网更新价格。live 模式仍须在最近交易日收盘数据可用后、下次开盘前运行。
关键价格失败返回 BLOCKED；人工模式不是 `--no-news-risk`。

**第二步：运行本次生成的 prompt。**

输出里的 `manual_research.artifacts` 和 `daily_index.md` 会指向：

```text
data/reports/daily/<date>/<run_id>/manual_research_prompt.md
data/reports/daily/<date>/<run_id>/manual_research_package.json
data/reports/daily/<date>/<run_id>/manual_research_result.schema.json
data/reports/daily/<date>/<run_id>/manual_research_result.template.json
```

将完整 `manual_research_prompt.md` 复制到 Codex/ChatGPT，并明确要求实际联网检索。
prompt 已内嵌量化数据、schema 和模板，ChatGPT 不需要访问本地路径。它包含完整
选中股票评分、规则、模型排名、价格/订单时间、市场板块、杠杆观察和全部持仓。
研究列表按原配置限额，杠杆关注最多 20 条；包里的 `selection` 明确展示选中数及
总数，不代表研究过整个股票池。原始量化分数与模型研究优先级分开显示。

会话应输出中文研究报告和独立 JSON；将 JSON 保存成
`manual_research_result.json`。实际 UTC 检索/生成时间、来源 URL、发布日期必须
填写准确。发布日无法核实可填 null，旧资料标记 `background_only: true`；无法
确认模型版本填 `UNKNOWN_NOT_RECORDED`。未联网或未研究的股票明确写
`NOT_SEARCHED` / `UNAVAILABLE`，不要凭记忆填近期事件。

**第三步：核对来源后，验证并导入结构化结果。**

```bash
.venv/bin/python -m quant_system decision import-manual-research \
  --package <本次manual_research_package.json> \
  --result <manual_research_result.json> \
  --reviewed-by <你的审核标识> --dry-run

.venv/bin/python -m quant_system decision import-manual-research \
  --package <本次manual_research_package.json> \
  --result <manual_research_result.json> \
  --reviewed-by <你的审核标识>
```

审核标识记录谁核对了来源和时间，不代表已认证完整新闻覆盖。未运行的模板不能
导入；schema、run ID、包 hash、股票、引用、时间或原始量化文件不匹配时返回
`REJECTED`，退出码 2。价格门禁失败同样返回 BLOCKED。dry-run 不追加事实或
生成综合报告，允许刷新可重建 DuckDB 与创建进程锁。

结果先以实际导入时刻追加到 `raw/manual_research/`，再生成新的
`combined_report.json` / `combined_report.md`，命令输出路径。原始量化报告保持
原样。相同 payload 重复导入不再追加；修订写新事实。同一包里旧 HIGH/MEDIUM
证据保留，后续无证据修订不能悄悄清除。

**第四步：阅读综合报告。**

报告把程序评分、人工新闻证据、研究优先级、风险、缺口和原始报告链接合并展示。
搜索过但没有发现坏消息仍为 UNKNOWN；综合报告的可执行候选数始终为 0。
`FOCUS/WATCH/SKIP` 是研究优先级；HIGH 风险优先 SKIP。

研究生成时间超过包生成后 24 小时应重新生成研究包。导入延迟或新闻 cutoff 超过
一小时会标记 STALE_RESEARCH；订单窗口是否过期单独显示，不能冒充盘前决定。
历史再生仅使用 requested cutoff 之前已导入的事实，而且 cutoff 不得早于研究包
生成时刻。需要回放时：

```bash
.venv/bin/python -m quant_system decision combine-manual-research \
  --package <manual_research_package.json> --cutoff <UTC时间>
```

### 一键更新并生成日报

完成初次历史补数后，建议使用统一入口，在最近一个交易日收盘数据可用后、下一
交易日开盘前运行。交易日和开盘时间由 NYSE 日历决定：

```bash
.venv/bin/python -m quant_system data check-sources --symbols AAPL,MSFT,NVDA
python scripts/run_daily_pipeline.py
```

流程依次执行来源检查、默认 AI/hedge 股票池价格更新、新闻和 SEC 更新、关键价格
校验、完整日报。指定其他股票池可重复传入 `--universe <yaml>`。整个本地数据集的
维护仍使用下文的 `update-prices --all-stored`。新入口默认不附加模型排名。

结果包含 `report_path` 和 `pipeline_report_path`，后者记录每个阶段的状态、尝试
次数和采集报告路径。SQLite 保存进度，进程锁防止与其他采集或事实导入同时运行。
同一交易日、配置与 UTC 半小时窗口内，完整且数据未变化的日报会返回
`reused: true`；原始数据变化或组件文件丢失会重建日报，保留成功的采集阶段。
中断后重跑会恢复流程；失败的采集使用新的 attempt run ID，仍受每日预算约束。

需要明确再次采集时使用 `--retry`，例如配置凭据后或需要新的新闻决策。可选
`--refresh-window` 必须是当前 UTC 半小时边界，通常不需要设置。价格门禁失败
返回 `BLOCKED`，不会生成日报；`BLOCKED` / `FAILED` 退出码为 2。新闻缺失或
覆盖不足保留 `DEGRADED`，只供研究，不形成可执行入场草案。

历史重放不联网采集，并使用当时的价格和新闻可用时间：

```bash
python scripts/run_daily_pipeline.py --replay --date 2026-10-09
```

原来的 `scripts/run_daily_workbench.py` 继续用于仅生成报告。

### 新闻来源和公司映射准备

在本地 `.env` 配置 `QUANT_ALPHA_VANTAGE_API_KEY` 和
`QUANT_SEC_USER_AGENT`。SEC User-Agent 应包含应用标识及真实联系邮箱；SEC 不需要
API key。密钥和联系方式不会出现在来源检查输出中。

Alpha Vantage key 可以免费申请，但官方文档当前将 NEWS_SENTIMENT 标为 Premium；
有 key 不代表具有新闻权限。`check-sources` 的 `READY_FOR_PROBE` 只说明凭据已准备，
不代表服务已验证。来源说明见 [Alpha Vantage 文档](https://www.alphavantage.co/documentation/#news-sentiment)
和 [SEC API 文档](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)。

```bash
.venv/bin/python -m quant_system data verify-news --symbols AAPL,MSFT,NVDA
```

验证最多三个代码，按配置预算调用，输出各代码 news/SEC 的覆盖状态。无凭据或无权限
记录为 `UNAVAILABLE`，不能当成查询成功且无风险。新闻接口每个代码单独请求，避免
多代码交集被误当成全股票池覆盖；主题过滤或响应达到上限会标记 `PARTIAL`。

来源检查同时列出 `unmapped_symbols` 和 `missing_cik_symbols`。新增公司身份需人工
审核，JSON 清单使用 `mappings` 数组；每条包含 `mapping_id`、`symbol`、数字 CIK、
`aliases`、来源 URL 和完整来源/版本/时间/run ID/质量字段。格式见数据契约。

```bash
.venv/bin/python -m quant_system data import-company-mappings --manifest <reviewed.json> --dry-run
.venv/bin/python -m quant_system data import-company-mappings --manifest <reviewed.json>
```

清单整体校验后追加写入，重复导入相同记录跳过，修订按可用时间生效。更名可继承
同一公司身份，股份转换不自动继承；明确失效的身份修订不会回退到旧配置。

### 价格信号和盘前新闻使用哪个时间？

价格特征始终固定在信号交易日收盘后 30 分钟的 `price_cutoff_utc`。历史重放默认
新闻也使用这个 cutoff；live pipeline 则使用生成时的 `news_cutoff_utc`，必须在下次
开盘前。仅生成报告时可显式传 `--news-cutoff <UTC timestamp>`。晚于新闻 cutoff
的事实不可见，未来时间或已经进入下次开盘的刷新会被拒绝。

更新新闻保留原有 `signal_id` 与价格信号时间，生成独立 `decision_id`。开盘门禁和
paper 必须匹配该新闻决策，不能复用旧决定；paper 仍按原始信号防止重复成交。

### 1. 更新价格数据

建议在最近一个美股交易日收盘后、Yahoo 日线数据基本可用之后运行：

```bash
uv run quant data update-prices --all-stored
```

它会做这些事：

- 更新 DuckDB 里已经存在的所有 symbol；
- 使用配置里的 rate limit、retry/backoff 和 daily call budget；
- 追加新的 raw Parquet 记录，不修改过去已经成功写入的 raw 数据；
- 刷新 DuckDB 分析视图；
- 在 `data/reports/quality/` 下写入数据质量报告。

如果只想更新某一个 universe，可以用：

```bash
uv run quant data update-prices --universe configs/universe/ai_watchlist.yaml
uv run quant data update-prices --universe configs/universe/ai_satellite_watchlist.yaml
uv run quant data update-prices --universe configs/universe/hedge_overlay.yaml
```

日常使用建议优先用 `--all-stored`。它适合在初次历史补数完成之后维护整个本地数据集。

### 2. 可选：更新本地新闻

dashboard 和一键 daily runner 不会自动联网抓新闻。它们只读取本地已经存好的 news/SEC Parquet。

如果希望当天报告启用 news-risk 层，需要先抓取一个有限的 symbol 集合：

```bash
uv run quant data update-news \
  --symbols NVDA,AMD,AVGO,ASML,TSM,MU,ARM,MRVL,ANET,VRT \
  --start 2026-08-03T00:00:00Z
```

它会做这些事：

- 在 provider 已配置时抓取新闻和 SEC/company events；
- 使用 `configs/sources/news.yaml` 里的速率限制和每日调用预算；
- 写入 point-in-time raw news/event Parquet；
- 用 deterministic rules 或可选 FinBERT 做事件分类；
- 如果 FinBERT 超时或模型不可用，会标记为 `DEGRADED`，不会静默把坏结果当成可信结果。

日常不要一次抓太多 symbol。优先抓：

- 当前持仓；
- daily workbench 里最靠前的候选；
- 你当晚真的可能研究的股票；
- 2x overlay 或 `generic_2x_watch` 提醒里你感兴趣的名字。

如果当天不抓新闻，就用 `--no-news-risk` 跑 daily workbench。

### 3. 生成每日 workbench

没有更新新闻时的保守默认命令：

```bash
python scripts/run_daily_workbench.py --no-news-risk
```

如果已经执行了第 2 步，并希望启用本地 news-risk：

```bash
python scripts/run_daily_workbench.py --news-risk
```

如果要指定某个 NYSE 交易日：

```bash
python scripts/run_daily_workbench.py --date 2026-08-07 --no-news-risk
```

它会做这些事：

- 生成盘前 Buy-the-Dip 计划；
- 加入大盘、板块、个股三层 rotation context；
- 在可用时加入只读 LightGBM rank context；
- 生成更宽的人工研究列表；
- 为候选股票生成 Codex 新闻研究 prompt；
- 扫描独立的 2x leveraged overlay；
- 如果有人工持仓记录，则检查持仓；
- 写入 `daily_index.json` 和 `daily_index.md`。

最应该先打开的是：

```text
data/reports/daily/<date>/<run_id>/daily_index.md
data/reports/daily/<date>/<run_id>/daily_index.json
```

`daily_index.md` 是当天所有报告的目录。每天先看它，别在一堆 JSON/CSV 里迷路——这套系统已经够像小机场塔台了，不需要再加迷宫。

### 4. 查看新闻研究 prompt

打开：

```text
data/reports/daily/<date>/<run_id>/news_research_prompt.md
```

把它复制到一个可以联网浏览的 Codex 会话里，让 Codex 总结这些股票最近几天的新闻。

这个 prompt 不是买卖建议，而是让 Codex 做带来源链接和日期的新闻整理，并给出人工研究分类，例如：

- `重点研究`
- `继续观察`
- `暂时跳过`

这一步适合用来研究：

- research list 里的股票；
- satellite watchlist 里的股票；
- 2x overlay / `generic_2x_watch` 提醒里的股票；
- 你已经持仓、但需要判断是否卖出或减仓的股票。

### 5. 可选：打开本地 dashboard

```bash
streamlit run apps/decision_dashboard.py
```

dashboard 会展示：

- `Today`：当天 `daily_index` 摘要和下一步；
- `Candidates`：盘前候选和 calibration candidates；
- `Research`：更宽的人工研究列表；
- `News`：本地 Parquet 里已经存在的 point-in-time 新闻和 SEC/company events；
- `2x Overlay`：具体 2x 产品和 generic risk-on leverage watch；
- `Portfolio`：人工持仓检查；
- `Reports`：当天生成的 Markdown 报告；
- `Data Health`：报告元数据和数据质量上下文。

停止 dashboard：

```text
在运行 Streamlit 的 terminal 里按 Ctrl-C
```

### 6. 可选：开盘前和开盘后门禁

开盘前刷新：

```bash
uv run quant decision preopen-refresh
```

T+30 和 T+60 检查：

```bash
uv run quant decision open-gate --minutes 30
uv run quant decision open-gate --minutes 60
```

如果没有券商 snapshot 或可靠的 intraday point-in-time 数据，这些门禁会保守返回 `DEFER`。这是故意的：系统不能假装日线 OHLC 数据里包含真实盘中时点信息。

broker snapshot CSV/JSON 现在必须包含完整来源和时间信息。旧的只有价格的文件会
返回 `DEFER`；不要靠补写历史时间让它通过。JSON 每行格式例如：

```json
{
  "symbol": "NVDA",
  "last_price": 101.0,
  "session_date_ny": "2026-10-12",
  "quote_at_utc": "2026-10-12T13:59:50Z",
  "available_at_utc": "2026-10-12T13:59:55Z",
  "fetched_at_utc": "2026-10-12T14:00:00Z",
  "source": "your_broker",
  "source_version": "your_adapter_version",
  "ingestion_run_id": "11111111-1111-4111-8111-111111111111",
  "is_stale": false,
  "quality_flags": [],
  "news_risk": "LOW",
  "liquidity_status": "OK"
}
```

将真实记录放在 `{"rows": [...]}` 或 JSON 数组中；CSV 的 `quality_flags` 用 JSON
数组字符串。报价最多允许早于门禁 cutoff 5 分钟；报价、抓取和可用时间均不能晚于
cutoff。命令应在门禁目标时间后的 5 分钟内运行，提前、错过窗口、错误交易日或
已收盘都会 `DEFER`。时刻来自实际交易所开盘和收盘，包含夏令时及提前收盘。
示例仅说明格式，不是有效实时行情或新闻确认。

### 新闻覆盖为什么是 UNKNOWN？

没有本地新闻，不等于已确认低风险。系统分别记录新闻与 SEC 的查询覆盖：未查询、
未配置、失败、部分覆盖和过期都不能显示为 `LOW`。只有两类来源都有 cutoff 前
可见的成功查询，覆盖完整 lookback 起点且查询终点距 cutoff 不超过 1 小时，才能
确认有效覆盖；成功空查询会明确记录为 `EMPTY`。

候选仍可出现在研究列表中，但会标为 `WATCH_ONLY_NEWS_COVERAGE`，不能通过开盘
门禁或形成 paper 入场。已知高风险事件仍然否决。显式 `--no-news-risk` 会显示
`DISABLED`。查看日报的 `data_health.news`、`coverage_status` 与运行状态；组件
完成并不意味着新闻已齐全。实时查询晚于某个历史 cutoff，也不能倒填那天的覆盖。

行情、新闻和 SEC 的本地每日调用预算现在跨命令、跨进程共享，保存在
`data/db/operations.sqlite`。重试计入预算，按 UTC 日期切换，不通过重启清零。
升级前的调用和绕开系统的外部请求不自动追溯；采集频率仍遵守各命令的限流配置。

### 7. 可选：记录人工交易和 paper trading

记录一笔真实人工成交：

```bash
uv run quant journal add-fill \
  --symbol NVDA \
  --side BUY \
  --quantity 10 \
  --price 120.50 \
  --stop-price 112.00 \
  --target-price 138.00 \
  --fill-time 2026-08-07T13:30:00+00:00
```

查看当前人工持仓：

```bash
uv run quant journal positions
```

检查已有持仓是否应该继续持有、减仓或退出：

```bash
uv run quant decision positions --date 2026-08-07
```

forward paper trading：

```bash
uv run quant paper update
uv run quant paper advance --date 2026-08-07
uv run quant paper positions
```

人工成交和 paper fills 是分开的 SQLite 表。paper 结果只用于校准流程，不代表真实交易。

## 每周复盘

每周运行或查看最近一次 calibration closure：

```bash
python scripts/strategy_calibration_closure.py
```

重点看：

- `RELAXED` 候选到底提高了机会，还是只是增加噪音；
- defensive overlay 是否足够早地提示了风险；
- `generic_2x_watch` 是有帮助，还是太吵；
- news prompt 有没有改变你的人工判断；
- 是否有 stale data、provider failure 或 quality gate 阻止推荐。

不要因为单个强势周就放宽规则。更重要的是规则在阴跌、反转、横盘、强趋势里都不要失控。

## 常见问题

### `ModuleNotFoundError: No module named 'quant_system'`

用下面任意一种方式：

```bash
uv run quant --help
python scripts/run_daily_workbench.py --no-news-risk
PYTHONPATH=src .venv/bin/python -m quant_system --help
```

### dashboard 的 News tab 是空的

这通常说明当天报告涉及的 symbol 在当前 cutoff 前没有本地 news/events。

先运行：

```bash
uv run quant data update-news --symbols NVDA,AMD,ASML --start 2026-08-03T00:00:00Z
```

然后重新跑：

```bash
python scripts/run_daily_workbench.py --news-risk
```

### 价格更新被 BLOCKED

查看 `update-prices` 输出里的 quality report 路径。关键价格源 missing 或 stale 时必须阻止推荐。不要手工绕过，应该先修复数据源并重新生成报告。

### 日报、扫描或回测返回 BLOCKED

这些入口会直接检查所请求交易日的 SPY、QQQ 和市场 benchmark，不能只凭
上次采集命令成功就继续生成建议。关键日线缺失、未更新到目标交易日或明确标记
为 stale 时，命令输出 `status: BLOCKED` 的 JSON，并以退出码 2 结束；一键
daily runner 会传递同样的状态。先检查 `blocked_reasons`、`latest_sessions`
和目标日期，再修复数据或选择确实有可用数据的历史交易日。

报告里的 `data_cutoff_utc` 使用交易所实际收盘后 30 分钟，不等于收盘时刻。
历史特征按各自交易日的 cutoff 读取数据，后来的价格修订不会覆盖此前决策所用
的版本。原始 Parquet 不会因这些检查被修改。旧数据的可用时间若为推断值，
仍需保留相应研究限制，不能据此认定它是真实的历史发布时间。

### 旧代码、并购和停牌如何处理？

`update-prices --all-stored` 会使用已审核的生命周期事实：更名和股份转换列在
`resolved_symbols`，已退出交易的证券列在 `skipped_inactive_symbols`，不再算作
当前活跃证券的 stale 或提供商失败。原始旧代码价格继续保留；这不代表历史缺口
已经补齐。核心行情失效仍会阻断推荐。

生命周期来自人工审核的 JSON 清单，包含 `events` 数组，每条必须提供来源 URL、
版本、抓取和可用时间、run ID、事件 ID、证券 ID、代码、事件类型及生效日期。
可先验证，再追加导入；完全相同的记录重复导入会跳过：

```bash
.venv/bin/python -m quant_system data import-lifecycle --manifest <reviewed.json> --dry-run
.venv/bin/python -m quant_system data import-lifecycle --manifest <reviewed.json>
.venv/bin/python -m quant_system data update-prices --all-stored
```

更名使用 `SYMBOL_CHANGE`；股份转换使用 `SHARE_CONVERSION` 并填写不同的
`successor_security_id`；并购使用 `MERGER`，不把收购方行情接到旧证券上。
已知停复牌使用 `SUSPENDED` / `RESUMED`。字段和时间边界见 `docs/data-contracts.md`。
事件可用时间晚于历史 cutoff 时，历史扫描不会提前使用它。股票池起止日期均包含
当天，日期之外的成员不会进入日报、轮动或层级诊断。

Yahoo 的 `padded_rows` 和 `action_only_rows` 是空行/公司行动分类；
`dropped_invalid_rows` 是实际行情校验失败。存在这些提示时运行仍可能是
`DEGRADED`，应查看具体证券及行数，不应把质量警告手工改成成功。

### 每天先打开哪个文件？

先打开：

```text
data/reports/daily/<date>/<run_id>/daily_index.md
```

它会链接当天的盘前计划、research list、2x overlay、持仓检查和下一步人工动作。

### 最简每日命令是什么？

如果你只想完成最核心的每日流程：

```bash
python scripts/run_daily_pipeline.py
```

然后打开最新的：

```text
data/reports/daily/<date>/<run_id>/daily_index.md
```
