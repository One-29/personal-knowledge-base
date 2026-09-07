"""应用配置：从 .env / 环境变量读取（pydantic-settings）。

单一事实：这里定义的字段是全应用唯一的配置来源。
新配置项先加到这里，再在 .env.example 补样例。
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # 数据库连接串（psycopg3 方言）
    database_url: str = "postgresql+psycopg://postgres@127.0.0.1:5432/knowbase"

    # 原文文件存储根目录（决策 D6）
    storage_dir: Path = Path("./data/storage")

    # 上传大小上限（字节）—— 对应错误码 TOO_LARGE
    max_upload_bytes: int = 10 * 1024 * 1024


# 进程内单例：整个应用共享一份配置（import settings 即用）
settings = Settings()
