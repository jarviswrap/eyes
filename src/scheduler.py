"""定时任务编排模块。

每日抓取 GitHub Weekly Trending，产生一条 trending pull 记录。
LLM 分析和周总结改为前端手动触发。
"""

import asyncio
import logging
from datetime import date

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import AppConfig
from .database import CRUD, Database, get_crud, get_database
from .fetcher import GitHubFetcher
from .tracker import ConsecutiveTracker

logger = logging.getLogger(__name__)


class TrendingJob:
    """每日 Trending 抓取任务。"""

    def __init__(self, config: AppConfig):
        self.config = config

        self.db: Database = get_database(config.database.path)
        self.crud: CRUD = get_crud(config.database.path)
        self.fetcher = GitHubFetcher(
            token=config.github.token,
            per_page=config.github.per_page,
            request_delay=config.github.request_delay,
        )
        self.tracker = ConsecutiveTracker(self.crud)

    async def run_once(self, force: bool = False) -> dict:
        """执行一次完整的每日流程。

        步骤:
        1. 抓取 GitHub Trending 数据
        2. 创建 trending_pull + 保存项目 + 写入 pull_items
        3. 更新近一周上榜跟踪

        LLM 分析和周总结不在此处触发，由前端手动触发。
        """
        today = date.today()
        result = {
            "date": today.isoformat(),
            "pull_id": None,
            "fetched": 0,
            "highlights": 0,
            "errors": [],
        }

        logger.info(f"{'='*60}")
        logger.info(f"开始执行每日 Trending 抓取: {today}")
        logger.info(f"{'='*60}")

        # ── 步骤 1: 抓取 Trending 数据 ──
        logger.info("步骤 1/3: 抓取 GitHub Weekly Trending 数据...")
        try:
            trending_repos = await self.fetcher.fetch_trending()
        except Exception as e:
            logger.error(f"抓取失败: {e}")
            result["errors"].append(f"fetch: {e}")
            return result

        if not trending_repos:
            logger.warning("未获取到任何 trending 数据，本次执行终止")
            return result

        result["fetched"] = len(trending_repos)
        logger.info(f"获取到 {len(trending_repos)} 个 trending 项目")

        # ── 步骤 2: 创建 pull + 保存项目 ──
        logger.info("步骤 2/3: 保存数据...")

        # 创建 trending pull
        pull = self.crud.create_pull()
        logger.info(f"创建 Trending Pull #{pull.id}")

        saved_repos = []
        for repo_data in trending_repos:
            try:
                repo = self.crud.upsert_repository(
                    github_id=repo_data.github_id if repo_data.github_id != 0 else None,
                    full_name=repo_data.full_name,
                    url=repo_data.url,
                    description=repo_data.description,
                    language=repo_data.language,
                )
                self.crud.add_pull_item(
                    pull_id=pull.id,
                    repo_id=repo.id,
                    rank=repo_data.rank,
                    stars=repo_data.stars,
                    stars_week=repo_data.stars_week,
                    forks=repo_data.forks,
                )
                saved_repos.append(repo)
            except Exception as e:
                logger.error(f"保存仓库 {repo_data.full_name} 失败: {e}")
                result["errors"].append(f"save:{repo_data.full_name}: {e}")

        self.crud.update_pull_count(pull.id, len(saved_repos))
        result["pull_id"] = pull.id

        if not saved_repos:
            logger.warning("没有成功保存任何仓库数据")
            return result

        # ── 步骤 3: 更新近一周上榜跟踪 ──
        logger.info("步骤 3/3: 更新近一周上榜跟踪...")
        try:
            self.tracker.update(today, saved_repos)
            highlights = self.tracker.get_highlights()
            result["highlights"] = len(highlights)
            if highlights:
                logger.info(f"当前有 {len(highlights)} 个项目需要重点关注")
                for h in highlights:
                    logger.info(f"  [HIGHLIGHT] {h['full_name']}: 近一周上榜 {h['appearance_days']} 天")
        except Exception as e:
            logger.error(f"跟踪更新失败: {e}")
            result["errors"].append(f"tracker: {e}")

        logger.info(f"每日任务完成: {result}")
        logger.info(f"{'='*60}")
        return result


def create_scheduler(config: AppConfig) -> tuple[AsyncIOScheduler, TrendingJob]:
    """创建并配置定时调度器。"""
    job = TrendingJob(config)

    scheduler = AsyncIOScheduler(timezone=config.scheduler.timezone)
    hour, minute = config.scheduler.run_time.split(":")
    trigger = CronTrigger(hour=int(hour), minute=int(minute))

    scheduler.add_job(
        job.run_once,
        trigger=trigger,
        id="daily_trending_pull",
        name="GitHub Trending 每日抓取",
        replace_existing=True,
    )

    logger.info(
        f"定时任务已配置: 每天 {config.scheduler.run_time} "
        f"({config.scheduler.timezone})"
    )

    return scheduler, job
