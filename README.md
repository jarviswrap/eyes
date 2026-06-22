# GitHub Weekly Trending 分析系统

每天定时抓取 [GitHub Weekly Trending](https://github.com/trending?since=weekly) 前 20 项目，通过 DeepSeek LLM 逐一分析功能、技术栈、痛点、竞品，并汇总生成周趋势总结。所有数据存入本地 SQLite，提供 Web 仪表盘查看和报告导出。

## 快速开始

```bash
pip install -r requirements.txt
export LLM_API_KEY="sk-xxx"          # DeepSeek API Key（必需）
export GITHUB_TOKEN="ghp_xxx"        # GitHub Token（可选）

python main.py --run-once             # 立即执行一次
python web_server.py                  # 启动 Web 仪表盘 → http://localhost:8080
```

## Web 仪表盘

- **仪表盘** — 统计概览 + Top 10 + 重点关注 + 导出报告列表
- **每日 Trending** — 按日期浏览排名，点击展开 LLM 分析详情，一键导出 Markdown
- **项目详情** — 搜索项目，查看分析历史和排名记录
- **周趋势总结** — LLM 生成的周趋势报告
- **重点关注** — 近 5 天上榜 ≥2 次的项目

页面顶部的操作按钮可直接触发抓取分析、守护进程启停。

## 导出报告

每日 Trending 页面支持将分析结果导出为 Markdown 文件，保存到 `reports/` 目录。

| [trending-2026-06-22.md](reports/trending-2026-06-22.md) | [trending-2026-06-22-pull1.md](reports/trending-2026-06-22-pull1.md) | [trending-2026-06-22-045119.md](reports/trending-2026-06-22-045119.md) | [trending-2026-06-21.md](reports/trending-2026-06-21.md) |

## 定时运行

```bash
python main.py          # 启动守护进程，每天 config.yaml 配置的时间自动执行
```

配置文件 `config.yaml` 可调整运行时间、LLM 参数、数据库路径等。
