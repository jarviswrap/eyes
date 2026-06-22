"""近一周上榜跟踪模块。

统计每个项目在最近7天内的 Trending Pull 中出现次数，
对 >=2 次的项目进行重点关注标记。
"""

import logging
from datetime import date, timedelta

from .database import CRUD, Repository, ConsecutiveTracking

logger = logging.getLogger(__name__)


class ConsecutiveTracker:
    """近一周上榜跟踪器。"""

    def __init__(self, crud: CRUD):
        self.crud = crud

    def update(self, reference_date: date, repos: list[Repository]):
        """更新所有仓库的近一周上榜统计。

        Args:
            reference_date: 参考日期
            repos: 本次涉及的所有仓库（不一定全部上榜）
        """
        seven_days_ago = reference_date - timedelta(days=6)

        with self.crud.db.get_session() as session:
            for repo in repos:
                appearance_count = self.crud.count_appearance_days(
                    repo.id, seven_days_ago, reference_date
                )

                tracking = self.crud.get_tracking_by_repo_id(repo.id)

                if tracking is None:
                    tracking = ConsecutiveTracking(
                        repo_id=repo.id,
                        first_seen=reference_date,
                        last_seen=reference_date,
                        appearance_days=appearance_count,
                        is_active=appearance_count >= 2,
                    )
                    session.add(tracking)
                    if appearance_count >= 2:
                        logger.info(
                            f"[HIGHLIGHT] {repo.full_name} "
                            f"近一周上榜 {appearance_count} 天！"
                        )
                else:
                    tracking.last_seen = reference_date
                    tracking.appearance_days = appearance_count
                    was_active = tracking.is_active
                    tracking.is_active = appearance_count >= 2

                    if tracking.is_active and not was_active:
                        logger.info(
                            f"[HIGHLIGHT] {repo.full_name} "
                            f"近一周上榜 {appearance_count} 天！"
                        )
                    elif not tracking.is_active and was_active:
                        logger.info(
                            f"[UNHIGHLIGHT] {repo.full_name} "
                            f"近一周上榜降至 {appearance_count} 天"
                        )

            # 更新不在本次列表中的仓库的统计
            all_tracked = self.crud.get_all_consecutive_tracking()
            today_ids = {r.id for r in repos}

            for tracking in all_tracked:
                if tracking.repo_id not in today_ids:
                    appearance_count = self.crud.count_appearance_days(
                        tracking.repo_id, seven_days_ago, reference_date
                    )
                    tracking.appearance_days = appearance_count
                    tracking.is_active = appearance_count >= 2

            session.commit()

    def get_highlights(self) -> list[dict]:
        """获取当前重点关注的项目。"""
        active = self.crud.get_active_tracking()
        highlights = []
        for tracking in active:
            repo = tracking.repository
            highlights.append({
                "full_name": repo.full_name,
                "url": repo.url,
                "description": repo.description,
                "language": repo.language,
                "appearance_days": tracking.appearance_days,
                "first_seen": tracking.first_seen.isoformat() if tracking.first_seen else "",
                "last_seen": tracking.last_seen.isoformat() if tracking.last_seen else "",
            })
        return highlights

    def generate_highlight_report(self) -> str:
        """生成重点关注项目的文字报告。"""
        highlights = self.get_highlights()
        if not highlights:
            return "本周无近一周多次上榜项目。"

        lines = ["### 🔥 近一周多次上榜重点项目", ""]
        for h in highlights:
            lines.append(
                f"- **{h['full_name']}**：近一周上榜 {h['appearance_days']} 天 "
                f"({h['first_seen']} ~ {h['last_seen']}) | "
                f"语言: {h['language']} | {h['description'][:100] if h['description'] else ''}"
            )
        return "\n".join(lines)
