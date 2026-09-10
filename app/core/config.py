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

    # 切分参数（04 §2 DR1；06 评估阶段用网格扫描回调）
    chunk_max_chars: int = 800
    chunk_overlap_chars: int = 80

    # 检索召回量（04 §4.2 DR3：向量 top-20 + 关键词 top-10 → RRF 合并）
    vector_top_k: int = 20
    keyword_top_k: int = 10
    retrieval_top_k: int = 8          # 交给生成层的候选块数

    # Embedding 通道（04 §3 DR2）：OpenAI 兼容协议，供应商可配；
    # 全项目模型唯一——换模型需全库重向量化 + 一次维度迁移。
    embedding_api_key: str | None = None
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimension: int = 1536
    embedding_timeout_seconds: float = 30.0

    # LLM 通道（M3 生成层）：OpenAI 兼容协议，供应商可配
    llm_api_key: str | None = None
    llm_base_url: str = "https://api.siliconflow.cn/v1"
    llm_model: str = "Qwen/Qwen2.5-7B-Instruct"
    llm_timeout_seconds: float = 60.0

    # 拒答阈值（04 §6 DR6）：候选块最高向量相似度低于 τ → 拒答（06 评估校准）
    refusal_similarity_threshold: float = 0.35


# 进程内单例：整个应用共享一份配置（import settings 即用）
settings = Settings()
