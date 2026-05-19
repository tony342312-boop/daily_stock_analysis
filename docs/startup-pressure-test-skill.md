# Codex Startup Pressure Test Skill 使用说明

`startup-pressure-test` 是一个 Codex skill，用来在开工前压测产品想法、功能方向和商业化假设。它不接入 daily_stock_analysis 的运行时链路，也不是行情/新闻/财报数据源；更适合放在产品规划阶段，避免先做一堆功能，最后发现没有用户愿意用或付费。

## 安装状态

已安装到：

```text
~/.codex/skills/startup-pressure-test
```

安装命令：

```bash
npx --yes codex-startup-pressure-test-skill@latest
```

安装后需要重启 Codex，新的会话里才能直接识别：

```text
Use $startup-pressure-test to pressure-test this startup idea: ...
```

## 可以怎么用到本项目

### 1. 压测核心产品定位

适合在决定网站要面向谁、怎么收费、主打什么差异化之前使用。

```text
Use $startup-pressure-test to pressure-test this startup idea:

我想把 daily_stock_analysis 做成一个面向中文投资者的 AI 股票诊断网站。
它支持美股、港股、A股，自动整合实时行情、SEC/财报、新闻搜索、宏观数据、估值对比和 PDF 报告。
目标用户是个人投资者和小型投研工作室。
用户输入股票代码后，系统给出操作建议、风险、买入/止损/止盈区间、财报链接和新闻引用。
我希望未来通过订阅制收费。
```

重点看它输出的：

- `Core Assumption`：这个产品必须成立的核心假设是什么
- `Fatal Flaws`：哪里最可能是假需求
- `Competition`：用户现在用什么替代方案
- `First 10 Customers`：最早应该找谁验证
- `MVP`：两周内应该验证什么，而不是继续堆功能

### 2. 压测单个功能值不值得做

每次要做大功能前先跑一遍，尤其是这些方向：

- 港股/A股完整扩展
- X/Twitter 舆情抓取
- 自建 SearXNG 搜索服务
- 自动生成 PDF 投研报告
- 飞书/微信/邮件推送
- 用户登录、收藏、组合跟踪
- 同行业估值对比
- 付费订阅和额度限制

示例：

```text
Use $startup-pressure-test to pressure-test this feature idea:

我想给 daily_stock_analysis 增加 X/Twitter 舆情模块。
它会自动搜索某只美股最近 7 天的 Twitter 讨论、KOL 观点和情绪变化，
并把结果融合进 AI 诊股报告里。
目标是让用户更快发现市场情绪变化。
```

### 3. 找第一批真实用户

当系统功能可以演示后，优先用 `first-10-customers` 模式，不要先做广告或复杂增长。

```text
Use $startup-pressure-test to find the first 10 customers for this idea:

daily_stock_analysis 是一个 AI 股票分析 WebUI。
它每天自动生成美股/港股/A股诊股报告，包含行情、新闻、财报、宏观、估值和 PDF。
目标客户是中文个人投资者、投资社群群主和小型投研工作室。
```

### 4. 定义两周 MVP

当功能太多、路线变散时，用 `mvp-plan` 把范围砍小。

```text
Use $startup-pressure-test to build a 2-week MVP plan for this idea:

做一个 AI 股票诊断网站，用户输入股票代码，系统用行情、新闻、SEC 财报和 LLM 生成可分享的 Markdown/PDF 报告。
先只做美股，目标验证用户是否愿意每天打开或转发报告。
```

## 建议工作流

```text
Reddtrends / Reddit / X / 社群反馈
        ↓
用 startup-pressure-test 压测真实痛点和第一批用户
        ↓
只挑 strong 或 pivot 后仍强的方向进入开发
        ↓
daily_stock_analysis 实现功能
        ↓
WebUI / PDF / 推送收集真实使用反馈
        ↓
再次压测下一轮功能
```

## 注意事项

- 这个 skill 不能替代真实用户访谈，只能帮你更快暴露假设。
- 它不会自动联网找市场数据；如果需要最新竞品或定价信息，要让 Codex 搜索并引用来源。
- 对本项目来说，它最适合用于产品判断，不适合塞进股票分析 pipeline。
- 如果 Codex 不认识 `$startup-pressure-test`，重启 Codex 后再试。
