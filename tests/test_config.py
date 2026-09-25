"""运行默认配置与已提交数据库结构的一致性测试。"""

from pathlib import Path

from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.core.paths import default_user_data_dir, runtime_config_file


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
        "KNOWBASE_DATA_DIR",
        "DATABASE_URL",
        "STORAGE_DIR",
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
    assert make_url(defaults.database_url).get_backend_name() == "sqlite"
    assert Path(make_url(defaults.database_url).database).resolve() == (
        defaults.data_dir / "knowbase.db"
    )
    assert defaults.storage_dir == defaults.data_dir / "storage"


def test_user_data_paths_follow_each_platform_convention():
    home = Path("/users/example")

    assert default_user_data_dir(
        environ={"LOCALAPPDATA": "C:/Users/example/AppData/Local"},
        platform_name="win32",
        home=home,
    ) == Path("C:/Users/example/AppData/Local/KnowBase")
    assert default_user_data_dir(
        environ={}, platform_name="darwin", home=home
    ) == home / "Library" / "Application Support" / "KnowBase"
    assert default_user_data_dir(
        environ={"XDG_DATA_HOME": "/data/example"},
        platform_name="linux",
        home=home,
    ) == Path("/data/example/knowbase")


def test_runtime_config_is_project_local_in_source_and_user_local_when_packaged(
    tmp_path,
):
    windows_env = {"LOCALAPPDATA": str(tmp_path / "Local")}

    assert runtime_config_file(environ={}, packaged=False) == Path(".env")
    assert runtime_config_file(
        environ=windows_env,
        packaged=True,
        platform_name="win32",
        home=tmp_path,
    ) == tmp_path / "Local" / "KnowBase" / "config.env"

    override = tmp_path / "portable" / "settings.env"
    assert runtime_config_file(
        environ={"KNOWBASE_CONFIG_FILE": str(override)},
        packaged=True,
    ) == override.resolve()


def test_data_dir_override_derives_database_and_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("KNOWBASE_DATA_DIR", str(tmp_path / "portable-data"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("STORAGE_DIR", raising=False)

    configured = Settings(_env_file=None)

    assert configured.data_dir == (tmp_path / "portable-data").resolve()
    assert Path(make_url(configured.database_url).database).resolve() == (
        configured.data_dir / "knowbase.db"
    )
    assert configured.storage_dir == configured.data_dir / "storage"


def test_data_dir_override_is_loaded_from_dotenv(monkeypatch, tmp_path):
    monkeypatch.delenv("KNOWBASE_DATA_DIR", raising=False)
    dotenv = tmp_path / ".env"
    expected = tmp_path / "dotenv-data"
    dotenv.write_text(
        f"KNOWBASE_DATA_DIR={expected.as_posix()}\n",
        encoding="utf-8",
    )

    configured = Settings(_env_file=dotenv)

    assert configured.data_dir == expected.resolve()
