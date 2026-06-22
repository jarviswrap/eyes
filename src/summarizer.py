"""周趋势总结模块。

基于一组项目的 LLM 分析结果，生成趋势总结报告。
"""

import logging
from datetime import date, datetime, timezone
from typing import Optional

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


SUMMARY_SYSTEM_PROMPT = """你是一位资深技术趋势分析师。请基于提供的 GitHub Trending 项目分析数据，
撰写一份全面、有洞察力的趋势总结报告。你必须严格按照 JSON 格式返回结果。"""

SUMMARY_USER_PROMPT_TEMPLATE = """请根据以下 GitHub Trending 项目分析汇总数据，撰写一份趋势总结报告。

分析项目总数：{total_count}

## 重点项目（近一周多次上榜）
{highlights}

## 各项目分析详情

{analyses_text}

请从以下角度进行深度总结（用中文回答）：

1. **技术热点与趋势方向**：涌现了哪些技术热点？哪些技术方向受到最多关注？
2. **最值得关注的新兴项目**：如果只能推荐 3-5 个项目给开发者关注，你会推荐哪些？为什么？
3. **技术栈变迁趋势**：从上榜项目的技术选择中，能看出哪些技术栈演进的趋势？
4. **领域分布分析**：上榜项目主要集中在哪些领域（如 AI/ML、DevOps、前端、后端、工具链等）？
5. **多次上榜重点项目解析**：那些多次上榜的项目有什么共同特点？反映了什么趋势？

请严格按照以下 JSON 格式返回（不要包含 markdown 代码块标记）：
{{
    "hot_topics": "技术热点与趋势方向...",
    "recommended_projects": "最值得关注的新兴项目...",
    "tech_trends": "技术栈变迁趋势...",
    "domain_analysis": "领域分布分析...",
    "key_insights": "多次上榜重点项目解析..."
}}"""


class WeeklySummarizer:
    """周趋势总结器。"""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com/v1",
        model: str = "deepseek-chat",
        max_tokens: int = 4096,
        temperature: float = 0.3,
        max_retries: int = 3,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_retries = max_retries

        self._client: Optional[AsyncOpenAI] = None

    @property
    def client(self) -> Optional[AsyncOpenAI]:
        if self._client is None and self.api_key:
            self._client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
            )
        return self._client

    async def generate_summary(
        self,
        analyses: list[dict],
        highlights_report: str = "",
    ) -> str:
        """基于已有的项目分析生成趋势总结。

        Args:
            analyses: 项目分析列表，每项包含 full_name, language, functionality 等
            highlights_report: 重点关注项目文字报告

        Returns:
            Markdown 格式的趋势总结文本
        """
        if not self.client:
            logger.error("无法生成总结: LLM API key 未配置")
            return ""

        if not analyses:
            logger.warning("无分析数据，无法生成总结")
            return ""

        # 构建分析详情文本
        parts = []
        for a in analyses:
            parts.append(
                f"### {a.get('full_name', '未知')}\n"
                f"- 语言: {a.get('language') or '未知'}\n"
                f"- 功能: {a.get('functionality') or '无'}\n"
                f"- 技术栈: {a.get('tech_stack') or '无'}\n"
                f"- 痛点: {a.get('pain_points') or '无'}\n"
                f"- 竞品: {a.get('competitors') or '无'}\n"
            )
        analyses_text = "\n".join(parts)

        user_prompt = SUMMARY_USER_PROMPT_TEMPLATE.format(
            total_count=len(analyses),
            highlights=highlights_report or "无",
            analyses_text=analyses_text,
        )

        import asyncio

        for attempt in range(1, self.max_retries + 1):
            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                )
                content = response.choices[0].message.content or ""

                parsed = self._parse_json_response(content)
                if parsed:
                    summary_text = self._format_summary(len(analyses), parsed)
                    return summary_text
                else:
                    logger.warning(f"JSON 解析失败，重试 {attempt}/{self.max_retries}")
                    if attempt < self.max_retries:
                        await asyncio.sleep(2 * attempt)

            except Exception as e:
                logger.error(f"LLM 调用异常，重试 {attempt}/{self.max_retries}: {e}")
                if attempt < self.max_retries:
                    await asyncio.sleep(2 * attempt)

        logger.error("总结生成最终失败")
        return ""

    @staticmethod
    def _format_summary(project_count: int, parsed: dict) -> str:
        lines = [
            "# GitHub Trending 趋势总结",
            f"**分析项目数**: {project_count}",
            f"**生成时间**: {datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}",
            "",
            "---",
            "",
            "## 🔥 技术热点与趋势方向",
            parsed.get("hot_topics", "无"),
            "",
            "## ⭐ 最值得关注的新兴项目",
            parsed.get("recommended_projects", "无"),
            "",
            "## 📊 技术栈变迁趋势",
            parsed.get("tech_trends", "无"),
            "",
            "## 🗂️ 领域分布分析",
            parsed.get("domain_analysis", "无"),
            "",
            "## 🔍 多次上榜重点项目解析",
            parsed.get("key_insights", "无"),
            "",
            "---",
            f"*报告由 DeepSeek LLM 自动生成*",
        ]
        return "\n".join(lines)

    @staticmethod
    def _parse_json_response(content: str) -> dict | None:
        import json
        import re

        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```\s*$", "", cleaned)

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        json_match = re.search(r"\{[\s\S]*\}", cleaned)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass

        return None
