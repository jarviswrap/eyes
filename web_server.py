"""GitHub Trending 分析系统 — Web 展示服务。"""

import argparse
import asyncio
import os
import sys
import threading
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import jwt
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import func as sa_func

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "deploy"))
from logutil import setup_logging, get_logger

from src.database import (
    Database,
    Repository,
    TrendingPull,
    TrendingPullItem,
    ProjectAnalysis,
    ConsecutiveTracking,
)
from main import (
    load_config,
    run_once as main_run_once,
    start_scheduler,
)
from src.analyzer import LLMAnalyzer
from src.summarizer import WeeklySummarizer

logger = get_logger("web_server")

# ── 全局状态 ──────────────────────────────────────────────
_scheduler_instance = None
_scheduler_lock = threading.Lock()
_config = load_config()
log_dir = str(Path(__file__).parent.parent / "deploy" / "logs")
setup_logging("eyes", log_dir=log_dir)

# ── 组件初始化 ──────────────────────────────────────────
db = Database(_config.database.path)
db.create_tables()
TEMPLATES_DIR = Path(__file__).parent / "src" / "web" / "templates"
REPORTS_DIR = Path(__file__).parent / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
README_PATH = Path(__file__).parent / "README.md"

# 自动恢复定时 Pull
def _auto_restore_scheduler():
    """启动时检查是否启用定时 Pull，若启用则自动启动调度器。"""
    from src.database import get_crud
    global _scheduler_instance
    with _scheduler_lock:
        crud = get_crud(_config.database.path)

        # ── 自愈：检查并清理历史重复 pull ──
        dup_removed = crud.dedup_all_pulls()
        if dup_removed > 0:
            logger.warning("自愈清理: 移除了 %d 条历史重复 pull", dup_removed)

        # ── 自愈：清理僵尸锁（PID 不存在但锁还在） ──
        owner = crud.get_scheduler_owner()
        if owner and not owner["is_alive"]:
            logger.warning("检测到僵尸调度器锁 PID=%s，自动释放", owner["pid"])
            crud.release_scheduler_lock()

        if crud.get_setting("auto_pull", "false") == "true":
            start_time = crud.get_setting("pull_start_time", "09:00")
            period_mode = crud.get_setting("pull_period_mode", "interval")
            interval_hours = float(crud.get_setting("pull_interval_hours", "24"))
            logger.info("检测到 auto_pull=true，自动启动调度器 mode=%s", period_mode)

            if not crud.acquire_scheduler_lock(mode="web_server"):
                owner = crud.get_scheduler_owner()
                logger.warning(
                    "无法获取调度器锁，已有调度器运行中: PID=%s mode=%s",
                    owner["pid"] if owner else "?", owner["mode"] if owner else "?"
                )
                return

            try:
                _scheduler_instance = start_scheduler(
                    _config, start_time, period_mode, interval_hours,
                    on_once_complete=_on_once_done,
                )
                logger.info("调度器已自动恢复")
            except Exception as e:
                crud.release_scheduler_lock()
                logger.error("自动启动调度器失败: %s", e)


def _restart_scheduler():
    """根据最新设置重启调度器。加锁防并发，含跨进程互斥。"""
    global _scheduler_instance
    if not _scheduler_lock.acquire(blocking=False):
        logger.warning("调度器正在重启中，跳过本次请求")
        return
    try:
        from src.database import get_crud
        crud = get_crud(_config.database.path)

        # 停止旧调度器（等待当前任务完成）
        if _scheduler_instance:
            try:
                _scheduler_instance.shutdown(wait=True)
            except Exception:
                pass
            _scheduler_instance = None
            crud.release_scheduler_lock()

        # 如果 auto_pull 未启用，不启动
        if crud.get_setting("auto_pull", "false") != "true":
            logger.info("auto_pull=false，调度器已停止")
            return

        start_time = crud.get_setting("pull_start_time", "09:00")
        period_mode = crud.get_setting("pull_period_mode", "interval")
        interval_hours = float(crud.get_setting("pull_interval_hours", "24"))

        # 获取跨进程互斥锁
        if not crud.acquire_scheduler_lock(mode="web_server"):
            owner = crud.get_scheduler_owner()
            logger.warning(
                "无法获取调度器锁，已有调度器运行中: PID=%s mode=%s",
                owner["pid"] if owner else "?", owner["mode"] if owner else "?"
            )
            return

        try:
            _scheduler_instance = start_scheduler(
                _config, start_time, period_mode, interval_hours,
                on_once_complete=_on_once_done,
            )
            logger.info("调度器已按新设置重启: %s %s %sh", start_time, period_mode, interval_hours)
        except Exception as e:
            crud.release_scheduler_lock()
            logger.error("调度器重启失败: %s", e)
    finally:
        _scheduler_lock.release()


def _on_once_done():
    """once 模式执行完成后的回调。"""
    from src.database import get_crud
    global _scheduler_instance
    crud = get_crud(_config.database.path)
    crud.set_setting("auto_pull", "false")
    crud.release_scheduler_lock()
    _scheduler_instance = None
    logger.info("Once 任务已完成，调度器已自动停止并释放锁")


def _git_commit_push(commit_msg: str = ""):
    """自动 git add + commit + push。非阻塞，静默失败。"""
    import subprocess
    import os as _os
    try:
        repo_root = Path(__file__).parent
        msg = commit_msg or "auto: export reports"

        env = {**_os.environ, "GIT_SSH_COMMAND": "ssh -o StrictHostKeyChecking=no"}

        r1 = subprocess.run(
            ["git", "add", "-f", "reports/", "README.md"],
            cwd=repo_root, capture_output=True, text=True, timeout=10,
        )
        if r1.returncode != 0:
            logger.warning("git add 失败: %s", r1.stderr.strip())

        r2 = subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=repo_root, env=env, capture_output=True, text=True, timeout=10,
        )
        committed = "nothing to commit" not in (r2.stdout + r2.stderr)
        if not committed:
            logger.info("git commit 无变更，跳过 push")
        elif r2.returncode != 0:
            logger.warning("git commit 失败: %s", r2.stderr.strip())

        if committed:
            r3 = subprocess.run(
                ["git", "push"],
                cwd=repo_root, env=env, capture_output=True, text=True, timeout=30,
            )
            if r3.returncode == 0:
                logger.info("Git push 成功: %s", msg)
            else:
                logger.warning("Git push 失败: %s", r3.stderr.strip())
        else:
            logger.info("Git commit 已是最新，跳过 push")
    except Exception as e:
        logger.warning("Git 自动提交异常: %s", e)


def _refresh_readme():
    """根据 reports/ 目录中的文件自动更新 README.md 中的导出报告表格。"""
    md_files = sorted(REPORTS_DIR.glob("trending-*.md"), reverse=True)
    if not md_files:
        rows = "暂无导出报告。\n"
    else:
        from urllib.parse import quote
        links = [f"[{f.name}](reports/{quote(f.name)})" for f in md_files]
        rows = ""
        for i in range(0, len(links), 4):
            chunk = links[i:i+4]
            while len(chunk) < 4:
                chunk.append("")
            rows += "| " + " | ".join(chunk) + " |\n"

    if not README_PATH.exists():
        return

    lines = README_PATH.read_text(encoding="utf-8").split("\n")
    # 找到 "## 导出报告" 行
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("## 项目报告"):
            start = i
            break
    if start is None:
        return

    # 找到下一个 "##" 行作为结束
    end = None
    for i in range(start + 1, len(lines)):
        if lines[i].strip().startswith("##"):
            end = i
            break

    # 重建：保留 start 之前，插入新内容，保留 end 之后
    prefix = "\n".join(lines[:start + 1]) + "\n\n"
    suffix = "\n" + "\n".join(lines[end:]) if end else ""
    new_content = prefix + "展开 Pull 后可导出为 Markdown，保存到 `reports/` 目录。README 中报告列表自动刷新。\n\n" + rows + suffix
    README_PATH.write_text(new_content.rstrip("\n") + "\n", encoding="utf-8")

def get_session():
    return db.SessionFactory()

# 懒加载 LLM 组件（无 API key 时可用）
def _get_analyzer():
    return LLMAnalyzer(
        api_key=_config.llm.api_key,
        base_url=_config.llm.base_url,
        model=_config.llm.model,
        max_tokens=_config.llm.max_tokens,
        temperature=_config.llm.temperature,
        concurrency=_config.llm.concurrency,
    )

def _get_summarizer():
    return WeeklySummarizer(
        api_key=_config.llm.api_key,
        base_url=_config.llm.base_url,
        model=_config.llm.model,
        max_tokens=_config.llm.max_tokens,
        temperature=_config.llm.temperature,
    )

# ── JWT 认证 ──────────────────────────────────────────────

JWT_SECRET = os.environ.get("JWT_SECRET", "umusic-dev-secret-key-change-in-production")
USER_SERVICE_URL = os.environ.get("USER_SERVICE_URL", "http://localhost:8001")

security = HTTPBearer(auto_error=False)

async def verify_token(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> dict | None:
    """验证 JWT access token。失败返回 None（不强制）。"""
    if not credentials:
        return None
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
        if payload.get("type") != "access":
            return None
        return payload  # {sub, username, email, role, ...}
    except jwt.InvalidTokenError:
        return None

async def require_login(user: dict | None = Depends(verify_token)) -> dict:
    """要求已登录。"""
    if not user:
        raise HTTPException(401, "请先登录")
    return user

async def require_super_admin(user: dict = Depends(require_login)) -> dict:
    """要求超级管理员。"""
    if user.get("role") != "super_admin":
        raise HTTPException(403, "需要超级管理员权限")
    return user


# ── FastAPI 应用 ──────────────────────────────────────────

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    _auto_restore_scheduler()
    yield


app = FastAPI(
    title="GitHub Trending 分析系统",
    description="每日 GitHub Weekly Trending 项目分析与趋势追踪",
    version="2.0.0",
    lifespan=lifespan,
)


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = TEMPLATES_DIR / "index.html"
    if not html_path.exists():
        return HTMLResponse("<h1>模板文件未找到</h1>", status_code=500)
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


# ═══════════════════════════════════════════════════════════
# Trending Pulls
# ═══════════════════════════════════════════════════════════

@app.get("/api/trending-pulls")
async def api_pulls(limit: int = Query(50, description="返回条数"), source: str = Query("", description="过滤来源: trending | search")):
    """Pull 列表，按时间倒序。可选的 source 过滤。"""
    session = get_session()
    try:
        q = session.query(TrendingPull).order_by(TrendingPull.id.desc())
        if source:
            q = q.filter(TrendingPull.source == source)
        pulls = q.limit(limit).all()
        return {
            "pulls": [
                {
                    "id": p.id,
                    "pulled_at": p.pulled_at.isoformat() + "Z" if p.pulled_at else "",
                    "project_count": p.project_count,
                    "source": p.source,
                    "has_summary": p.summary is not None,
                }
                for p in pulls
            ]
        }
    finally:
        session.close()


@app.get("/api/trending-pulls/latest")
async def api_pulls_latest():
    """最新一条 pull 的详情 + 项目列表。"""
    session = get_session()
    try:
        pull = (
            session.query(TrendingPull)
            .order_by(TrendingPull.id.desc())
            .first()
        )
        if not pull:
            return {"error": "暂无数据"}
        return _build_pull_detail(session, pull)
    finally:
        session.close()


@app.get("/api/trending-pulls/{pull_id}")
async def api_pull_detail(pull_id: int):
    """指定 pull 的详情 + 项目列表。"""
    session = get_session()
    try:
        pull = session.query(TrendingPull).filter_by(id=pull_id).first()
        if not pull:
            return {"error": "Pull 不存在"}, 404
        return _build_pull_detail(session, pull)
    finally:
        session.close()


def _build_pull_detail(session, pull: TrendingPull) -> dict:
    """构建 pull 详情（含项目列表、分析状态、近一周次数）。"""
    items = (
        session.query(TrendingPullItem)
        .filter_by(pull_id=pull.id)
        .order_by(TrendingPullItem.rank)
        .all()
    )

    # 收集所有 repo_id
    repo_ids = [item.repo_id for item in items]

    # 批量获取分析状态
    analyses = {
        a.repo_id: a
        for a in session.query(ProjectAnalysis)
        .filter(ProjectAnalysis.repo_id.in_(repo_ids))
        .all()
    }

    # 批量获取跟踪状态
    trackings = {
        t.repo_id: t
        for t in session.query(ConsecutiveTracking)
        .filter(ConsecutiveTracking.repo_id.in_(repo_ids))
        .all()
    }

    today = date.today()

    project_list = []
    for item in items:
        repo = item.repository
        analysis = analyses.get(repo.id)
        tracking = trackings.get(repo.id)

        # 获取近一周出现次数
        from datetime import timedelta
        five_days_ago = today - timedelta(days=6)
        appearance_days = (
            session.query(sa_func.count(sa_func.distinct(
                sa_func.date(TrendingPull.pulled_at)
            )))
            .select_from(TrendingPullItem)
            .join(TrendingPull, TrendingPullItem.pull_id == TrendingPull.id)
            .filter(
                TrendingPullItem.repo_id == repo.id,
                sa_func.date(TrendingPull.pulled_at) >= five_days_ago,
                sa_func.date(TrendingPull.pulled_at) <= today,
            )
            .scalar()
        ) or 0

        project_list.append({
            "repo_id": repo.id,
            "full_name": repo.full_name,
            "url": repo.url,
            "description": repo.description or "",
            "language": repo.language,
            "rank": item.rank,
            "stars": item.stars,
            "stars_week": item.stars_week,
            "forks": item.forks,
            "created_at": item.created_at or "",
            "appearance_days": appearance_days,
            "analysis_status": "done" if analysis else "none",
            "analyzed_at": analysis.analyzed_at.isoformat() + "Z" if analysis and analysis.analyzed_at else None,
            "analysis": {
                "functionality": analysis.functionality,
                "tech_stack": analysis.tech_stack,
                "pain_points": analysis.pain_points,
                "competitors": analysis.competitors,
            } if analysis else None,
        })

    return {
        "id": pull.id,
        "pulled_at": pull.pulled_at.isoformat() + "Z" if pull.pulled_at else "",
        "project_count": pull.project_count,
        "source": pull.source,
        "summary": pull.summary,
        "items": project_list,
    }


@app.delete("/api/trending-pulls/{pull_id}")
async def api_delete_pull(pull_id: int):
    """删除 pull 及其所有 items。"""
    from src.database import get_crud
    crud = get_crud(_config.database.path)
    session = get_session()
    try:
        pull = session.query(TrendingPull).filter_by(id=pull_id).first()
        if not pull:
            return {"status": "error", "message": "Pull 不存在"}, 404
        crud.delete_pull(pull_id)
        logger.info(f"删除 Pull #{pull_id}")
        return {"status": "ok"}
    finally:
        session.close()


@app.post("/api/trending-pulls/{pull_id}/export")
async def api_export_pull(pull_id: int, user: dict = Depends(require_login)):
    """导出 pull 为 Markdown 文件。"""
    session = get_session()
    try:
        pull = session.query(TrendingPull).filter_by(id=pull_id).first()
        if not pull:
            return {"status": "error", "message": "Pull 不存在"}, 404

        detail = _build_pull_detail(session, pull)
        items = detail["items"]
        pulled_date = pull.pulled_at.strftime("%Y-%m-%d") if pull.pulled_at else "unknown"

        lines = [
            "# GitHub Trending 分析报告",
            f"**Pull ID**: {pull.id}",
            f"**抓取时间**: {detail['pulled_at']}",
            f"**项目数**: {len(items)}",
            "",
            "---",
            "",
        ]
        for it in items:
            lines.append(f"## #{it['rank']} {it['full_name']}")
            lines.append("")
            lines.append(f"- **总 Stars**: {it['stars']:,} | **本周 Stars**: +{it['stars_week']:,} | **Forks**: {it['forks']:,} | **语言**: {it['language'] or 'Unknown'}")
            if it.get("description"):
                lines.append(f"- **描述**: {it['description']}")
            lines.append(f"- **GitHub**: [{it['full_name']}]({it['url']})")
            lines.append(f"- **近一周上榜**: {it['appearance_days']} 天")
            lines.append("")
            a = it.get("analysis")
            if a and a.get("functionality"):
                lines.append(f"**💡 功能概述**: {a['functionality']}")
                lines.append("")
                lines.append(f"**⚙️ 技术栈**: {a['tech_stack'] or '无'}")
                lines.append("")
                lines.append(f"**🎯 痛点解决**: {a['pain_points'] or '无'}")
                lines.append("")
                lines.append(f"**🏆 竞品分析**: {a['competitors'] or '无'}")
            else:
                lines.append("*（暂无 LLM 分析数据）*")
            lines.append("")
            lines.append("---")
            lines.append("")

        if pull.summary:
            lines.append("## 📝 趋势总结")
            lines.append("")
            lines.append(pull.summary)
            lines.append("")

        md_content = "\n".join(lines)
        ts = pull.pulled_at.strftime("%Y-%m-%d-%H%M%S") if pull.pulled_at else pulled_date
        filename = f"trending-{ts}.md"
        filepath = REPORTS_DIR / filename
        filepath.write_text(md_content, encoding="utf-8")
        logger.info("导出报告: %s (%s bytes)", filepath, len(md_content))
        try:
            _refresh_readme()
        except Exception as e:
            logger.error("_refresh_readme 失败: %s", e)
        ts_display = pull.pulled_at.strftime("%Y-%m-%d %H:%M") if pull.pulled_at else "unknown"
        uid = user.get("username", "unknown")
        try:
            _git_commit_push(f"report export, from {pull.source} pulled at {ts_display} ({len(items)} projects) — by user:{uid}")
        except Exception as e:
            logger.error("_git_commit_push 异常: %s", e)
        logger.info("导出响应准备返回")

        return {
            "status": "ok",
            "filename": filename,
            "path": str(filepath),
        }
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════
# 分析触发
# ═══════════════════════════════════════════════════════════

@app.post("/api/projects/{repo_id}/analyze")
async def api_analyze_project(repo_id: int, user: dict = Depends(require_login)):
    """对单个项目执行 LLM 分析。"""
    session = get_session()
    try:
        repo = session.query(Repository).filter_by(id=repo_id).first()
        if not repo:
            return {"status": "error", "message": "项目不存在"}, 404

        # 获取该项目最近的 pull item 信息（用于 star/forks）
        latest_item = (
            session.query(TrendingPullItem)
            .filter_by(repo_id=repo_id)
            .order_by(TrendingPullItem.id.desc())
            .first()
        )

        logger.info(f"手动触发分析: {repo.full_name}")

        analyzer = _get_analyzer()
        if not analyzer.client:
            return {"status": "error", "message": "LLM API key 未配置"}

        from src.fetcher import TrendingRepo
        trending_data = TrendingRepo(
            github_id=repo.github_id or 0,
            full_name=repo.full_name,
            url=repo.url,
            description=repo.description or "",
            language=repo.language or "Unknown",
            stars=latest_item.stars if latest_item else 0,
            stars_week=latest_item.stars_week if latest_item else 0,
            forks=latest_item.forks if latest_item else 0,
            rank=0,
        )

        result = await analyzer.analyze_single(trending_data)

        if result is None:
            return {"status": "error", "message": "分析失败"}

        # 保存分析（覆盖旧分析）
        from src.database import get_crud
        crud = get_crud(_config.database.path)
        crud.save_analysis(
            repo_id=repo.id,
            functionality=result.functionality,
            tech_stack=result.tech_stack,
            pain_points=result.pain_points,
            competitors=result.competitors,
            raw_response=result.raw_response,
        )

        return {
            "status": "ok",
            "repo_id": repo.id,
            "analyzed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "analysis": {
                "functionality": result.functionality,
                "tech_stack": result.tech_stack,
                "pain_points": result.pain_points,
                "competitors": result.competitors,
            },
        }
    finally:
        session.close()


@app.post("/api/trending-pulls/{pull_id}/analyze")
async def api_analyze_pull(pull_id: int, user: dict = Depends(require_login)):
    """对 pull 中所有未分析的项目逐一执行 LLM 分析。"""
    session = get_session()
    try:
        pull = session.query(TrendingPull).filter_by(id=pull_id).first()
        if not pull:
            return {"status": "error", "message": "Pull 不存在"}, 404

        items = (
            session.query(TrendingPullItem)
            .filter_by(pull_id=pull_id)
            .order_by(TrendingPullItem.rank)
            .all()
        )

        if not items:
            return {"status": "error", "message": "Pull 中无项目"}

        analyzer = _get_analyzer()
        if not analyzer.client:
            return {"status": "error", "message": "LLM API key 未配置"}

        from src.database import get_crud
        from src.fetcher import TrendingRepo
        crud = get_crud(_config.database.path)

        # 构建 TrendingRepo 列表
        trending_list = []
        for item in items:
            repo = item.repository
            trending_list.append(TrendingRepo(
                github_id=repo.github_id or 0,
                full_name=repo.full_name,
                url=repo.url,
                description=repo.description or "",
                language=repo.language or "Unknown",
                stars=item.stars,
                stars_week=item.stars_week,
                forks=item.forks,
                rank=item.rank,
            ))

        logger.info(f"手动触批量分析: Pull #{pull_id}, {len(trending_list)} 个项目")

        results = await analyzer.analyze_batch(trending_list)

        analyzed = 0
        for trending_repo, analysis in results:
            if analysis is not None:
                repo = (
                    session.query(Repository)
                    .filter_by(full_name=trending_repo.full_name)
                    .first()
                )
                if repo:
                    crud.save_analysis(
                        repo_id=repo.id,
                        functionality=analysis.functionality,
                        tech_stack=analysis.tech_stack,
                        pain_points=analysis.pain_points,
                        competitors=analysis.competitors,
                        raw_response=analysis.raw_response,
                    )
                    analyzed += 1

        return {
            "status": "ok",
            "pull_id": pull_id,
            "total": len(trending_list),
            "analyzed": analyzed,
        }
    finally:
        session.close()


@app.post("/api/trending-pulls/{pull_id}/summarize")
async def api_summarize_pull(pull_id: int):
    """为 pull 生成趋势总结。基于该 pull 中已有分析的项目。"""
    session = get_session()
    try:
        pull = session.query(TrendingPull).filter_by(id=pull_id).first()
        if not pull:
            return {"status": "error", "message": "Pull 不存在"}, 404

        # 获取该 pull 中有分析的项目
        items = (
            session.query(TrendingPullItem)
            .filter_by(pull_id=pull_id)
            .order_by(TrendingPullItem.rank)
            .all()
        )

        repo_ids = [item.repo_id for item in items]
        analyses = {
            a.repo_id: a
            for a in session.query(ProjectAnalysis)
            .filter(ProjectAnalysis.repo_id.in_(repo_ids))
            .all()
        }

        if not analyses:
            return {"status": "error", "message": "该 pull 中暂无已分析的项目，请先执行分析"}

        # 构建分析数据列表传给 summarizer
        analysis_data = []
        for item in items:
            a = analyses.get(item.repo_id)
            if a:
                analysis_data.append({
                    "full_name": item.repository.full_name,
                    "language": item.repository.language,
                    "functionality": a.functionality,
                    "tech_stack": a.tech_stack,
                    "pain_points": a.pain_points,
                    "competitors": a.competitors,
                })

        logger.info(f"手动触发总结: Pull #{pull_id}, {len(analysis_data)} 个已分析项目")

        summarizer = _get_summarizer()
        if not summarizer.client:
            return {"status": "error", "message": "LLM API key 未配置"}

        summary_text = await summarizer.generate_summary(analysis_data)

        if not summary_text:
            return {"status": "error", "message": "总结生成失败"}

        # 保存总结到 pull
        from src.database import get_crud
        crud = get_crud(_config.database.path)
        crud.update_pull_summary(pull_id, summary_text)

        return {
            "status": "ok",
            "pull_id": pull_id,
            "summary": summary_text,
        }
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════
# 项目详情
# ═══════════════════════════════════════════════════════════

@app.get("/api/projects")
async def api_projects(search: str = Query("", description="搜索关键词")):
    """项目列表，支持搜索。"""
    session = get_session()
    try:
        q = session.query(Repository)
        if search:
            q = q.filter(Repository.full_name.ilike(f"%{search}%"))
        repos = q.order_by(Repository.full_name).limit(50).all()
        return {
            "projects": [
                {
                    "id": r.id,
                    "full_name": r.full_name,
                    "url": r.url,
                    "description": (r.description or "")[:150],
                    "language": r.language,
                }
                for r in repos
            ]
        }
    finally:
        session.close()


@app.get("/api/project/{repo_id}")
async def api_project_detail(repo_id: int):
    """项目详情 + 最新分析 + 跟踪状态。"""
    session = get_session()
    try:
        repo = session.query(Repository).filter_by(id=repo_id).first()
        if not repo:
            return {"error": "项目不存在"}, 404

        analysis = session.query(ProjectAnalysis).filter_by(repo_id=repo_id).first()
        tracking = session.query(ConsecutiveTracking).filter_by(repo_id=repo_id).first()

        # 获取最新的 stars 和 forks
        latest_item = (
            session.query(TrendingPullItem)
            .filter_by(repo_id=repo_id)
            .order_by(TrendingPullItem.id.desc())
            .first()
        )

        return {
            "id": repo.id,
            "full_name": repo.full_name,
            "url": repo.url,
            "description": repo.description or "",
            "language": repo.language,
            "stars": latest_item.stars if latest_item else 0,
            "stars_week": latest_item.stars_week if latest_item else 0,
            "forks": latest_item.forks if latest_item else 0,
            "analysis": {
                "analyzed_at": analysis.analyzed_at.isoformat() + "Z" if analysis and analysis.analyzed_at else None,
                "functionality": analysis.functionality if analysis else None,
                "tech_stack": analysis.tech_stack if analysis else None,
                "pain_points": analysis.pain_points if analysis else None,
                "competitors": analysis.competitors if analysis else None,
                "has_analysis": analysis is not None,
            },
            "tracking": {
                "first_seen": tracking.first_seen.isoformat() if tracking and tracking.first_seen else "",
                "last_seen": tracking.last_seen.isoformat() if tracking and tracking.last_seen else "",
                "appearance_days": tracking.appearance_days if tracking else 0,
                "is_active": tracking.is_active if tracking else False,
            } if tracking else None,
        }
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════
# 重点关注 & 报告导出
# ═══════════════════════════════════════════════════════════

@app.get("/api/highlights")
async def api_highlights():
    """当前重点关注项目。"""
    session = get_session()
    try:
        rows = (
            session.query(Repository, ConsecutiveTracking)
            .join(ConsecutiveTracking, ConsecutiveTracking.repo_id == Repository.id)
            .filter(
                ConsecutiveTracking.is_active == True,
                ConsecutiveTracking.appearance_days >= 2,
            )
            .order_by(ConsecutiveTracking.appearance_days.desc())
            .all()
        )
        return {
            "highlights": [
                {
                    "id": repo.id,
                    "full_name": repo.full_name,
                    "url": repo.url,
                    "description": (repo.description or "")[:150],
                    "language": repo.language,
                    "first_seen": track.first_seen.isoformat() if track.first_seen else "",
                    "last_seen": track.last_seen.isoformat() if track.last_seen else "",
                    "appearance_days": track.appearance_days,
                    "is_active": track.is_active,
                }
                for repo, track in rows
            ]
        }
    finally:
        session.close()


@app.get("/api/reports")
async def api_reports():
    """已导出报告列表。"""
    session = get_session()
    reports = []
    try:
        for f in sorted(REPORTS_DIR.glob("trending-*.md"), reverse=True):
            stat = f.stat()
            # 尝试匹配对应的 trending pull
            pull_time = ""
            filename = f.name
            # 从文件名提取日期 (trending-YYYY-MM-DD*.md)
            date_match = filename.replace("trending-", "").replace(".md", "")
            try:
                # 尝试匹配同一天的 pull
                from datetime import datetime as dt
                pull_date = dt.strptime(date_match[:10], "%Y-%m-%d").date()
                pull = (
                    session.query(TrendingPull)
                    .filter(sa_func.date(TrendingPull.pulled_at) == pull_date)
                    .order_by(TrendingPull.id.desc())
                    .first()
                )
                if pull and pull.pulled_at:
                    pull_time = pull.pulled_at.isoformat() + "Z"
            except Exception:
                pass

            reports.append({
                "filename": filename,
                "size": stat.st_size,
                "date": date_match,
                "mtime": dt.utcfromtimestamp(stat.st_mtime).isoformat() + "Z",
                "pull_time": pull_time,
            })
    finally:
        session.close()
    return {"reports": reports}


@app.get("/api/reports/{filename}")
async def api_view_report(filename: str):
    """查看导出报告的内容。"""
    filepath = REPORTS_DIR / filename
    if not filepath.exists():
        return {"status": "error", "message": "文件不存在"}, 404
    return {
        "status": "ok",
        "filename": filename,
        "content": filepath.read_text(encoding="utf-8"),
    }


@app.delete("/api/reports/{filename}")
async def api_delete_report(filename: str):
    """删除导出报告文件。"""
    filepath = REPORTS_DIR / filename
    if not filepath.exists():
        return {"status": "error", "message": "文件不存在"}, 404
    filepath.unlink()
    logger.info(f"删除报告: {filename}")
    _refresh_readme()

    # 尝试匹配 pull 信息
    ts_match = filename.replace("trending-", "").replace(".md", "")
    try:
        from datetime import datetime as dt
        pull_time = dt.strptime(ts_match[:19], "%Y-%m-%d-%H%M%S")
        ts_display = pull_time.strftime("%Y-%m-%d %H:%M")
        s = get_session()
        try:
            pull = s.query(TrendingPull).filter(TrendingPull.pulled_at == pull_time).first()
            if pull:
                src = pull.source
                cnt = pull.project_count
            else:
                src = "unknown"
                cnt = 0
        finally:
            s.close()
        _git_commit_push(f"report removed, from {src} pulled at {ts_display} ({cnt} projects)")
    except ValueError:
        _git_commit_push(f"report removed: {filename}")

    return {"status": "ok"}


@app.post("/api/export/{snapshot_date}")
async def api_export(snapshot_date: str, user: dict = Depends(require_login)):
    """导出某个 pull 的数据为 Markdown 报告。"""
    session = get_session()
    try:
        # 找到该日期的 pull
        pull = (
            session.query(TrendingPull)
            .filter(sa_func.date(TrendingPull.pulled_at) == snapshot_date)
            .order_by(TrendingPull.id.desc())
            .first()
        )
        if not pull:
            # 尝试最新 pull
            pull = session.query(TrendingPull).order_by(TrendingPull.id.desc()).first()
            if not pull:
                return {"status": "error", "message": "无可用数据"}

        detail = _build_pull_detail(session, pull)
        items = detail["items"]

        lines = [
            "# GitHub Daily Trending 分析报告",
            f"**Pull ID**: {pull.id}",
            f"**抓取时间**: {detail['pulled_at']}",
            f"**项目数**: {len(items)}",
            "",
            "---",
            "",
        ]
        for it in items:
            lines.append(f"## #{it['rank']} {it['full_name']}")
            lines.append("")
            lines.append(f"- **Stars**: {it['stars']:,} | **Forks**: {it['forks']:,} | **语言**: {it['language'] or 'Unknown'}")
            if it.get("description"):
                lines.append(f"- **描述**: {it['description']}")
            lines.append(f"- **GitHub**: [{it['full_name']}]({it['url']})")
            lines.append(f"- **近一周上榜**: {it['appearance_days']} 天")
            lines.append("")

            a = it.get("analysis")
            if a and a.get("functionality"):
                lines.append(f"**💡 功能概述**: {a['functionality']}")
                lines.append("")
                lines.append(f"**⚙️ 技术栈**: {a['tech_stack'] or '无'}")
                lines.append("")
                lines.append(f"**🎯 痛点解决**: {a['pain_points'] or '无'}")
                lines.append("")
                lines.append(f"**🏆 竞品分析**: {a['competitors'] or '无'}")
            else:
                lines.append("*（暂无 LLM 分析数据）*")
            lines.append("")
            lines.append("---")
            lines.append("")

        md_content = "\n".join(lines)

        ts = pull.pulled_at.strftime("%Y-%m-%d-%H%M%S") if pull.pulled_at else snapshot_date
        filename = f"trending-{ts}.md"
        filepath = REPORTS_DIR / filename
        filepath.write_text(md_content, encoding="utf-8")

        logger.info("导出报告: %s (%s bytes)", filepath, len(md_content))
        try:
            _refresh_readme()
        except Exception as e:
            logger.error("_refresh_readme 失败: %s", e)
        ts = pull.pulled_at.strftime("%Y-%m-%d %H:%M") if pull.pulled_at else snapshot_date
        uid = user.get("username", "unknown")
        try:
            _git_commit_push(f"report export, from {pull.source} pulled at {ts} ({len(items)} projects) — by user:{uid}")
        except Exception as e:
            logger.error("_git_commit_push 异常: %s", e)
        logger.info("导出响应准备返回")

        return {
            "status": "ok",
            "filename": filename,
            "path": str(filepath),
            "content": md_content,
        }
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════
# 触发 API
# ═══════════════════════════════════════════════════════════

@app.post("/api/trigger/run-once")
async def trigger_run_once(force: bool = False, user: dict = Depends(require_login)):
    global _config
    # 读取最新设置
    from src.database import get_crud
    crud = get_crud(_config.database.path)
    _config.github.per_page = int(crud.get_setting("per_page", "20"))
    auto_analyze = crud.get_setting("auto_analyze", "false") == "true"

    logger.info(f"Web 触发: 抓取 (force={force}, per_page={_config.github.per_page}, auto_analyze={auto_analyze})")
    try:
        result = await main_run_once(_config, force=force)
        resp = {"status": "ok", "result": result}

        # 自动分析
        if auto_analyze and result.get("pull_id"):
            pull_id = result["pull_id"]
            logger.info(f"自动触发批量分析: Pull #{pull_id}")
            try:
                # 在后台触发分析
                import asyncio as asyncio_mod
                asyncio_mod.create_task(_auto_analyze_pull(pull_id))
                resp["auto_analyze"] = "started"
            except Exception as e:
                logger.error(f"自动分析失败: {e}")

        return resp
    except Exception as e:
        logger.error(f"抓取失败: {e}")
        return {"status": "error", "message": str(e)}


async def _auto_analyze_pull(pull_id: int):
    """后台执行批量分析。"""
    # 等待 pull 数据写入完成
    import asyncio as asyncio_mod
    await asyncio_mod.sleep(1)

    session = get_session()
    try:
        pull = session.query(TrendingPull).filter_by(id=pull_id).first()
        if not pull:
            return

        items = (
            session.query(TrendingPullItem)
            .filter_by(pull_id=pull_id)
            .order_by(TrendingPullItem.rank)
            .all()
        )

        analyzer = _get_analyzer()
        if not analyzer.client:
            logger.warning("自动分析跳过: LLM API key 未配置")
            return

        from src.database import get_crud
        from src.fetcher import TrendingRepo
        crud = get_crud(_config.database.path)

        trending_list = []
        for item in items:
            repo = item.repository
            trending_list.append(TrendingRepo(
                github_id=repo.github_id or 0,
                full_name=repo.full_name,
                url=repo.url,
                description=repo.description or "",
                language=repo.language or "Unknown",
                stars=item.stars,
                stars_week=item.stars_week,
                forks=item.forks,
                rank=item.rank,
            ))

        logger.info(f"自动分析 Pull #{pull_id}: {len(trending_list)} 个项目")
        results = await analyzer.analyze_batch(trending_list)

        analyzed = 0
        for trending_repo, analysis in results:
            if analysis is not None:
                repo = (
                    session.query(Repository)
                    .filter_by(full_name=trending_repo.full_name)
                    .first()
                )
                if repo:
                    crud.save_analysis(
                        repo_id=repo.id,
                        functionality=analysis.functionality,
                        tech_stack=analysis.tech_stack,
                        pain_points=analysis.pain_points,
                        competitors=analysis.competitors,
                        raw_response=analysis.raw_response,
                    )
                    analyzed += 1

        logger.info(f"自动分析完成 Pull #{pull_id}: {analyzed}/{len(trending_list)}")
    finally:
        session.close()


@app.get("/api/scheduler/status")
async def api_scheduler_status():
    """调度器运行状态（含跨进程锁持有者信息）。"""
    global _scheduler_instance
    from src.database import get_crud
    crud = get_crud(_config.database.path)
    owner = crud.get_scheduler_owner()
    return {
        "running": _scheduler_instance is not None,
        "auto_pull": crud.get_setting("auto_pull", "false") == "true",
        "start_time": crud.get_setting("pull_start_time", "09:00"),
        "period_mode": crud.get_setting("pull_period_mode", "interval"),
        "interval_hours": crud.get_setting("pull_interval_hours", "24"),
        "lock_owner": owner,  # None 表示无锁，或 {pid, started_at, mode, is_alive}
    }


@app.post("/api/trigger/daemon/start")
async def trigger_daemon_start(user: dict = Depends(require_super_admin)):
    """手动启动调度器守护进程（仅超级管理员）。"""
    global _scheduler_instance
    from src.database import get_crud
    crud = get_crud(_config.database.path)

    if _scheduler_instance is not None:
        return {"status": "ok", "message": "调度器已在运行"}

    start_time = crud.get_setting("pull_start_time", "09:00")
    period_mode = crud.get_setting("pull_period_mode", "interval")
    interval_hours = float(crud.get_setting("pull_interval_hours", "24"))

    with _scheduler_lock:
        if _scheduler_instance is not None:  # double-check
            return {"status": "ok", "message": "调度器已在运行"}

        # 获取跨进程互斥锁
        if not crud.acquire_scheduler_lock(mode="web_server"):
            owner = crud.get_scheduler_owner()
            return {
                "status": "error",
                "message": f"已有调度器运行中: PID={owner['pid']}, mode={owner['mode']}" if owner else "无法获取调度器锁"
            }

        crud.set_setting("auto_pull", "true")
        try:
            _scheduler_instance = start_scheduler(
                _config, start_time, period_mode, interval_hours,
                on_once_complete=_on_once_done,
            )
            logger.info("管理员手动启动调度器: %s %s %sh", start_time, period_mode, interval_hours)
            return {"status": "ok", "message": "调度器已启动"}
        except Exception as e:
            crud.release_scheduler_lock()
            logger.error("手动启动调度器失败: %s", e)
            return {"status": "error", "message": str(e)}


@app.post("/api/trigger/daemon/stop")
async def trigger_daemon_stop(user: dict = Depends(require_super_admin)):
    """手动停止调度器守护进程（仅超级管理员）。"""
    global _scheduler_instance
    from src.database import get_crud
    crud = get_crud(_config.database.path)

    with _scheduler_lock:
        if _scheduler_instance is None:
            return {"status": "ok", "message": "调度器未在运行"}
        try:
            _scheduler_instance.shutdown(wait=True)
        except Exception:
            pass
        _scheduler_instance = None
        crud.set_setting("auto_pull", "false")
        crud.release_scheduler_lock()
        logger.info("管理员手动停止调度器并释放锁")
        return {"status": "ok", "message": "调度器已停止"}


@app.get("/api/trigger/daemon/status")
async def trigger_daemon_status():
    """守护进程运行状态（别名，无需登录）。"""
    global _scheduler_instance
    from src.database import get_crud
    crud = get_crud(_config.database.path)
    return {
        "running": _scheduler_instance is not None,
        "auto_pull": crud.get_setting("auto_pull", "false") == "true",
        "start_time": crud.get_setting("pull_start_time", "09:00"),
        "period_mode": crud.get_setting("pull_period_mode", "interval"),
        "interval_hours": crud.get_setting("pull_interval_hours", "24"),
    }


# ═══════════════════════════════════════════════════════════
# 设置 API
# ═══════════════════════════════════════════════════════════

@app.get("/api/settings")
async def api_get_settings(user: dict = Depends(require_super_admin)):
    """获取所有设置。"""
    from src.database import get_crud
    crud = get_crud(_config.database.path)
    return crud.get_all_settings()


@app.post("/api/settings")
async def api_save_settings(data: dict, user: dict = Depends(require_super_admin)):
    """批量保存设置。"""
    from src.database import get_crud
    crud = get_crud(_config.database.path)
    allowed = {"per_page", "auto_analyze", "auto_pull",
               "pull_start_time", "pull_period_mode", "pull_interval_hours",
               "github_token"}
    restart_keys = {"auto_pull", "pull_start_time", "pull_period_mode", "pull_interval_hours"}
    need_restart = False

    for k, v in data.items():
        if k in allowed:
            if isinstance(v, bool):
                v = "true" if v else "false"
            else:
                v = str(v)
            crud.set_setting(k, v)
            if k in restart_keys:
                need_restart = True
            if k == "github_token":
                logger.info("设置已更新: %s = ***", k)
            else:
                logger.info("设置已更新: %s = %s", k, v)

    _config.github.per_page = int(crud.get_setting("per_page", "20"))
    if need_restart:
        _restart_scheduler()

    return {"status": "ok"}


@app.post("/api/maintenance/dedup-pulls")
async def api_dedup_pulls(user: dict = Depends(require_super_admin)):
    """清理历史重复 pull（仅超级管理员）。

    同一天若有多条 pull，保留最新一条，删除其余。
    返回删除数量。
    """
    from src.database import get_crud
    crud = get_crud(_config.database.path)
    removed = crud.dedup_all_pulls()
    return {"status": "ok", "removed": removed}


# ═══════════════════════════════════════════════════════════
# Search API
# ═══════════════════════════════════════════════════════════

@app.post("/api/search")
async def api_search(data: dict):
    """通过 GitHub Search API 搜索仓库，结果保存为 pull 记录。"""
    from src.fetcher import GitHubFetcher
    from src.database import get_crud
    crud = get_crud(_config.database.path)

    token = data.get("token", "") or crud.get_setting("github_token", "")
    per_page = int(data.get("per_page", 20))
    params = {
        "period": int(data.get("search_period_days", 7)),
        "forks_min": int(data.get("search_min_forks", 3)),
        "sort": data.get("search_sort", "stars"),
        "order": data.get("search_order", "desc"),
        "per_page": per_page,
    }
    keyword = data.get("keyword", "").strip()
    logger.info("Search 请求: keyword=%s params=%s", keyword, params)
    fetcher = GitHubFetcher(
        token=token,
        per_page=per_page,
        search_period_days=params["period"],
        search_sort=params["sort"],
        search_order=params["order"],
        search_min_forks=params["forks_min"],
        search_keyword=keyword,
    )

    try:
        repos = await fetcher.fetch_via_search_api()
    except Exception as e:
        return {"status": "error", "message": str(e)}

    if not repos:
        logger.info("Search 完成: 0 个结果")
        return {"status": "ok", "count": 0, "pull_id": None, "repos": []}

    logger.info("Search 完成: %s 个结果", len(repos))

    # 保存为 pull 记录
    session = get_session()
    try:
        pull = crud.create_pull(source="search")
        saved = 0
        for repo_data in repos:
            try:
                repo = crud.upsert_repository(
                    github_id=repo_data.github_id if repo_data.github_id != 0 else None,
                    full_name=repo_data.full_name,
                    url=repo_data.url,
                    description=repo_data.description,
                    language=repo_data.language,
                )
                crud.add_pull_item(
                    pull_id=pull.id,
                    repo_id=repo.id,
                    rank=repo_data.rank,
                    stars=repo_data.stars,
                    stars_week=0,
                    forks=repo_data.forks,
                    created_at=repo_data.created_at or None,
                )
                saved += 1
            except Exception as e:
                logger.error(f"保存 {repo_data.full_name} 失败: {e}")

        crud.update_pull_count(pull.id, saved)

        # 更新跟踪
        from src.tracker import ConsecutiveTracker
        tracker = ConsecutiveTracker(crud)
        repo_objs = (
            session.query(Repository)
            .filter(Repository.id.in_(
                session.query(TrendingPullItem.repo_id).filter_by(pull_id=pull.id)
            ))
            .all()
        )
        tracker.update(date.today(), repo_objs)

        return {
            "status": "ok",
            "count": saved,
            "pull_id": pull.id,
            "repos": [
                {
                    "rank": r.rank,
                    "full_name": r.full_name,
                    "url": r.url,
                    "description": r.description,
                    "language": r.language,
                    "stars": r.stars,
                    "forks": r.forks,
                    "created_at": r.created_at,
                }
                for r in repos[:saved]
            ],
        }
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════
# Auth 代理
# ═══════════════════════════════════════════════════════════

from fastapi import Request as FastAPIRequest
from fastapi.responses import JSONResponse


async def _proxy_auth(path: str, data: dict, req: FastAPIRequest | None = None):
    """通用代理到 user-service，转发 httpOnly cookie。"""
    headers = {}
    # 转发浏览器 cookie 到 user-service
    if req and "cookie" in req.headers:
        headers["Cookie"] = req.headers["cookie"]

    async with httpx.AsyncClient() as client:
        r = await client.post(f"{USER_SERVICE_URL}{path}", json=data, headers=headers)
        try:
            body = r.json()
        except Exception:
            body = {"detail": r.text}

        logger.info("Proxy %s: status=%s", path, r.status_code)
        if r.status_code >= 400:
            raise HTTPException(r.status_code, detail=body.get("detail", str(body)))

        # 将 refresh_token 从 body 中提取并设置为 httpOnly cookie
        rt = body.pop("refresh_token", None)
        resp = JSONResponse(content=body)
        if rt:
            resp.set_cookie(
                key="refresh_token",
                value=rt,
                httponly=True,
                secure=False,       # 开发环境用 http，生产改为 True
                samesite="lax",
                max_age=7 * 24 * 3600,  # 7天
                path="/api/auth",
            )
        return resp


@app.post("/api/auth/login")
async def proxy_login(data: dict, req: FastAPIRequest = None):
    return await _proxy_auth("/api/auth/login", data, req)


@app.post("/api/auth/register")
async def proxy_register(data: dict, req: FastAPIRequest = None):
    return await _proxy_auth("/api/auth/register", data, req)


@app.post("/api/auth/refresh")
async def proxy_refresh(data: dict, req: FastAPIRequest = None):
    # 优先从 cookie 读取 refresh_token，fallback 到 body
    if not data.get("refresh_token"):
        cookie_rt = req.cookies.get("refresh_token") if req else None
        if cookie_rt:
            data = {**data, "refresh_token": cookie_rt}
    return await _proxy_auth("/api/auth/refresh", data, req)


@app.post("/api/auth/verify-email")
async def proxy_verify_email(data: dict, req: FastAPIRequest = None):
    return await _proxy_auth("/api/auth/verify-email", data, req)


@app.post("/api/auth/resend-code")
async def proxy_resend_code(data: dict):
    return await _proxy_auth("/api/auth/resend-code", data)


@app.get("/api/auth/me")
async def api_me(user: dict | None = Depends(verify_token)):
    """返回当前登录用户信息。"""
    if not user:
        return {"user": None}
    return {"user": {"id": user["sub"], "username": user["username"], "email": user["email"], "role": user["role"]}}


# ═══════════════════════════════════════════════════════════
# 启动入口
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="GitHub Trending Web 展示服务")
    parser.add_argument("--port", type=int, default=8080, help="监听端口 (默认 8080)")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    args = parser.parse_args()

    import uvicorn

    print(f"\n  GitHub Trending 分析系统 — Web 展示服务 v2.0")
    print(f"  访问地址: http://localhost:{args.port}")
    print(f"  API 文档: http://localhost:{args.port}/docs")
    print(f"  按 Ctrl+C 退出\n")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
