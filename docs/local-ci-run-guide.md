# 本地 CI 与测试运行指南

这份说明用于 WSL / Linux 本机环境，也可以直接发给另一个 AI 作为执行步骤。当前项目不要使用 Hermes 自己的 venv，统一使用 WSL 里的 `daily_stock_analysis` conda 环境。

## 1. 进入正确环境

交互 shell：

```bash
cd /home/tony_9756/daily_stock_analysis
source /home/tony_9756/miniconda3/etc/profile.d/conda.sh
conda activate daily_stock_analysis
which python
python -V
```

期望 Python 路径：

```text
/home/tony_9756/miniconda3/envs/daily_stock_analysis/bin/python
```

非交互 shell 推荐直接用：

```bash
/home/tony_9756/miniconda3/bin/conda run -n daily_stock_analysis python -V
```

如需补齐 CI 依赖：

```bash
python -m pip install -r requirements-ci.txt
```

## 2. 后端快速门禁

```bash
./scripts/ci_gate.sh
```

也可以分阶段跑：

```bash
./scripts/ci_gate.sh syntax
./scripts/ci_gate.sh flake8
./scripts/ci_gate.sh deterministic
./scripts/ci_gate.sh offline-tests
```

`offline-tests` 已限定为：

```bash
python -m pytest tests -m "not network"
```

不要在本地直接跑 `python -m pytest -m "not network"`，因为 `setup.cfg` 的 `testpaths = .` 会把根目录 `test_env.py::test_llm` 也收进去；那个测试会触发真实 LLM 调用，容易 600 秒超时。

## 3. 后端重点回归

当前常用 focused tests：

```bash
python -m pytest \
  tests/test_config_env_compat.py \
  tests/test_main_schedule_mode.py \
  tests/test_search_tavily_provider.py \
  -q
```

如果只验证本次修复的 4 个历史失败点：

```bash
python -m pytest \
  tests/test_config_env_compat.py::ConfigEnvCompatibilityTestCase::test_schedule_run_immediately_falls_back_to_legacy_run_immediately \
  tests/test_config_env_compat.py::ConfigEnvCompatibilityTestCase::test_empty_legacy_run_immediately_stays_false_when_schedule_alias_is_unset \
  tests/test_main_schedule_mode.py::MainScheduleModeTestCase::test_schedule_time_provider_propagates_config_read_failures \
  tests/test_search_tavily_provider.py::TestTavilySearchProvider::test_search_comprehensive_intel_uses_dimension_specific_topic_for_tavily \
  -q
```

## 4. 前端检查

```bash
cd /home/tony_9756/daily_stock_analysis/apps/dsa-web
npm run lint
npm run build
npm run test
```

如果 `npm run lint` 报 React hooks / setState 相关错误，先修 lint 再 build。构建产物和测试缓存不要手动删除，除非明确知道来源。

## 5. 注意事项

- 不要把 `.env` 里的 key 打到日志里。
- 不要使用 Hermes venv 跑这个项目，依赖集合不一致。
- 网络类、真实 LLM、真实行情接口测试不属于离线门禁；需要单独带说明跑。
- 本地工作区可能有用户未提交改动，只改当前任务相关文件，不要重置或回滚无关文件。
