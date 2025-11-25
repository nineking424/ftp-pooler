"""Tests for configuration module."""

import tempfile
from pathlib import Path

import pytest

from ftp_pooler.config.connections import (
    ConnectionRegistry,
    ConnectionType,
    FTPConnectionConfig,
    LocalConnectionConfig,
    load_connections,
)
from ftp_pooler.config.settings import (
    KafkaSettings,
    PoolSettings,
    Settings,
)


class TestSettings:
    """Tests for Settings class."""

    def test_default_settings(self) -> None:
        """Test default settings values."""
        settings = Settings()

        assert settings.kafka.consumer_group == "ftp-pooler"
        assert settings.kafka.input_topic == "ftp-tasks"
        assert settings.pool.max_sessions_per_pod == 100
        assert settings.pool.max_sessions_per_connection == 10
        assert settings.api.port == 8080
        assert settings.metrics.port == 9090

    def test_kafka_settings(self) -> None:
        """Test Kafka settings."""
        kafka = KafkaSettings(
            bootstrap_servers=["kafka:9092"],
            consumer_group="test-group",
        )

        assert kafka.bootstrap_servers == ["kafka:9092"]
        assert kafka.consumer_group == "test-group"

    def test_pool_settings_validation(self) -> None:
        """Test pool settings validation."""
        pool = PoolSettings(
            max_sessions_per_pod=50,
            max_sessions_per_connection=5,
        )

        assert pool.max_sessions_per_pod == 50
        assert pool.max_sessions_per_connection == 5

    def test_settings_from_yaml(self) -> None:
        """Test loading settings from YAML file."""
        yaml_content = """
kafka:
  bootstrap_servers:
    - kafka-test:9092
  consumer_group: test-pooler
  input_topic: test-tasks

pool:
  max_sessions_per_pod: 50
  max_sessions_per_connection: 5

api:
  port: 9000

logging:
  level: DEBUG
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()

            settings = Settings.from_yaml(f.name)

            assert settings.kafka.bootstrap_servers == ["kafka-test:9092"]
            assert settings.kafka.consumer_group == "test-pooler"
            assert settings.pool.max_sessions_per_pod == 50
            assert settings.api.port == 9000
            assert settings.logging.level == "DEBUG"

        Path(f.name).unlink()


class TestConnectionConfig:
    """Tests for connection configuration."""

    def test_ftp_connection_config(self) -> None:
        """Test FTP connection configuration."""
        config = FTPConnectionConfig(
            connection_id="test-ftp",
            type=ConnectionType.FTP,
            host="ftp.example.com",
            port=21,
            user="testuser",
            password="testpass",
        )

        assert config.connection_id == "test-ftp"
        assert config.type == ConnectionType.FTP
        assert config.host == "ftp.example.com"
        assert config.port == 21
        assert config.user == "testuser"
        assert config.password == "testpass"
        assert config.passive is True

    def test_ftp_connection_requires_host(self) -> None:
        """Test that FTP connection requires host."""
        with pytest.raises(ValueError, match="Host is required"):
            FTPConnectionConfig(
                connection_id="test-ftp",
                type=ConnectionType.FTP,
                host="",
            )

    def test_local_connection_config(self) -> None:
        """Test local connection configuration."""
        config = LocalConnectionConfig(
            connection_id="local",
            type=ConnectionType.LOCAL,
            base_path="/data/storage",
        )

        assert config.connection_id == "local"
        assert config.type == ConnectionType.LOCAL
        assert config.base_path == "/data/storage"

    def test_local_connection_requires_absolute_path(self) -> None:
        """Test that local connection requires absolute path."""
        with pytest.raises(ValueError, match="base_path must be absolute"):
            LocalConnectionConfig(
                connection_id="local",
                type=ConnectionType.LOCAL,
                base_path="relative/path",
            )


class TestConnectionRegistry:
    """Tests for ConnectionRegistry."""

    def test_register_and_get(self) -> None:
        """Test registering and retrieving connections."""
        registry = ConnectionRegistry()

        ftp_config = FTPConnectionConfig(
            connection_id="test-ftp",
            type=ConnectionType.FTP,
            host="ftp.example.com",
        )
        registry.register(ftp_config)

        retrieved = registry.get("test-ftp")
        assert retrieved == ftp_config

    def test_get_ftp(self) -> None:
        """Test getting FTP connection."""
        registry = ConnectionRegistry()

        ftp_config = FTPConnectionConfig(
            connection_id="test-ftp",
            type=ConnectionType.FTP,
            host="ftp.example.com",
        )
        registry.register(ftp_config)

        retrieved = registry.get_ftp("test-ftp")
        assert isinstance(retrieved, FTPConnectionConfig)

    def test_get_local(self) -> None:
        """Test getting local connection."""
        registry = ConnectionRegistry()

        local_config = LocalConnectionConfig(
            connection_id="local",
            type=ConnectionType.LOCAL,
            base_path="/data",
        )
        registry.register(local_config)

        retrieved = registry.get_local("local")
        assert isinstance(retrieved, LocalConnectionConfig)

    def test_duplicate_registration_raises(self) -> None:
        """Test that duplicate registration raises error."""
        registry = ConnectionRegistry()

        config = FTPConnectionConfig(
            connection_id="test-ftp",
            type=ConnectionType.FTP,
            host="ftp.example.com",
        )
        registry.register(config)

        with pytest.raises(ValueError, match="already registered"):
            registry.register(config)

    def test_get_nonexistent_raises(self) -> None:
        """Test that getting nonexistent connection raises error."""
        registry = ConnectionRegistry()

        with pytest.raises(KeyError, match="Connection not found"):
            registry.get("nonexistent")

    def test_list_connections(self) -> None:
        """Test listing connections."""
        registry = ConnectionRegistry()

        registry.register(
            FTPConnectionConfig(
                connection_id="ftp1",
                type=ConnectionType.FTP,
                host="ftp1.example.com",
            )
        )
        registry.register(
            FTPConnectionConfig(
                connection_id="ftp2",
                type=ConnectionType.FTP,
                host="ftp2.example.com",
            )
        )

        connections = registry.list_connections()
        assert len(connections) == 2
        assert "ftp1" in connections
        assert "ftp2" in connections


class TestLoadConnections:
    """Tests for load_connections function."""

    def test_load_connections_from_ini(self) -> None:
        """Test loading connections from INI file."""
        ini_content = """
[remote-server]
type = ftp
host = ftp.example.com
port = 21
user = testuser
pass = testpass
passive = true

[local]
type = local
base_path = /data/storage
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ini", delete=False) as f:
            f.write(ini_content)
            f.flush()

            registry = load_connections(f.name)

            assert len(registry) == 2
            assert "remote-server" in registry
            assert "local" in registry

            ftp = registry.get_ftp("remote-server")
            assert ftp.host == "ftp.example.com"
            assert ftp.user == "testuser"

            local = registry.get_local("local")
            assert local.base_path == "/data/storage"

        Path(f.name).unlink()

    def test_load_nonexistent_file_raises(self) -> None:
        """Test that loading nonexistent file raises error."""
        with pytest.raises(FileNotFoundError):
            load_connections("/nonexistent/path.ini")
