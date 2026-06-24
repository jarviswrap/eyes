"""GitHub Weekly Trending 每日分析系统 — 入口模块。

用法:
    python main.py                  # 启动调度器守护进程
    python main.py --run-once       # 立即执行一次完整流程
    python main.py --dry-run        # 干运行（仅抓取，不调用 LLM）
    python main.py --config PATH    # 指定配置文件路径

本模块的核心函数可被 web_server.py 等外部模块导入复用。
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# 将项目根目录加入 sys.path，支持直接运行 main.py
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))

# 加载 logutil 日志模块
sys.path.insert(0, str(Path(__file__).parent.parent / "deploy"))
from logutil import setup_logging, get_logger

from src.config import load_config, AppConfig
from src.scheduler import create_scheduler, TrendingJob


async def dry_run(config: AppConfig):
    """干运行模式：仅抓取数据并打印，不调用 LLM 也不写数据库。"""
    from src.fetcher import GitHubFetcher

    logger = logging.getLogger("dry-run")
    logger.info("=== 干运行模式：仅抓取 GitHub Trending 数据 ===")

    fetcher = GitHubFetcher(
        token=config.github.token,
        per_page=config.github.per_page,
        request_delay=config.github.request_delay,
    )

    repos = await fetcher.fetch_trending()

    if not repos:
        logger.warning("未获取到任何数据")
        return

    logger.info(f"\n获取到 {len(repos)} 个 trending 项目:\n")
    for repo in repos:
        logger.info(
            f"  #{repo.rank:2d} | stars:{repo.stars:6,} | forks:{repo.forks:4,} | "
            f"[{repo.language:15s}] | {repo.full_name}"
        )
        if repo.description:
            logger.info(f"       {repo.description[:120]}")
        logger.info("")

    logger.info("=== 干运行完成 ===")


async def run_once(config: AppConfig, force: bool = False):
    """立即执行一次完整流程。

    Args:
        config: 应用配置
        force: True 时强制重新抓取，忽略当日已有数据
    """
    logger = logging.getLogger("run-once")
    logger.info(f"=== 单次运行模式 {'(强制)' if force else ''} ===")

    job = TrendingJob(config)
    result = await job.run_once(force=force)

    logger.info(f"\n执行结果: {result}")
    logger.info("=== 单次运行完成 ===")
    return result


def run_daemon(config: AppConfig):
    """以守护进程模式运行。"""
    logger = logging.getLogger("daemon")

    # ── 数据库层面互斥：检查 auto_pull 并获取跨进程锁 ──
    from src.database import get_crud
    crud = get_crud(config.database.path)

    if crud.get_setting("auto_pull", "false") != "true":
        logger.info("auto_pull=false，守护进程不启动调度器")
        return

    if not crud.acquire_scheduler_lock(mode="daemon"):
        owner = crud.get_scheduler_owner()
        logger.error(
            "无法获取调度器锁，已有调度器在运行: PID=%s mode=%s",
            owner["pid"] if owner else "?", owner["mode"] if owner else "?"
        )
        return

    logger.info("=== 守护进程模式 ===")
    logger.info(f"计划任务: 每天 {config.scheduler.run_time} ({config.scheduler.timezone})")
    logger.info("按 Ctrl+C 退出")

    scheduler, job = create_scheduler(config)

    try:
        scheduler.start()
        logger.info("调度器已启动，等待任务触发...")
        # 保持主线程运行
        asyncio.get_event_loop().run_forever()
    except (KeyboardInterrupt, SystemExit):
        logger.info("收到退出信号，正在关闭...")
        scheduler.shutdown(wait=False)
        logger.info("调度器已关闭")
    finally:
        crud.release_scheduler_lock()


def start_scheduler(config: AppConfig, start_time: str = "09:00",
                    period_mode: str = "interval", interval_hours: int = 24,
                    on_once_complete: callable = None):
    """启动调度器并返回 scheduler 对象（非阻塞）。

    period_mode: "once" | "interval"
    on_once_complete: once 模式执行完成后的回调（用于停调度器）
    """
    from src.scheduler import create_scheduler_with_settings
    scheduler, job = create_scheduler_with_settings(
        config, start_time, period_mode, interval_hours, on_once_complete
    )
    scheduler.start()
    logger = logging.getLogger("main")
    logger.info("调度器已启动: mode=%s", period_mode)
    return scheduler


def main():
    parser = argparse.ArgumentParser(
        description="GitHub Weekly Trending 每日分析系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py                      # 启动守护进程
  python main.py --run-once           # 立即执行一次
  python main.py --dry-run            # 干运行（仅抓取）
  python main.py --config myconf.yaml # 指定配置文件
        """,
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="配置文件路径 (默认: config.yaml)",
    )
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="立即执行一次完整流程后退出",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="干运行：仅抓取 trending 数据，不调用 LLM 也不写数据库",
    )
    args = parser.parse_args()

    # 加载配置
    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        sys.stderr.write(f"❌ {e}\n请确保配置文件存在，或使用 --config 指定路径\n")
        sys.exit(1)
    except Exception as e:
        sys.stderr.write(f"❌ 配置加载失败: {e}\n")
        sys.exit(1)

    # 配置日志
    log_dir = str(Path(__file__).parent.parent / "deploy" / "logs")
    setup_logging("eyes", log_dir=log_dir)
    logger = get_logger("main")

    logger.info("GitHub Weekly Trending 每日分析系统 启动")
    logger.info("LLM: %s | 模型: %s", config.llm.provider, config.llm.model)
    logger.info("数据库: %s", config.database.path)

    # 根据参数选择运行模式
    if args.dry_run:
        asyncio.run(dry_run(config))
    elif args.run_once:
        asyncio.run(run_once(config))
    else:
        run_daemon(config)


if __name__ == "__main__":
    main()
