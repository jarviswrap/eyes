"""LLM 项目分析模块。

对每个 GitHub Trending 项目使用 DeepSeek API 进行四维度分析：
1. 功能概述
2. 技术栈
3. 痛点解决
4. 竞品分析
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

import httpx
from openai import AsyncOpenAI

from .fetcher import TrendingRepo

logger = logging.getLogger(__name__)


@dataclass
class ProjectAnalysisResult:
    """单项目分析结果。"""

    functionality: str      # 功能概述
    tech_stack: str         # 技术栈
    pain_points: str        # 痛点解决
    competitors: str        # 竞品分析
    raw_response: str       # LLM 原始返回


ANALYSIS_SYSTEM_PROMPT = """你是一位资深技术分析师，擅长分析 GitHub 开源项目。
请对用户提供的 GitHub 项目进行专业、客观、全面的分析。
你必须严格按照 JSON 格式返回结果，不要添加任何额外的解释或 markdown 标记。"""

ANALYSIS_USER_PROMPT_TEMPLATE = """请对以下 GitHub 项目进行专业分析：

项目名称：{full_name}
项目描述：{description}
主语言：{language}
Star 数：{stars}
Fork 数：{forks}
README 摘要：{readme_summary}

请从以下四个维度进行深入分析（用中文回答）：

1. **功能概述**：这个项目实现了什么核心功能？面向什么用户群体？主要使用场景是什么？

2. **技术栈**：使用了哪些关键技术和框架？架构设计有什么特点？

3. **痛点解决**：这个项目解决了哪些实际的开发/运维/业务痛点？为什么用户会选择它？

4. **竞品分析**：有哪些已知的同领域知名项目？与这些竞品相比，这个项目的差异化优势是什么？

请严格按照以下 JSON 格式返回（不要包含 markdown 代码块标记）：
{{
    "functionality": "功能概述内容...",
    "tech_stack": "技术栈分析内容...",
    "pain_points": "痛点解决分析内容...",
    "competitors": "竞品分析内容..."
}}"""


class LLMAnalyzer:
    """使用 LLM 分析 GitHub 项目。"""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com/v1",
        model: str = "deepseek-chat",
        max_tokens: int = 4096,
        temperature: float = 0.3,
        max_retries: int = 3,
        concurrency: int = 5,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_retries = max_retries
        self.concurrency = concurrency

        self._client: Optional[AsyncOpenAI] = None

        # 并发控制信号量
        self._semaphore = asyncio.Semaphore(concurrency)

    @property
    def client(self) -> Optional[AsyncOpenAI]:
        """懒加载 LLM 客户端。API key 为空时返回 None。"""
        if self._client is None and self.api_key:
            self._client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
            )
        return self._client

    async def fetch_readme(self, full_name: str) -> str:
        """获取项目的 README 内容摘要。"""
        url = f"https://raw.githubusercontent.com/{full_name}/master/README.md"
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    text = resp.text[:3000]  # 只取前 3000 字符
                    return text
                # 尝试 main 分支
                url = f"https://raw.githubusercontent.com/{full_name}/main/README.md"
                resp = await client.get(url)
                if resp.status_code == 200:
                    text = resp.text[:3000]
                    return text
            except Exception as e:
                logger.debug(f"获取 README 失败 ({full_name}): {e}")

        return "(README 无法获取)"

    async def analyze_single(self, repo: TrendingRepo) -> Optional[ProjectAnalysisResult]:
        """分析单个项目。失败返回 None。"""
        async with self._semaphore:
            # 检查 API key 是否可用
            if not self.client:
                logger.error(f"无法分析 {repo.full_name}: LLM API key 未配置")
                return None

            logger.debug("分析中: %s", repo.full_name)

            # 1. 获取 README
            readme = await self.fetch_readme(repo.full_name)

            # 2. 构建 prompt
            user_prompt = ANALYSIS_USER_PROMPT_TEMPLATE.format(
                full_name=repo.full_name,
                description=repo.description or "(无描述)",
                language=repo.language,
                stars=repo.stars,
                forks=repo.forks,
                readme_summary=readme,
            )

            # 3. 调用 LLM（含重试）
            for attempt in range(1, self.max_retries + 1):
                try:
                    response = await self.client.chat.completions.create(
                        model=self.model,
                        messages=[
                            {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                            {"role": "user", "content": user_prompt},
                        ],
                        max_tokens=self.max_tokens,
                        temperature=self.temperature,
                    )
                    content = response.choices[0].message.content or ""

                    # 解析 JSON
                    parsed = self._parse_json_response(content)
                    if parsed:
                        logger.debug("分析完成: %s", repo.full_name)
                        return ProjectAnalysisResult(
                            functionality=parsed.get("functionality", ""),
                            tech_stack=parsed.get("tech_stack", ""),
                            pain_points=parsed.get("pain_points", ""),
                            competitors=parsed.get("competitors", ""),
                            raw_response=content,
                        )
                    else:
                        logger.warning("JSON 解析失败 (%s)，重试 %s/%s", repo.full_name, attempt, self.max_retries)
                        if attempt < self.max_retries:
                            await asyncio.sleep(2 * attempt)

                except Exception as e:
                    logger.error("LLM 调用异常 (%s)，重试 %s/%s: %s", repo.full_name, attempt, self.max_retries, e)
                    if attempt < self.max_retries:
                        await asyncio.sleep(2 * attempt)

            logger.error(f"分析最终失败: {repo.full_name}")
            return None

    async def analyze_batch(self, repos: list[TrendingRepo]) -> list[tuple[TrendingRepo, Optional[ProjectAnalysisResult]]]:
        """批量分析多个项目（并发控制）。"""
        logger.info("开始批量分析: %d 个项目, 并发 %d", len(repos), self.concurrency)
        tasks = [self.analyze_single(repo) for repo in repos]
        results = await asyncio.gather(*tasks)
        success = sum(1 for r in results if r is not None)
        logger.info("批量分析完成: %d/%d 成功", success, len(repos))
        return list(zip(repos, results))

    @staticmethod
    def _parse_json_response(content: str) -> Optional[dict]:
        """从 LLM 返回中提取 JSON 对象。"""
        # 尝试直接解析
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        # 尝试去掉 markdown 代码块标记
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```\s*$", "", cleaned)

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # 尝试用正则提取 JSON 对象
        json_match = re.search(r"\{[\s\S]*\}", cleaned)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass

        return None
