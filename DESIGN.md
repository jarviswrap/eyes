# 重构：以 Trending Pull 为中心的数据模型

## Context

当前模型以「日期」为中心（daily_rankings 按天存排名，project_analyses 按天存分析）。用户要求重构为以「拉取记录」为中心：每次抓取操作产生一条 trending pull 记录，记录拉取时间和包含的项目。LLM 分析改为手动触发，每个项目只保留最新一份分析报告。周趋势总结跟随 trending pull 保存，手动触发。

## 新旧对比

| 维度 | 旧 | 新 |
|------|-----|-----|
| 排名存储 | `daily_rankings`（按日期+repo） | `trending_pulls` + `trending_pull_items`（每次拉取一条 pull，关联多个 repo） |
| 分析存储 | `project_analyses`（按 repo+date，可有多份历史分析） | `project_analyses`（按 repo UNIQUE，仅保留最新一份） |
| LLM 分析触发 | 自动（每次抓取后立即并发分析全部 20 个） | 手动（前端按钮：单个项目分析 / 整条 pull 批量分析） |
| 周总结 | 自动（每周日自动生成） | 手动（前端对某条 pull 点击「生成总结」，结果存 pull.summary） |
| 前端主页 | Dashboard 统计卡片 + Top 10 | Trending Pull 时间线列表 |

---

## 新数据模型

### ER 关系

```
repositories (项目表，full_name 唯一)
    │
    ├── 1:N ── trending_pull_items (pull 与 repo 关联，含排名/star/forks)
    │              │
    │              N:1 ── trending_pulls (拉取记录，含时间戳+周总结)
    │
    ├── 1:1 ── project_analyses (每 repo 仅一条，最新分析)
    │
    └── 1:1 ── consecutive_tracking (每 repo 仅一条，近5天出现次数)
```

### 删除的表
- `daily_rankings` — 由 `trending_pulls` + `trending_pull_items` 替代
- `weekly_summaries` — 周总结跟随 `trending_pulls` 存储（`summary` 列）

### 保留的表（不变）
- `repositories` — `id`, `github_id`, `full_name(UNIQUE)`, `url`, `description`, `language`

### 新建的表

```sql
-- trending_pulls: 每次抓取产生一条记录
CREATE TABLE trending_pulls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pulled_at TIMESTAMP NOT NULL DEFAULT (datetime('now')),
    project_count INTEGER NOT NULL DEFAULT 0,
    summary TEXT  -- 周总结 Markdown，手动触发后填充
);

-- trending_pull_items: pull 包含的项目列表
CREATE TABLE trending_pull_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pull_id INTEGER NOT NULL REFERENCES trending_pulls(id),
    repo_id INTEGER NOT NULL REFERENCES repositories(id),
    rank INTEGER NOT NULL,         -- 1-20
    stars INTEGER NOT NULL DEFAULT 0,
    forks INTEGER NOT NULL DEFAULT 0,
    UNIQUE(pull_id, repo_id)
);
```

### 修改的表

```sql
-- project_analyses: 旧约束 (repo_id, analysis_date)，新约束 (repo_id UNIQUE)
CREATE TABLE project_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER UNIQUE NOT NULL REFERENCES repositories(id),
    analyzed_at TIMESTAMP NOT NULL DEFAULT (datetime('now')),
    functionality TEXT,
    tech_stack TEXT,
    pain_points TEXT,
    competitors TEXT,
    raw_response TEXT
);
```

```sql
-- consecutive_tracking: 列不变，统计逻辑改为查 trending_pull_items
CREATE TABLE consecutive_tracking (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER UNIQUE NOT NULL REFERENCES repositories(id),
    first_seen DATE,
    last_seen DATE,
    appearance_days INTEGER NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT 0
);
```

---

## 新流水线 (scheduler.py run_once)

```
步骤 1: 抓取 GitHub Trending → list[TrendingRepo]

步骤 2: 创建 trending_pull + 保存项目 + 写入 pull_items
    - INSERT trending_pulls (pulled_at=now, project_count=N)
    - 对每个 TrendingRepo:
        - upsert_repository (重复项目自动跳过)
        - INSERT trending_pull_items (pull_id, repo_id, rank, stars, forks)
    - 幂等性: force=True 时先删除同一 pulled_at 日期范围内的 pull（可选）

步骤 3: (已移除 - 不再自动 LLM 分析)

步骤 4: 更新连续出现跟踪
    - 对每个 repo: 统计近5天在 trending_pull_items 中出现的天数
    - appearance_days >= 2 → is_active = True

步骤 5: (已移除 - 周总结改为手动触发)
```

---

## 前端设计 (index.html)

### 页面结构：两列布局

```
┌─ Header (Logo + 操作按钮) ─────────────────────────────┐
├─ Tab Nav ──────────────────────────────────────────────┤
│  [📋 Pull 列表] [🔥 重点关注] [📄 导出报告]               │
├────────────────────────────────────────────────────────┤
│                                                        │
│  ┌─ Trending Pull 列表 ───────────────────────────┐    │
│  │                                                │    │
│  │  ▼ Pull #3  2026-06-22 10:30  (19 projects)   │    │
│  │  ┌─────────────────────────────────────────┐   │    │
│  │  │ #1 repo/name  ★1,234  近5天:3次 [分析]  │   │    │
│  │  │ #2 repo/name  ★987   近5天:2次 [分析]  │   │    │
│  │  │ ...                                     │   │    │
│  │  │                    [📝 生成周总结]       │   │    │
│  │  └─────────────────────────────────────────┘   │    │
│  │                                                │    │
│  │  ▶ Pull #2  2026-06-21 21:41  (19 projects)   │    │
│  │  ▶ Pull #1  2026-06-21 13:21  (20 projects)   │    │
│  └────────────────────────────────────────────────┘    │
│                                                        │
│  右侧面板 (选中项目时显示):                               │
│  ┌─ 项目详情 ──────────────────────────────────────┐    │
│  │  repo/name                                     │    │
│  │  描述: ...                                     │    │
│  │  语言: Python | 近5天上榜: 3次                   │    │
│  │                                                │    │
│  │  📊 LLM 分析报告 (2026-06-22 11:00)             │    │
│  │  ┌───────────────────────────────────────┐     │    │
│  │  │ 💡 功能概述: ...                      │     │    │
│  │  │ ⚙️ 技术栈: ...                        │     │    │
│  │  │ 🎯 痛点: ...                          │     │    │
│  │  │ 🏆 竞品: ...                          │     │    │
│  │  └───────────────────────────────────────┘     │    │
│  └────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────┘
```

### 交互流程

1. **Pull 列表 Tab**：倒序展示所有 trending pull，每行显示拉取时间 + 项目数
2. **点击 Pull 展开**：显示该项目列表（排名、名称、star、近5天出现次数）
3. **单项目分析**：点击项目行的「分析」→ 调用 `POST /api/projects/{id}/analyze` → 分析完成后该行自动刷新显示结果
4. **批量分析**：点击 pull 头部的「全部分析」→ 调用 `POST /api/trending-pulls/{id}/analyze` → 逐一分析该 pull 下所有项目
5. **生成周总结**：点击 pull 底部的「生成周总结」→ 调用 `POST /api/trending-pulls/{id}/summarize` → LLM 汇总所有项目分析，结果存回 `pull.summary`，展开显示
6. **项目详情**：点击项目名称 → 右侧面板显示项目信息 + 分析结果

### 分析状态指示

每个项目行有状态图标：
- ⬜ 未分析
- 🔄 分析中（spinner）
- ✅ 已分析（显示分析时间）

---

## API 设计

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/trending-pulls` | Pull 列表，按时间倒序 |
| `GET` | `/api/trending-pulls/latest` | 最新一条 pull 的详情 + 项目列表 |
| `GET` | `/api/trending-pulls/{id}` | 指定 pull 详情 + 项目列表（含每个项目的分析状态和近5天次数） |
| `POST` | `/api/projects/{id}/analyze` | 对单个项目执行 LLM 分析，覆盖旧分析 |
| `POST` | `/api/trending-pulls/{id}/analyze` | 对该 pull 下所有项目逐一分析 |
| `POST` | `/api/trending-pulls/{id}/summarize` | 对该 pull 生成周总结（基于已有项目分析） |
| `GET` | `/api/projects/{id}` | 项目详情 + 最新分析 + 跟踪状态 |
| `GET` | `/api/projects?search=` | 项目搜索 |
| `GET` | `/api/highlights` | 当前活跃的重点关注项目 |
| `GET` | `/api/reports` | 已导出报告列表 |
| `POST` | `/api/export/{date}` | 导出指定日期报告 |
| `POST` | `/api/trigger/run-once` | 触发一次抓取 |
| `POST` | `/api/trigger/dry-run` | 干运行 |
| `POST` | `/api/trigger/daemon/*` | 守护进程控制 |

### 关键 API 响应格式

**GET `/api/trending-pulls/{id}`**
```json
{
  "id": 3,
  "pulled_at": "2026-06-22T10:30:00Z",
  "project_count": 19,
  "summary": null,
  "items": [
    {
      "repo_id": 1,
      "full_name": "owner/repo",
      "url": "https://github.com/...",
      "description": "...",
      "language": "Python",
      "rank": 1,
      "stars": 1234,
      "forks": 56,
      "appearance_days": 3,
      "analysis_status": "done",
      "analyzed_at": "2026-06-22T11:00:00Z"
    }
  ]
}
```

---

## 涉及文件清单

| 文件 | 改动程度 |
|------|----------|
| `src/database.py` | 🔴 重写 — 模型+CRUD 全量替换 |
| `src/scheduler.py` | 🟡 中等 — 移除自动分析+周总结，调整数据写入 |
| `src/analyzer.py` | 🟢 小改 — 分析结果写入改为 upsert by repo_id |
| `src/tracker.py` | 🟢 小改 — 统计来源改为 trending_pull_items |
| `src/summarizer.py` | 🟡 中等 — 改为按 pull 维度生成 |
| `src/config.py` | 🟢 不变 |
| `main.py` | 🟢 不变 |
| `src/fetcher.py` | 🟢 不变 |
| `web_server.py` | 🔴 重写 — API 全量重构 |
| `src/web/templates/index.html` | 🔴 重写 — UI 全量重构 |

---

## 验证步骤

1. 删除 `data/trending.db`
2. `python web_server.py` 启动
3. 点击「立即执行」触发抓取 → 产生一条 pull（19-20 个项目）
4. Pull 列表出现新记录，点击展开看到项目列表，每个项目显示「未分析」
5. 点击单个项目「分析」→ 等待 3-8 秒 → 项目行状态变为「已分析」
6. 点击展开项目名称 → 右侧面板展示四维度分析
7. 点击「全部分析」→ 批量分析所有未分析项目
8. 点击「生成周总结」→ 等待 LLM 汇总 → pull 下方展示 Markdown 总结
9. 再次「立即执行」→ 产生新 pull → 项目「近5天出现次数」更新
10. 切换到「重点关注」Tab → 看到 >=2 次的项目
