"""应用配置：从 .env / 环境变量读取（pydantic-settings）。

单一事实：这里定义的字段是全应用唯一的配置来源。
新配置项先加到这里，再在 .env.example 补样例。
"""

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .paths import (
    DATABASE_FILE_NAME,
    default_user_data_dir,
    runtime_config_file,
    sqlite_database_url,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=runtime_config_file(),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        populate_by_name=True,
    )

    # 日常数据默认进入操作系统用户目录；DATABASE_URL / STORAGE_DIR 仍可覆盖，
    # 供旧 PostgreSQL 数据迁移、CI 和高级自托管使用。
    data_dir: Path = Field(
        default_factory=default_user_data_dir,
        alias="KNOWBASE_DATA_DIR",
    )
    database_url: str | None = None
    storage_dir: Path | None = None
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    db_pool_recycle: int = Field(default=1800, gt=0)
    db_pool_timeout: float = Field(default=5.0, gt=0)

    # 普通文本与一期 Markdown 图片包限制。ZIP 同时限制压缩前后大小、条目数、
    # 单图大小/像素及压缩比，避免压缩炸弹和超大图片耗尽内存。
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    max_package_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    max_package_uncompressed_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    max_package_files: int = Field(default=200, ge=2)
    max_image_bytes: int = Field(default=25 * 1024 * 1024, gt=0)
    max_image_pixels: int = Field(default=40_000_000, gt=0)
    max_zip_compression_ratio: float = Field(default=100.0, gt=1)

    # 切分参数（04 §2 DR1；06 评估阶段用网格扫描回调）
    chunk_max_chars: int = 800
    chunk_overlap_chars: int = 80

    # 检索召回量（04 §4.2 DR3：向量 top-20 + 关键词 top-10 → RRF 合并）
    vector_top_k: int = 20
    keyword_top_k: int = 10
    keyword_similarity_threshold: float = Field(default=0.1, gt=0, le=1)
    retrieval_top_k: int = 8          # 交给生成层的候选块数

    # Embedding 通道（04 §3 DR2）：OpenAI 兼容协议，供应商可配；
    # 全项目模型唯一——换模型需全库重向量化 + 一次维度迁移。
    embedding_api_key: str | None = None
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = "BAAI/bge-m3"
    embedding_dimension: int = Field(default=1024, ge=1)
    embedding_timeout_seconds: float = Field(default=30.0, gt=0)
    embedding_batch_size: int = Field(default=32, ge=1)
    embedding_max_retries: int = Field(default=2, ge=0)
    embedding_retry_base_seconds: float = Field(default=0.5, ge=0)

    # LLM 通道（M3 生成层）：OpenAI 兼容协议，供应商可配
    llm_api_key: str | None = None
    llm_base_url: str = "https://api.siliconflow.cn/v1"
    llm_model: str = "deepseek-ai/DeepSeek-V4-Flash"
    llm_timeout_seconds: float = Field(default=60.0, gt=0)

    # 拒答阈值（04 §6 DR6）：候选块最高向量相似度低于 τ → 拒答
    # τ=0.50 由 06 的 v2 多库评估校准（2026-09-20）：库内最低约 0.543、
    # 库外最高约 0.493；取两者之间便于解释的值。
    refusal_similarity_threshold: float = 0.50

    # 会话（决策 D5：进程内存，不落库）；追问改写见 04 DR5
    session_ttl_seconds: float = 1800.0     # 30 分钟无活动即过期
    session_max_turns: int = 5              # 只保留最近 5 轮作为改写上下文

    # 多步工作流（05 §5 AW2）：单次任务的步骤上限
    workflow_max_steps: int = Field(default=5, ge=1)

    @model_validator(mode="after")
    def derive_local_storage_paths(self) -> "Settings":
        self.data_dir = self.data_dir.expanduser().resolve()
        if not self.database_url or not self.database_url.strip():
            self.database_url = sqlite_database_url(
                self.data_dir / DATABASE_FILE_NAME
            )
        if self.storage_dir is None:
            self.storage_dir = self.data_dir / "storage"
        else:
            self.storage_dir = self.storage_dir.expanduser().resolve()
        return self


# 进程内单例：整个应用共享一份配置（import settings 即用）
settings = Settings()
assert settings.database_url is not None
assert settings.storage_dir is not None
