"""配置加载模块。从 config.yaml 读取配置，支持环境变量替换。"""

import os
import re
from pathlib import Path
from dataclasses import dataclass, field

import yaml


@dataclass
class SchedulerConfig:
    run_time: str = "09:00"
    timezone: str = "Asia/Shanghai"
    summary_day: int = 6  # 周日


@dataclass
class LLMConfig:
    provider: str = "deepseek"
    api_key: str = ""
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    max_tokens: int = 4096
    temperature: float = 0.3
    concurrency: int = 5
    max_retries: int = 3


@dataclass
class GithubConfig:
    token: str = ""
    per_page: int = 20
    request_delay: int = 2


@dataclass
class DatabaseConfig:
    path: str = "data/trending.db"


@dataclass
class OutputConfig:
    log_level: str = "INFO"
    log_file: str = "logs/app.log"


@dataclass
class AppConfig:
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    github: GithubConfig = field(default_factory=GithubConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    output: OutputConfig = field(default_factory=OutputConfig)


# 环境变量占位符正则: ${VAR_NAME} 或 ${VAR_NAME:default_value}
_ENV_VAR_PATTERN = re.compile(r"\$\{(\w+)(?::([^}]*))?\}")


def _resolve_env_vars(value: str) -> str:
    """替换字符串中的 ${ENV_VAR} 或 ${ENV_VAR:default} 为环境变量值。"""
    def replacer(match):
        var_name = match.group(1)
        default = match.group(2)
        return os.environ.get(var_name, default if default is not None else "")

    return _ENV_VAR_PATTERN.sub(replacer, value)


def _resolve_dict(d: dict) -> dict:
    """递归解析字典中所有字符串值的环境变量。"""
    result = {}
    for key, value in d.items():
        if isinstance(value, str):
            result[key] = _resolve_env_vars(value)
        elif isinstance(value, dict):
            result[key] = _resolve_dict(value)
        else:
            result[key] = value
    return result


def load_config(config_path: str = "config.yaml") -> AppConfig:
    """从 YAML 文件加载配置。

    Args:
        config_path: 配置文件路径，默认为项目根目录的 config.yaml

    Returns:
        AppConfig 实例
    """
    config_file = Path(config_path)
    if not config_file.is_absolute():
        # 相对于项目根目录
        project_root = Path(__file__).parent.parent
        config_file = project_root / config_path

    if not config_file.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_file}")

    with open(config_file, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    raw = _resolve_dict(raw)

    return AppConfig(
        scheduler=SchedulerConfig(**raw.get("scheduler", {})),
        llm=LLMConfig(**raw.get("llm", {})),
        github=GithubConfig(**raw.get("github", {})),
        database=DatabaseConfig(**raw.get("database", {})),
        output=OutputConfig(**raw.get("output", {})),
    )
