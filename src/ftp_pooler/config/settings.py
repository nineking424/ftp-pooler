"""Application settings loader."""

from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class KafkaSettings(BaseModel):
    """Kafka configuration."""

    bootstrap_servers: list[str] = Field(default=["localhost:9092"])
    consumer_group: str = Field(default="ftp-pooler")
    input_topic: str = Field(default="ftp-tasks")
    result_topic: str = Field(default="ftp-results")
    fail_topic: str = Field(default="ftp-failures")


class PoolSettings(BaseModel):
    """FTP session pool configuration."""

    max_sessions_per_pod: int = Field(default=100, ge=1)
    max_sessions_per_connection: int = Field(default=10, ge=1)
    session_timeout_seconds: int = Field(default=300, ge=1)


class ApiSettings(BaseModel):
    """REST API configuration."""

    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8080, ge=1, le=65535)


class MetricsSettings(BaseModel):
    """Prometheus metrics configuration."""

    port: int = Field(default=9090, ge=1, le=65535)


class LoggingSettings(BaseModel):
    """Logging configuration."""

    level: str = Field(default="INFO")
    format: str = Field(default="json")


class Settings(BaseSettings):
    """Application settings."""

    kafka: KafkaSettings = Field(default_factory=KafkaSettings)
    pool: PoolSettings = Field(default_factory=PoolSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    metrics: MetricsSettings = Field(default_factory=MetricsSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    # Paths
    config_path: Optional[str] = Field(default=None, env="FTP_POOLER_CONFIG")
    connections_path: Optional[str] = Field(
        default=None, env="FTP_POOLER_CONNECTIONS"
    )

    model_config = {"env_prefix": "FTP_POOLER_"}

    @classmethod
    def from_yaml(cls, config_path: str | Path) -> "Settings":
        """Load settings from YAML file.

        Args:
            config_path: Path to the YAML configuration file.

        Returns:
            Settings instance loaded from the file.
        """
        import os

        config_path = Path(config_path)
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with open(config_path, "r", encoding="utf-8") as f:
            config_data = yaml.safe_load(f) or {}

        # Also check environment variables for paths
        connections_path = os.getenv("FTP_POOLER_CONNECTIONS")
        if connections_path:
            config_data["connections_path"] = connections_path

        return cls(**config_data, config_path=str(config_path))


@lru_cache
def get_settings() -> Settings:
    """Get cached application settings.

    Returns:
        Cached Settings instance.
    """
    import os

    config_path = os.getenv("FTP_POOLER_CONFIG")
    if config_path and Path(config_path).exists():
        return Settings.from_yaml(config_path)

    return Settings()
