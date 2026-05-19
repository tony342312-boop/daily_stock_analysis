# Daily Stock Analysis 测试站工作流

## 当前环境

- 正式站目录：`/opt/daily_stock_analysis`
- 正式站服务：`daily-stock-analysis.service`
- 正式站内部端口：`127.0.0.1:8000`
- 正式站域名：`http://stock.cn.mt/`

- 测试站目录：`/opt/daily_stock_analysis_staging`
- 测试站服务：`daily-stock-analysis-staging.service`
- 测试站内部端口：`127.0.0.1:8001`
- 测试站域名：`http://staging.47-251-20-121.sslip.io/`
- 可选自有测试域名：`http://test.stock.cn.mt/`（需要 DNS A 记录指向 `47.251.20.121` 后可用）

## 验证命令

```bash
systemctl is-active daily-stock-analysis.service
systemctl is-active daily-stock-analysis-staging.service
curl --noproxy '*' -sS -o /dev/null -w 'prod=%{http_code}\n' http://stock.cn.mt/api/health
curl --noproxy '*' -sS -o /dev/null -w 'staging=%{http_code}\n' http://staging.47-251-20-121.sslip.io/api/health
curl --noproxy '*' -sS -o /dev/null -w 'raw-ip=%{http_code}\n' http://47.251.20.121/api/health
```

期望：正式站 `200`，测试站 `200`，裸 IP `404`。

## 以后改动流程

1. 只在测试站目录 `/opt/daily_stock_analysis_staging` 改代码。
2. 如果改了前端：
   ```bash
   cd /opt/daily_stock_analysis_staging/apps/dsa-web
   npm run lint
   npm run build
   ```
3. 如果改了后端：
   ```bash
   cd /opt/daily_stock_analysis_staging
   ./.venv/bin/python -m py_compile main.py server.py webui.py
   ```
4. 重启测试站：
   ```bash
   sudo systemctl restart daily-stock-analysis-staging.service
   ```
5. 打开测试站确认：`http://staging.47-251-20-121.sslip.io/`
6. 确认没问题后，再把同一批代码改动复制到正式站，并重启 `daily-stock-analysis.service`。

## 注意

- 测试站是从正式站复制出来的初始快照，代码、静态页面、历史数据起点一致。
- 两个站使用不同进程和端口；测试站重启不会影响正式站。
- 目前两边 `.env` 内容一致，生产密钥不会写入本文档。以后如果测试站要避免真实通知/真实交易类副作用，需要单独把测试站 `.env` 的通知配置改成测试通道或关闭。
- HTTPS 仍受当前服务器 443 被 sshd 占用的限制；本次只配置 HTTP。
