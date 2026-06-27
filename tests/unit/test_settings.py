from pathlib import Path

from quant_system.settings import PROJECT_ROOT, Settings


def test_relative_data_directory_is_rooted_at_project() -> None:
    settings = Settings(data_dir=Path("runtime-data"))

    assert settings.resolved_data_dir == PROJECT_ROOT / "runtime-data"


def test_public_config_exposes_only_runtime_values() -> None:
    settings = Settings(
        app_env="development",
        data_dir=Path("data"),
        log_level="INFO",
    )

    assert settings.public_config() == {
        "app_env": "development",
        "data_dir": str(PROJECT_ROOT / "data"),
        "log_level": "INFO",
    }
