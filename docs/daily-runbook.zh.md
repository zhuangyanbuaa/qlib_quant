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

如果未来你提供 broker snapshot CSV/JSON，open gate 可以基于 `symbol,last_price,news_risk` 等字段返回 `KEEP`、`DEFER` 或 `CANCEL`。

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

### 每天先打开哪个文件？

先打开：

```text
data/reports/daily/<date>/<run_id>/daily_index.md
```

它会链接当天的盘前计划、research list、2x overlay、持仓检查和下一步人工动作。

### 最简每日命令是什么？

如果你只想完成最核心的每日流程：

```bash
uv run quant data update-prices --all-stored
python scripts/run_daily_workbench.py --no-news-risk
```

然后打开最新的：

```text
data/reports/daily/<date>/<run_id>/daily_index.md
```
