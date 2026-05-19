# TradingAgents 数据需求映射

参考仓库已克隆到：

```text
/home/tony_9756/TradingAgents
```

上游参考：

- https://github.com/TauricResearch/TradingAgents
- https://www.zdoc.app/zh/TauricResearch/TradingAgents

## TradingAgents 的资料分层

TradingAgents 的核心不是单一数据源，而是把资料拆给不同角色：

| 角色 | 主要资料 | daily_stock_analysis 对应 |
|---|---|---|
| Market Analyst | OHLCV、MA、EMA、MACD、RSI、Bollinger、ATR、VWMA、MFI | `StockTrendAnalyzer` 已补齐扩展指标 |
| News Analyst | 公司新闻、全球宏观新闻 | `SearchService.search_comprehensive_intel` 已有公司新闻，并新增 `global_macro_news` |
| Social/Sentiment Analyst | 社交舆情、公开讨论热度 | `SocialSentimentService` 可用时接入 Reddit/X/Polymarket，搜索层作为兜底 |
| Fundamentals Analyst | 公司概况、财报、资产负债表、现金流、利润表 | 美股使用 SEC EDGAR companyfacts/filings；后续可补 Alpha Vantage/YFinance 报表 |
| Insider Transactions | 内部人交易 | 美股新增 SEC Forms 3/4/5/144 原文链接；买卖方向后续可解析 Form 4 XML |
| Bull/Bear/Risk Debate | 多角度辩论，不是新数据源 | 项目已有 multi-agent 技术/情报/风险/决策链，可后续增加 bull/bear 双方辩论模板 |

## 本次落地内容

1. 技术指标扩展：
   - EMA10
   - MA50 / MA200
   - Bollinger 中轨/上轨/下轨
   - ATR14
   - VWMA20
   - MFI14

2. 搜索维度扩展：
   - `global_macro_news`：Fed、通胀、利率、美债收益率、全球市场新闻
   - `insider_activity`：Form 4、内部人交易/减持等搜索维度

3. SEC EDGAR 扩展：
   - 最近 4 份 10-Q
   - 最近 1 份 10-K
   - 最近 SEC Forms 3/4/5/144 内部人申报链接

## 后续优先级

1. 解析 SEC Form 4 XML，把“买入/卖出、数量、价格、持有人”结构化，而不只是放原文链接。
2. 加 Alpha Vantage 可选 provider，补齐 TradingAgents 里的 `OVERVIEW`、`BALANCE_SHEET`、`CASH_FLOW`、`INCOME_STATEMENT`、`INSIDER_TRANSACTIONS`。
3. 在现有 multi-agent 基础上增加 Bull/Bear Debate 与 Risk Debate 的报告小节。
4. 对港股/A股复用同一张映射表：行情、财报/公告、宏观/行业新闻、内部人/股东变动、同业估值。
