"""运行默认配置与已提交数据库结构的一致性测试。"""

from app.core.config import Settings


def test_default_embedding_configuration_matches_current_schema(monkeypatch):
    """未提供 embedding 环境变量时，默认维度必须匹配迁移后的 vector(1024)。"""
    for name in (
        "EMBEDDING_BASE_URL",
        "EMBEDDING_MODEL",
        "EMBEDDING_DIMENSION",
    ):
        monkeypatch.delenv(name, raising=False)

    defaults = Settings(_env_file=None)

    assert defaults.embedding_base_url == "https://api.siliconflow.cn/v1"
    assert defaults.embedding_model == "BAAI/bge-m3"
    assert defaults.embedding_dimension == 1024
