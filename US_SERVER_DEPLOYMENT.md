# 美国服务器部署说明

这份说明给服务器上的 AI / 运维助手使用。目标是把本目录 `daily_stock_analysis` 部署到美国 Linux 服务器，提供 WebUI、公网访问、PDF 下载和后台常驻运行。

## 0. 结论

可以把整个 `/home/tony_9756/daily_stock_analysis` 复制到服务器，但复制后还必须做这些事：

- 安装 Python 依赖。
- 安装 Node 依赖并重新构建前端到 `static/`。
- 配置 `.env`，尤其是 LLM、搜索、行情和数据库路径。
- 安装 Chromium，保证 WebUI PDF 下载可用。
- 用 systemd 常驻运行后端。
- 用 Nginx 或 Cloudflare Tunnel 暴露公网访问。

## 1. 推荐服务器环境

- Ubuntu 22.04 / 24.04
- Python 3.10 或 3.11
- Node.js 20+
- 2 核 CPU / 4GB RAM 起步，建议 4 核 / 8GB
- 磁盘至少 20GB

安装基础依赖：

```bash
sudo apt-get update
sudo apt-get install -y \
  git curl rsync build-essential nginx \
  fonts-noto-cjk chromium
```

安装 Node.js 20：

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs
```

## 2. 复制项目

推荐放到：

```text
/opt/daily_stock_analysis
```

从本机复制：

```bash
rsync -az \
  --exclude node_modules \
  --exclude __pycache__ \
  --exclude .pytest_cache \
  /home/tony_9756/daily_stock_analysis/ \
  USER@SERVER_IP:/opt/daily_stock_analysis/
```

如果希望迁移本机已有历史记录，必须确认这些内容也被复制：

```text
.env
data/stock_analysis.db
reports/
static/
```

注意：`.env` 里有 API Key，服务器上执行：

```bash
chmod 600 /opt/daily_stock_analysis/.env
```

## 3. 创建运行用户

```bash
sudo useradd --system --create-home --shell /bin/bash dsa || true
sudo chown -R dsa:dsa /opt/daily_stock_analysis
```

后续命令如果用 `dsa` 用户执行：

```bash
sudo -iu dsa
cd /opt/daily_stock_analysis
```

## 4. Python 环境

推荐使用 conda。假设 conda 安装在 `/opt/miniconda3`：

```bash
conda create -n daily_stock_analysis python=3.11 -y
conda activate daily_stock_analysis

cd /opt/daily_stock_analysis
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

如果服务器没有 conda，也可以用 venv：

```bash
cd /opt/daily_stock_analysis
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

使用 venv 时，后面的 systemd 里的 `PYTHON_BIN` 要改成：

```text
/opt/daily_stock_analysis/.venv/bin/python
```

## 5. 前端构建

```bash
cd /opt/daily_stock_analysis/apps/dsa-web
npm ci
npm run build
```

构建输出位置：

```text
/opt/daily_stock_analysis/static
```

如果 `static/index.html` 不存在，WebUI 会打不开。

## 6. `.env` 配置

最少需要一个 LLM Key。例如：

```env
GEMINI_API_KEY=...
# 或
OPENAI_API_KEY=...
OPENAI_BASE_URL=...
OPENAI_MODEL=...
```

建议保留或设置：

```env
DATABASE_PATH=./data/stock_analysis.db
WEBUI_ENABLED=false
WEBUI_HOST=127.0.0.1
WEBUI_PORT=8000
ADMIN_AUTH_ENABLED=true
```

项目内也放了一个服务器模板：

```text
deploy/env.server.example
```

可以这样初始化：

```bash
cp /opt/daily_stock_analysis/deploy/env.server.example /opt/daily_stock_analysis/.env
chmod 600 /opt/daily_stock_analysis/.env
vim /opt/daily_stock_analysis/.env
```

搜索源建议至少配置：

```env
TAVILY_API_KEYS=...
EXA_API_KEY=...
FRED_API_KEY=...
SEC_EDGAR_ENABLED=true
SEC_EDGAR_USER_AGENT=daily_stock_analysis/1.0 admin@stock.cn.mt
```

如果要开放给别人访问，强烈建议启用认证：

```env
ADMIN_AUTH_ENABLED=true
```

然后按项目认证接口或现有 Web 设置页创建管理员账号。没有用户隔离前，公网用户仍可能看到同一套历史记录。

## 7. PDF 下载支持

项目的 WebUI PDF 下载会调用：

```text
scripts/chromium-markdown-pdf.sh
```

这个脚本现在会自动寻找 `chromium`、`chromium-browser`、`google-chrome` 或 Playwright 缓存的 Chromium。

检查：

```bash
cd /opt/daily_stock_analysis
chmod +x scripts/chromium-markdown-pdf.sh
scripts/chromium-markdown-pdf.sh --version
```

如果找不到浏览器，安装：

```bash
sudo apt-get install -y chromium
```

或者手动指定：

```bash
export CHROME=/usr/bin/chromium
```

## 8. 本地启动测试

```bash
cd /opt/daily_stock_analysis
conda activate daily_stock_analysis
python main.py --webui-only --host 127.0.0.1 --port 8000
```

另开一个 shell：

```bash
curl http://127.0.0.1:8000/api/health
```

正常返回类似：

```json
{"status":"ok","timestamp":"..."}
```

## 9. systemd 常驻运行

项目内已经准备模板：

```text
deploy/daily-stock-analysis.service.example
```

复制：

```bash
sudo cp /opt/daily_stock_analysis/deploy/daily-stock-analysis.service.example \
  /etc/systemd/system/daily-stock-analysis.service
```

编辑：

```bash
sudo vim /etc/systemd/system/daily-stock-analysis.service
```

至少确认：

```ini
User=dsa
Group=dsa
WorkingDirectory=/opt/daily_stock_analysis
EnvironmentFile=/opt/daily_stock_analysis/.env
Environment=PYTHON_BIN=/opt/miniconda3/envs/daily_stock_analysis/bin/python
```

启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable daily-stock-analysis
sudo systemctl start daily-stock-analysis
sudo systemctl status daily-stock-analysis
```

查看日志：

```bash
journalctl -u daily-stock-analysis -f
```

## 10. Nginx 反向代理

项目内已经准备模板：

```text
deploy/nginx-daily-stock-analysis.conf.example
```

复制：

```bash
sudo cp /opt/daily_stock_analysis/deploy/nginx-daily-stock-analysis.conf.example \
  /etc/nginx/sites-available/daily-stock-analysis
```

编辑域名：

```bash
sudo vim /etc/nginx/sites-available/daily-stock-analysis
```

把：

```nginx
server_name your-domain.com;
```

改成你的域名。如果暂时没有域名，可以先写服务器 IP 或 `_`。

启用：

```bash
sudo ln -sf /etc/nginx/sites-available/daily-stock-analysis \
  /etc/nginx/sites-enabled/daily-stock-analysis
sudo nginx -t
sudo systemctl reload nginx
```

访问：

```text
http://your-domain.com
```

## 11. HTTPS

如果有域名：

```bash
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
```

如果没有域名，可以先用 Cloudflare Tunnel。

## 12. Cloudflare Tunnel 可选

安装：

```bash
mkdir -p "$HOME/bin"
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o "$HOME/bin/cloudflared"
chmod +x "$HOME/bin/cloudflared"
```

临时 tunnel：

```bash
cd /opt/daily_stock_analysis
CLOUDFLARED="$HOME/bin/cloudflared" ./scripts/start-cloudflare-tunnel.sh
```

固定域名建议用 Cloudflare Named Tunnel，并把 tunnel 指向：

```text
http://127.0.0.1:8000
```

## 13. 防火墙

如果用 Nginx：

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
```

不建议直接开放 8000 到公网。正式部署时后端只监听 `127.0.0.1:8000`，外部通过 Nginx 或 Cloudflare 进入。

## 14. 验证清单

```bash
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/v1/history?limit=1
```

打开网页后检查：

- 首页能加载。
- 能提交股票分析。
- 远程访问能看到任务进度。
- 完整报告能打开。
- 完整报告右上角能下载 PDF。

PDF 接口可用性测试：

```bash
curl -L -o /tmp/report.pdf \
  http://127.0.0.1:8000/api/v1/history/RECORD_ID/pdf
file /tmp/report.pdf
```

应该显示 `PDF document`。

## 15. 常见问题

### Web 页面空白

通常是前端没构建：

```bash
cd /opt/daily_stock_analysis/apps/dsa-web
npm ci
npm run build
```

确认：

```bash
ls -lh /opt/daily_stock_analysis/static/index.html
```

### PDF 下载失败

检查 Chromium：

```bash
which chromium || which chromium-browser || which google-chrome
/opt/daily_stock_analysis/scripts/chromium-markdown-pdf.sh --version
```

### systemd 启动失败

看日志：

```bash
journalctl -u daily-stock-analysis -n 200 --no-pager
```

重点检查：

- `PYTHON_BIN` 路径是否正确。
- `.env` 是否存在。
- `/opt/daily_stock_analysis` 是否属于 `dsa` 用户。
- Python 依赖是否安装完整。

### 别人看到相同历史记录

当前系统默认是共享数据库。要让每个人只能看到自己的记录，需要增加用户身份和后端权限过滤。临时内测可以用 Cloudflare Access，正式版本建议做登录用户隔离。
