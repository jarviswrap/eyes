# GitHub Trending Eyes

每日抓取 [GitHub Weekly Trending](https://github.com/trending?since=weekly) 前 20 项目，支持 DeepSeek LLM 手动触发四维度分析（功能 / 技术栈 / 痛点 / 竞品），并提供 Search API 自定义搜索。所有数据存入本地 SQLite，Web 页面查看、导出 Markdown 报告。用户认证基于 [user-service](../user-service/) 的 JWT 方案，支持登录注册和角色权限控制。

## 快速开始

```bash
pip install -r requirements.txt

# 环境变量
export LLM_API_KEY="sk-xxx"       # DeepSeek API Key（如需 LLM 分析）
export JWT_SECRET="共享密钥"       # 与 user-service 一致（如需登录注册）

# 启动 Web 服务
python web_server.py               # http://localhost:8080
```

## 页面功能

| Tab | 功能 |
|-----|------|
| **Trendings** | 按时间倒序展示每次抓取的 Pull，展开查看项目列表，手动触发分析或生成总结 |
| **Searching** | GitHub Search API 自定义查询（时间范围 / Fork 数 / 排序），结果自动保存为 Pull |
| **项目报告** | 已导出 Markdown 报告列表，点击可查看，支持删除 |
| **设置** | 拉取数量、GitHub Token、定时 Pull 开关、自动分析开关（仅超级管理员可见） |

### 操作权限

| 操作 | 未登录 | 普通用户 | 超管 |
|------|:---:|:---:|:---:|
| 浏览数据 | ✅ | ✅ | ✅ |
| 搜索项目 | ✅ | ✅ | ✅ |
| 开始分析 | - | ✅ | ✅ |
| 导出报告 | - | ✅ | ✅ |
| 抓取数据 | - | ✅ | ✅ |
| 设置 | - | - | ✅ |

## 项目报告

展开 Pull 后可导出为 Markdown，保存到 `reports/` 目录。README 中报告列表自动刷新。

| [trending-2026-06-23-131909.md](reports/trending-2026-06-23-131909.md) |  |  |  |

## 配置文件

`config.yaml` 可调整：

```yaml
scheduler:
  timezone: "Asia/Shanghai"

llm:
  api_key: "${LLM_API_KEY}"
  model: "deepseek-chat"

github:
  token: "${GITHUB_TOKEN}"
  per_page: 20
```
