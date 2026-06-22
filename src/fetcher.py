"""GitHub Weekly Trending 数据抓取模块。

主方案：解析 github.com/trending?since=weekly 页面（真正的周 trending 数据）
备选方案：GitHub Search API（当页面抓取失败时降级使用）
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


@dataclass
class TrendingRepo:
    """Trending 仓库数据结构。"""

    github_id: int
    full_name: str          # owner/repo
    url: str
    description: str
    language: str
    stars: int              # 总 stars
    stars_week: int         # 本周 stars
    forks: int              # 总 forks
    rank: int               # 排名 1-20

    def __repr__(self):
        return f"<TrendingRepo(#{self.rank} {self.full_name} stars:{self.stars})>"


# GitHub Weekly Trending 页面 URL
GITHUB_TRENDING_URL = "https://github.com/trending?since=weekly"

# GitHub Search API（备选）
GITHUB_SEARCH_API = "https://api.github.com/search/repositories"

# 模拟真实浏览器的请求头，避免被 GitHub 拦截
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Chromium";v="126", "Google Chrome";v="126", "Not=A?Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# GitHub API 请求头
API_HEADERS = {
    "Accept": "application/vnd.github.v3+json",
    "User-Agent": "GitHubTrendingAnalyzer/1.0",
}


class GitHubFetcher:
    """GitHub Weekly Trending 数据抓取器。

    优先从 github.com/trending?since=weekly 页面抓取真正的周 trending 数据，
    Search API 仅作为备选方案。
    """

    def __init__(
        self,
        token: str = "",
        per_page: int = 20,
        request_delay: int = 2,
        search_period_days: int = 7,
        search_sort: str = "stars",
        search_order: str = "desc",
        search_min_forks: int = 3,
    ):
        self.token = token
        self.per_page = per_page
        self.request_delay = request_delay
        self.search_period_days = search_period_days
        self.search_sort = search_sort
        self.search_order = search_order
        self.search_min_forks = search_min_forks

    async def fetch_via_trending_page(self) -> list[TrendingRepo]:
        """【主方案】解析 GitHub Weekly Trending 页面获取仓库列表。

        URL: https://github.com/trending?since=weekly
        这是 GitHub 官方周 trending 排名，按本周获得 star 数排序。
        """
        repos = []

        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
        ) as client:
            try:
                response = await client.get(
                    GITHUB_TRENDING_URL,
                    headers=BROWSER_HEADERS,
                )
                response.raise_for_status()
                soup = BeautifulSoup(response.text, "lxml")

                # 解析 Trending 页面仓库条目
                # GitHub 页面结构：每个仓库是一个 article.Box-row
                articles = soup.select("article.Box-row")
                if not articles:
                    # 备用选择器
                    articles = soup.select('[data-testid="repository-list-item"]')
                if not articles:
                    # 再试更通用的选择器
                    articles = soup.select(".Box-row")

                if not articles:
                    logger.warning("Trending 页面未找到仓库条目，可能页面结构已变更")
                    # 保存页面片段用于调试
                    body_preview = response.text[:500] if response.text else "(empty)"
                    logger.debug(f"页面预览: {body_preview}")
                    return []

                for idx, article in enumerate(articles[:self.per_page]):
                    try:
                        # ── 提取仓库名和 owner ──
                        h2 = article.select_one("h2")
                        if not h2:
                            continue
                        name_link = h2.select_one("a")
                        if not name_link:
                            continue

                        full_name = name_link.get("href", "").strip("/")
                        if not full_name:
                            continue
                        url = f"https://github.com/{full_name}"

                        # ── 提取描述 ──
                        desc_el = article.select_one("p")
                        description = desc_el.get_text(strip=True) if desc_el else ""

                        # ── 提取语言 ──
                        lang_el = article.select_one('[itemprop="programmingLanguage"]')
                        language = lang_el.get_text(strip=True) if lang_el else "Unknown"

                        # ── 提取总 stars 和 forks ──
                        stars = 0
                        forks = 0
                        stars_week = 0
                        stats_links = article.select("a.Link--muted")
                        for link in stats_links:
                            text = link.get_text(strip=True)
                            href = link.get("href", "")
                            if "/stargazers" in href:
                                stars = self._parse_number(text)
                            elif "/forks" in href:
                                forks = self._parse_number(text)
                        # 提取本周 stars
                        stars_week_el = article.select_one(".d-inline-block.float-sm-right")
                        if stars_week_el:
                            stars_week = self._parse_number(stars_week_el.get_text(strip=True))

                        repos.append(TrendingRepo(
                            github_id=0,
                            full_name=full_name,
                            url=url,
                            description=description,
                            language=language,
                            stars=stars,
                            stars_week=stars_week,
                            forks=forks,
                            rank=idx + 1,
                        ))
                    except Exception as e:
                        logger.debug(f"解析 trending 条目 #{idx+1} 失败: {e}")
                        continue

                if repos:
                    logger.info(f"Weekly Trending 页面: 成功抓取 {len(repos)} 个项目")
                else:
                    logger.warning("Trending 页面解析到 0 个有效条目")

                return repos

            except httpx.HTTPStatusError as e:
                logger.warning(f"Trending 页面请求被拒 (HTTP {e.response.status_code})")
                return []
            except httpx.TimeoutException:
                logger.warning("Trending 页面请求超时")
                return []
            except Exception as e:
                logger.error(f"Trending 页面抓取异常: {e}")
                return []

    async def fetch_via_search_api(self) -> list[TrendingRepo]:
        """【备选方案】通过 GitHub Search API 获取近期热门仓库。

        搜索过去 7 天内创建的仓库，按 stars 排序。
        注意：这不是真正的 "weekly trending"，仅作为降级方案。
        """
        headers = dict(API_HEADERS)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        period_ago = date.today() - timedelta(days=self.search_period_days)
        date_str = period_ago.isoformat()
        params = {
            "q": f"created:>={date_str} forks:>={self.search_min_forks}",
            "sort": self.search_sort,
            "order": self.search_order,
            "per_page": self.per_page,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.get(
                    GITHUB_SEARCH_API,
                    headers=headers,
                    params=params,
                )
                response.raise_for_status()
                data = response.json()

                repos = []
                for idx, item in enumerate(data.get("items", [])[:self.per_page]):
                    repos.append(TrendingRepo(
                        github_id=item["id"],
                        full_name=item["full_name"],
                        url=item["html_url"],
                        description=item.get("description") or "",
                        language=item.get("language") or "Unknown",
                        stars=item["stargazers_count"],
                        stars_week=0,
                        forks=item["forks_count"],
                        rank=idx + 1,
                    ))

                logger.info(f"Search API 返回 {len(repos)} 个仓库（备选）")
                return repos

            except httpx.HTTPStatusError as e:
                logger.warning(f"Search API 请求失败 (HTTP {e.response.status_code})")
                if e.response.status_code == 403:
                    logger.warning("可能是 API 限流")
                return []
            except Exception as e:
                logger.error(f"Search API 请求异常: {e}")
                return []

    async def fetch_trending(self) -> list[TrendingRepo]:
        """主入口：获取 GitHub Weekly Trending 前 20 项目。

        优先使用 github.com/trending?since=weekly 页面（真正的周 trending），
        抓取失败时降级使用 Search API。
        """
        # ── 主方案：抓取 Weekly Trending 页面 ──
        trending_repos = await self.fetch_via_trending_page()

        if trending_repos:
            logger.info(f"使用 Weekly Trending 页面数据（{len(trending_repos)} 个项目）")
            return trending_repos

        # ── 备选方案：Search API ──
        logger.warning("Trending 页面无数据，降级使用 Search API")
        search_repos = await self.fetch_via_search_api()

        if search_repos:
            logger.info(f"使用 Search API 数据（{len(search_repos)} 个项目）")
            return search_repos

        logger.error("无法获取任何 trending 数据")
        return []

    @staticmethod
    def _parse_number(text: str) -> int:
        """将 '1.2k', '12,345', '999', '1,234 stars this week' 等文本转为整数。"""
        # 去掉 "stars this week" / "stars today" 等后缀
        text = text.strip().replace(",", "").lower()
        for suffix in ["stars this week", "stars today", "stars this month", "stars"]:
            if suffix in text:
                text = text.replace(suffix, "").strip()
        if text.endswith("k"):
            try:
                return int(float(text[:-1]) * 1000)
            except ValueError:
                return 0
        try:
            return int(text)
        except ValueError:
            return 0
