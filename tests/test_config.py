"""运行默认配置与已提交数据库结构的一致性测试。"""

from app.core.config import Settings


def test_default_model_configuration_matches_documented_runtime(monkeypatch):
    """未提供模型环境变量时，默认值匹配数据库结构与运行文档。"""
    for name in (
        "EMBEDDING_BASE_URL",
        "EMBEDDING_MODEL",
        "EMBEDDING_DIMENSION",
        "EMBEDDING_BATCH_SIZE",
        "EMBEDDING_MAX_RETRIES",
        "EMBEDDING_RETRY_BASE_SECONDS",
        "LLM_BASE_URL",
        "LLM_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    defaults = Settings(_env_file=None)

    assert defaults.embedding_base_url == "https://api.siliconflow.cn/v1"
    assert defaults.embedding_model == "BAAI/bge-m3"
    assert defaults.embedding_dimension == 1024
    assert defaults.embedding_batch_size == 32
    assert defaults.embedding_max_retries == 2
    assert defaults.embedding_retry_base_seconds == 0.5
    assert defaults.llm_base_url == "https://api.siliconflow.cn/v1"
    assert defaults.llm_model == "deepseek-ai/DeepSeek-V4-Flash"
