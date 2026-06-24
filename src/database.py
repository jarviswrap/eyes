"""数据库模块 — SQLAlchemy 模型定义与 CRUD 操作。"""

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    Date,
    DateTime,
    Boolean,
    ForeignKey,
    UniqueConstraint,
    create_engine,
    func as sa_func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    relationship,
    sessionmaker,
    joinedload,
)

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


# ═══════════════════════════════════════════════════════════════
# 模型定义
# ═══════════════════════════════════════════════════════════════

class Repository(Base):
    """GitHub 仓库基础信息。full_name 唯一。"""

    __tablename__ = "repositories"

    id: Mapped[int] = Column(Integer, primary_key=True, autoincrement=True)
    github_id: Mapped[Optional[int]] = Column(Integer, unique=True, nullable=True, index=True)
    full_name: Mapped[str] = Column(String(255), unique=True, nullable=False)
    url: Mapped[str] = Column(String(500), nullable=False)
    description: Mapped[Optional[str]] = Column(Text, nullable=True)
    language: Mapped[Optional[str]] = Column(String(100), nullable=True)

    # 关联
    pull_items = relationship("TrendingPullItem", back_populates="repository")
    analysis = relationship("ProjectAnalysis", back_populates="repository", uselist=False)
    tracking = relationship("ConsecutiveTracking", back_populates="repository", uselist=False)

    def __repr__(self):
        return f"<Repository(id={self.id}, full_name='{self.full_name}')>"


class TrendingPull(Base):
    """每次抓取产生一条 trending pull 记录。"""

    __tablename__ = "trending_pulls"

    id: Mapped[int] = Column(Integer, primary_key=True, autoincrement=True)
    pulled_at: Mapped[datetime] = Column(DateTime, nullable=False, default=datetime.utcnow)
    project_count: Mapped[int] = Column(Integer, nullable=False, default=0)
    source: Mapped[str] = Column(String(32), nullable=False, default="trending")
    summary: Mapped[Optional[str]] = Column(Text, nullable=True)

    # 关联
    items = relationship("TrendingPullItem", back_populates="pull", order_by="TrendingPullItem.rank")

    def __repr__(self):
        return f"<TrendingPull(id={self.id}, pulled_at={self.pulled_at}, count={self.project_count})>"


class TrendingPullItem(Base):
    """Trending pull 与 repo 的多对多关联。"""

    __tablename__ = "trending_pull_items"

    id: Mapped[int] = Column(Integer, primary_key=True, autoincrement=True)
    pull_id: Mapped[int] = Column(Integer, ForeignKey("trending_pulls.id"), nullable=False, index=True)
    repo_id: Mapped[int] = Column(Integer, ForeignKey("repositories.id"), nullable=False, index=True)
    rank: Mapped[int] = Column(Integer, nullable=False)
    stars: Mapped[int] = Column(Integer, nullable=False, default=0)
    stars_week: Mapped[int] = Column(Integer, nullable=False, default=0)
    forks: Mapped[int] = Column(Integer, nullable=False, default=0)
    created_at: Mapped[Optional[str]] = Column(String(64), nullable=True)

    pull = relationship("TrendingPull", back_populates="items")
    repository = relationship("Repository", back_populates="pull_items")

    __table_args__ = (
        UniqueConstraint("pull_id", "repo_id", name="uq_pull_repo"),
    )

    def __repr__(self):
        return f"<TrendingPullItem(pull={self.pull_id}, repo={self.repo_id}, rank={self.rank})>"


class ProjectAnalysis(Base):
    """LLM 项目分析结果。每个 repo 只保留最新一份。"""

    __tablename__ = "project_analyses"

    id: Mapped[int] = Column(Integer, primary_key=True, autoincrement=True)
    repo_id: Mapped[int] = Column(Integer, ForeignKey("repositories.id"), unique=True, nullable=False, index=True)
    analyzed_at: Mapped[datetime] = Column(DateTime, nullable=False, default=datetime.utcnow)
    functionality: Mapped[Optional[str]] = Column(Text, nullable=True)
    tech_stack: Mapped[Optional[str]] = Column(Text, nullable=True)
    pain_points: Mapped[Optional[str]] = Column(Text, nullable=True)
    competitors: Mapped[Optional[str]] = Column(Text, nullable=True)
    raw_response: Mapped[Optional[str]] = Column(Text, nullable=True)

    repository = relationship("Repository", back_populates="analysis")

    def __repr__(self):
        return f"<ProjectAnalysis(repo={self.repo_id}, at={self.analyzed_at})>"


class ConsecutiveTracking(Base):
    """近一周上榜跟踪。每个 repo 仅一条记录。"""

    __tablename__ = "consecutive_tracking"

    id: Mapped[int] = Column(Integer, primary_key=True, autoincrement=True)
    repo_id: Mapped[int] = Column(Integer, ForeignKey("repositories.id"), unique=True, nullable=False)
    first_seen: Mapped[Optional[date]] = Column(Date, nullable=True)
    last_seen: Mapped[Optional[date]] = Column(Date, nullable=True)
    appearance_days: Mapped[int] = Column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = Column(Boolean, nullable=False, default=False)

    repository = relationship("Repository", back_populates="tracking")

    def __repr__(self):
        return f"<ConsecutiveTracking(repo={self.repo_id}, days={self.appearance_days}, active={self.is_active})>"


class AppSetting(Base):
    """应用设置。key-value 存储。"""

    __tablename__ = "app_settings"

    key: Mapped[str] = Column(String(64), primary_key=True)
    value: Mapped[str] = Column(Text, nullable=False, default="")

    def __repr__(self):
        return f"<AppSetting({self.key}={self.value})>"


# ═══════════════════════════════════════════════════════════════
# 数据库管理
# ═══════════════════════════════════════════════════════════════

class Database:
    """数据库管理器。"""

    def __init__(self, db_path: str = "data/trending.db"):
        db_file = Path(db_path)
        db_file.parent.mkdir(parents=True, exist_ok=True)

        self.engine = create_engine(
            f"sqlite:///{db_path}",
            echo=False,
            connect_args={"check_same_thread": False},
        )
        self.SessionFactory = sessionmaker(bind=self.engine)

    def create_tables(self):
        """创建所有表。"""
        Base.metadata.create_all(self.engine)
        logger.info("数据库表已就绪")

    def get_session(self) -> Session:
        return self.SessionFactory()


# ═══════════════════════════════════════════════════════════════
# CRUD 操作
# ═══════════════════════════════════════════════════════════════

class CRUD:
    """封装常用 CRUD 操作。"""

    def __init__(self, db: Database):
        self.db = db

    # ── Repository ──────────────────────────────────────────

    def upsert_repository(
        self,
        github_id: Optional[int],
        full_name: str,
        url: str,
        description: Optional[str] = None,
        language: Optional[str] = None,
    ) -> Repository:
        """插入或更新仓库。按 full_name 查找，不存在则新建。"""
        with self.db.get_session() as session:
            repo = session.query(Repository).filter_by(full_name=full_name).first()
            if repo is None and github_id is not None:
                repo = session.query(Repository).filter_by(github_id=github_id).first()

            if repo:
                repo.full_name = full_name
                repo.url = url
                repo.description = description
                repo.language = language
                if github_id is not None:
                    repo.github_id = github_id
            else:
                repo = Repository(
                    github_id=github_id,
                    full_name=full_name,
                    url=url,
                    description=description,
                    language=language,
                )
                session.add(repo)
            session.commit()
            session.refresh(repo)
            return repo

    # ── TrendingPull ────────────────────────────────────────

    def create_pull(self, pulled_at: Optional[datetime] = None, source: str = "trending") -> TrendingPull:
        """创建一条 pull 记录。source: trending | search"""
        with self.db.get_session() as session:
            pull = TrendingPull(pulled_at=pulled_at or datetime.utcnow(), source=source)
            session.add(pull)
            session.commit()
            session.refresh(pull)
            return pull

    def add_pull_item(
        self,
        pull_id: int,
        repo_id: int,
        rank: int,
        stars: int = 0,
        stars_week: int = 0,
        forks: int = 0,
        created_at: Optional[str] = None,
    ):
        """向 pull 添加一个项目。"""
        with self.db.get_session() as session:
            existing = (
                session.query(TrendingPullItem)
                .filter_by(pull_id=pull_id, repo_id=repo_id)
                .first()
            )
            if existing:
                existing.rank = rank
                existing.stars = stars
                existing.stars_week = stars_week
                existing.forks = forks
                if created_at is not None:
                    existing.created_at = created_at
            else:
                item = TrendingPullItem(
                    pull_id=pull_id,
                    repo_id=repo_id,
                    rank=rank,
                    stars=stars,
                    stars_week=stars_week,
                    forks=forks,
                    created_at=created_at,
                )
                session.add(item)
            session.commit()

    def update_pull_count(self, pull_id: int, count: int):
        """更新 pull 的项目计数。"""
        with self.db.get_session() as session:
            pull = session.query(TrendingPull).filter_by(id=pull_id).first()
            if pull:
                pull.project_count = count
                session.commit()

    def update_pull_summary(self, pull_id: int, summary: str):
        """更新 pull 的周总结。"""
        with self.db.get_session() as session:
            pull = session.query(TrendingPull).filter_by(id=pull_id).first()
            if pull:
                pull.summary = summary
                session.commit()

    def get_pulls(self, limit: int = 50) -> list[TrendingPull]:
        """获取所有 trending pull，按时间倒序。"""
        with self.db.get_session() as session:
            return (
                session.query(TrendingPull)
                .order_by(TrendingPull.id.desc())
                .limit(limit)
                .all()
            )

    def get_pull(self, pull_id: int) -> Optional[TrendingPull]:
        """获取单条 pull 记录。"""
        with self.db.get_session() as session:
            return session.query(TrendingPull).filter_by(id=pull_id).first()

    def get_pull_with_items(self, pull_id: int) -> Optional[TrendingPull]:
        """获取 pull 详情，含项目列表（eager load）。"""
        with self.db.get_session() as session:
            pull = (
                session.query(TrendingPull)
                .filter_by(id=pull_id)
                .first()
            )
            if pull:
                # 在 session 内加载 items 和关联的 repository
                _ = pull.items
                for item in pull.items:
                    _ = item.repository
            return pull

    def get_latest_pull(self) -> Optional[TrendingPull]:
        """获取最新一条 pull。"""
        with self.db.get_session() as session:
            return (
                session.query(TrendingPull)
                .order_by(TrendingPull.id.desc())
                .first()
            )

    def has_pull_for_date(self, target_date: date) -> bool:
        """检查指定日期是否已有 trending pull 记录（幂等性保护）。"""
        with self.db.get_session() as session:
            return (
                session.query(TrendingPull)
                .filter(sa_func.date(TrendingPull.pulled_at) == target_date)
                .first()
                is not None
            )

    def get_pull_item_repos(self, pull_id: int) -> list[dict]:
        """获取 pull 中所有项目（含 repo 信息）。"""
        with self.db.get_session() as session:
            items = (
                session.query(TrendingPullItem)
                .options(joinedload(TrendingPullItem.repository))
                .filter_by(pull_id=pull_id)
                .order_by(TrendingPullItem.rank)
                .all()
            )
            return [
                {
                    "repo": item.repository,
                    "rank": item.rank,
                    "stars": item.stars,
                    "forks": item.forks,
                }
                for item in items
            ]

    def delete_pull(self, pull_id: int):
        """删除 pull 及其所有 items。"""
        with self.db.get_session() as session:
            session.query(TrendingPullItem).filter_by(pull_id=pull_id).delete()
            session.query(TrendingPull).filter_by(id=pull_id).delete()
            session.commit()

    def dedup_pulls_for_date(self, target_date: date) -> int:
        """清理指定日期的重复 pull，保留最新一条，返回删除数。"""
        with self.db.get_session() as session:
            pulls = (
                session.query(TrendingPull)
                .filter(sa_func.date(TrendingPull.pulled_at) == target_date)
                .order_by(TrendingPull.id.desc())
                .all()
            )
            if len(pulls) <= 1:
                return 0

            # 保留最新的（ID 最大），删除其余
            keep = pulls[0]
            to_delete = pulls[1:]
            for p in to_delete:
                session.query(TrendingPullItem).filter_by(pull_id=p.id).delete()
                session.query(TrendingPull).filter_by(id=p.id).delete()
            session.commit()
            logger.info("去重 %s: 保留 #%d, 删除 %d 条 (%s)",
                         target_date, keep.id, len(to_delete),
                         [p.id for p in to_delete])
            return len(to_delete)

    def dedup_all_pulls(self) -> int:
        """清理所有日期的重复 pull，返回总删除数。"""
        with self.db.get_session() as session:
            # 找到有重复的日期
            dup_dates = (
                session.query(sa_func.date(TrendingPull.pulled_at).label("d"))
                .group_by(sa_func.date(TrendingPull.pulled_at))
                .having(sa_func.count(TrendingPull.id) > 1)
                .all()
            )
        total = 0
        for (d,) in dup_dates:
            total += self.dedup_pulls_for_date(d)
        logger.info("全局去重完成: 删除 %d 条重复 pull", total)
        return total

    # ── ProjectAnalysis ─────────────────────────────────────

    def save_analysis(
        self,
        repo_id: int,
        functionality: Optional[str] = None,
        tech_stack: Optional[str] = None,
        pain_points: Optional[str] = None,
        competitors: Optional[str] = None,
        raw_response: Optional[str] = None,
    ):
        """保存/更新项目分析。按 repo_id upsert（覆盖旧分析）。"""
        with self.db.get_session() as session:
            analysis = session.query(ProjectAnalysis).filter_by(repo_id=repo_id).first()
            if analysis:
                analysis.functionality = functionality
                analysis.tech_stack = tech_stack
                analysis.pain_points = pain_points
                analysis.competitors = competitors
                analysis.raw_response = raw_response
                analysis.analyzed_at = datetime.utcnow()
            else:
                analysis = ProjectAnalysis(
                    repo_id=repo_id,
                    functionality=functionality,
                    tech_stack=tech_stack,
                    pain_points=pain_points,
                    competitors=competitors,
                    raw_response=raw_response,
                )
                session.add(analysis)
            session.commit()

    def get_analysis(self, repo_id: int) -> Optional[ProjectAnalysis]:
        """获取项目的最新分析。"""
        with self.db.get_session() as session:
            return session.query(ProjectAnalysis).filter_by(repo_id=repo_id).first()

    def get_analyses_for_repos(self, repo_ids: list[int]) -> dict[int, Optional[ProjectAnalysis]]:
        """批量获取多个项目的分析。"""
        with self.db.get_session() as session:
            analyses = (
                session.query(ProjectAnalysis)
                .filter(ProjectAnalysis.repo_id.in_(repo_ids))
                .all()
            )
            mapping = {a.repo_id: a for a in analyses}
            return {rid: mapping.get(rid) for rid in repo_ids}

    # ── ConsecutiveTracking ─────────────────────────────────

    def get_all_consecutive_tracking(self) -> list[ConsecutiveTracking]:
        """获取所有跟踪记录。"""
        with self.db.get_session() as session:
            return session.query(ConsecutiveTracking).all()

    def get_active_tracking(self) -> list[ConsecutiveTracking]:
        """获取活跃的跟踪记录（近一周上榜 >= 2 天）。"""
        with self.db.get_session() as session:
            return (
                session.query(ConsecutiveTracking)
                .filter(
                    ConsecutiveTracking.is_active == True,
                    ConsecutiveTracking.appearance_days >= 2,
                )
                .order_by(ConsecutiveTracking.appearance_days.desc())
                .all()
            )

    def get_tracking_by_repo_id(self, repo_id: int) -> Optional[ConsecutiveTracking]:
        """获取指定仓库的跟踪记录。"""
        with self.db.get_session() as session:
            return (
                session.query(ConsecutiveTracking)
                .filter_by(repo_id=repo_id)
                .first()
            )

    def count_appearance_days(self, repo_id: int, since_date: date, until_date: date) -> int:
        """统计仓库在指定日期范围内出现在 trending_pull_items 中的天数。

        通过 trending_pull_items → trending_pulls.pulled_at 关联计算。
        """
        with self.db.get_session() as session:
            result = (
                session.query(sa_func.count(sa_func.distinct(
                    sa_func.date(TrendingPull.pulled_at)
                )))
                .select_from(TrendingPullItem)
                .join(TrendingPull, TrendingPullItem.pull_id == TrendingPull.id)
                .filter(
                    TrendingPullItem.repo_id == repo_id,
                    sa_func.date(TrendingPull.pulled_at) >= since_date,
                    sa_func.date(TrendingPull.pulled_at) <= until_date,
                )
                .scalar()
            )
            return result or 0

    # ── Scheduler Lock (跨进程互斥) ──────────────────────────

    def acquire_scheduler_lock(self, mode: str = "web_server") -> bool:
        """尝试获取调度器锁（跨进程互斥）。

        成功返回 True，失败（已有活跃调度器）返回 False。
        自动清理孤儿锁（PID 已不存在）。
        """
        import os as _os
        import sys as _sys

        current_pid = str(_os.getpid())

        with self.db.get_session() as session:
            existing_pid = self.get_setting("scheduler_pid", "")
            existing_started = self.get_setting("scheduler_started_at", "")

            # 检查是否有已存在的锁
            if existing_pid:
                # 同一进程，已持有锁
                if existing_pid == current_pid:
                    return True

                # 检查锁持有者是否还存活
                if self._pid_is_alive(int(existing_pid)):
                    logger.warning(
                        "调度器锁被 PID %s 持有（%s），拒绝获取",
                        existing_pid, existing_started
                    )
                    return False
                else:
                    logger.warning(
                        "检测到孤儿锁 PID %s（已不存在），自动清理",
                        existing_pid
                    )

            # 获取锁
            self.set_setting("scheduler_pid", current_pid)
            self.set_setting("scheduler_started_at",
                             datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"))
            self.set_setting("scheduler_mode", mode)
            logger.info("调度器锁已获取: PID=%s mode=%s", current_pid, mode)
            return True

    def release_scheduler_lock(self):
        """释放调度器锁。"""
        import os as _os
        current_pid = str(_os.getpid())
        existing = self.get_setting("scheduler_pid", "")
        if existing == current_pid or not existing:
            self.set_setting("scheduler_pid", "")
            self.set_setting("scheduler_started_at", "")
            self.set_setting("scheduler_mode", "")
            logger.info("调度器锁已释放: PID=%s", current_pid)

    def get_scheduler_owner(self) -> dict | None:
        """查询当前调度器锁的持有者信息。"""
        pid = self.get_setting("scheduler_pid", "")
        if not pid:
            return None
        return {
            "pid": int(pid),
            "started_at": self.get_setting("scheduler_started_at", ""),
            "mode": self.get_setting("scheduler_mode", ""),
            "is_alive": self._pid_is_alive(int(pid)),
        }

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        """检查进程是否存活（跨平台）。"""
        import os as _os
        import sys as _sys
        try:
            if _sys.platform == "win32":
                import ctypes
                from ctypes import wintypes
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFORMATION
                if handle:
                    kernel32.CloseHandle(handle)
                    return True
                return False
            else:
                _os.kill(pid, 0)
                return True
        except (OSError, ProcessLookupError):
            return False


    # ── AppSetting ──────────────────────────────────────────

    def get_setting(self, key: str, default: str = "") -> str:
        """获取单个设置值。"""
        with self.db.get_session() as session:
            s = session.query(AppSetting).filter_by(key=key).first()
            return s.value if s else default

    def set_setting(self, key: str, value: str):
        """设置单个值。"""
        with self.db.get_session() as session:
            s = session.query(AppSetting).filter_by(key=key).first()
            if s:
                s.value = value
            else:
                session.add(AppSetting(key=key, value=value))
            session.commit()

    def get_all_settings(self) -> dict:
        """获取所有设置。"""
        defaults = {
            "per_page": "20",
            "auto_analyze": "false",
            "auto_pull": "false",
            "pull_start_time": "09:00",
            "pull_period_mode": "interval",
            "pull_interval_hours": "24",
            "github_token": "",
        }
        with self.db.get_session() as session:
            for s in session.query(AppSetting).all():
                v = s.value
                # 兼容历史脏数据：布尔值统一转小写
                if v in ("True", "False"):
                    v = v.lower()
                defaults[s.key] = v
        return defaults


# 全局单例
_db_instance: Optional[Database] = None
_crud_instance: Optional[CRUD] = None


def get_database(db_path: str = "data/trending.db") -> Database:
    global _db_instance
    if _db_instance is None:
        _db_instance = Database(db_path)
        _db_instance.create_tables()
    return _db_instance


def get_crud(db_path: str = "data/trending.db") -> CRUD:
    global _crud_instance
    if _crud_instance is None:
        _crud_instance = CRUD(get_database(db_path))
    return _crud_instance
