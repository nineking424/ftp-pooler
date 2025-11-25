"""Connection configuration parser (rclone-style INI format)."""

import configparser
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


class ConnectionType(str, Enum):
    """Supported connection types."""

    FTP = "ftp"
    LOCAL = "local"


@dataclass
class FTPConnectionConfig:
    """FTP connection configuration."""

    connection_id: str
    type: ConnectionType
    host: str
    port: int = 21
    user: str = "anonymous"
    password: str = ""
    passive: bool = True
    timeout: int = 30

    def __post_init__(self) -> None:
        """Validate configuration after initialization."""
        if not self.host:
            raise ValueError(f"Host is required for FTP connection: {self.connection_id}")


@dataclass
class LocalConnectionConfig:
    """Local filesystem connection configuration."""

    connection_id: str
    type: ConnectionType
    base_path: str = "/"

    def __post_init__(self) -> None:
        """Validate configuration after initialization."""
        path = Path(self.base_path)
        if not path.is_absolute():
            raise ValueError(
                f"base_path must be absolute: {self.base_path} "
                f"for connection: {self.connection_id}"
            )


ConnectionConfig = FTPConnectionConfig | LocalConnectionConfig


class ConnectionRegistry:
    """Registry for managing connection configurations."""

    def __init__(self) -> None:
        """Initialize the connection registry."""
        self._connections: dict[str, ConnectionConfig] = {}

    def register(self, config: ConnectionConfig) -> None:
        """Register a connection configuration.

        Args:
            config: Connection configuration to register.

        Raises:
            ValueError: If connection ID is already registered.
        """
        if config.connection_id in self._connections:
            raise ValueError(
                f"Connection ID already registered: {config.connection_id}"
            )
        self._connections[config.connection_id] = config

    def get(self, connection_id: str) -> ConnectionConfig:
        """Get a connection configuration by ID.

        Args:
            connection_id: The connection ID to look up.

        Returns:
            The connection configuration.

        Raises:
            KeyError: If connection ID is not found.
        """
        if connection_id not in self._connections:
            raise KeyError(f"Connection not found: {connection_id}")
        return self._connections[connection_id]

    def get_ftp(self, connection_id: str) -> FTPConnectionConfig:
        """Get an FTP connection configuration by ID.

        Args:
            connection_id: The connection ID to look up.

        Returns:
            The FTP connection configuration.

        Raises:
            KeyError: If connection ID is not found.
            TypeError: If connection is not an FTP connection.
        """
        config = self.get(connection_id)
        if not isinstance(config, FTPConnectionConfig):
            raise TypeError(
                f"Connection {connection_id} is not an FTP connection"
            )
        return config

    def get_local(self, connection_id: str) -> LocalConnectionConfig:
        """Get a local connection configuration by ID.

        Args:
            connection_id: The connection ID to look up.

        Returns:
            The local connection configuration.

        Raises:
            KeyError: If connection ID is not found.
            TypeError: If connection is not a local connection.
        """
        config = self.get(connection_id)
        if not isinstance(config, LocalConnectionConfig):
            raise TypeError(
                f"Connection {connection_id} is not a local connection"
            )
        return config

    def list_connections(self) -> list[str]:
        """List all registered connection IDs.

        Returns:
            List of connection IDs.
        """
        return list(self._connections.keys())

    def get_all_ftp(self) -> dict[str, FTPConnectionConfig]:
        """Get all FTP connection configurations.

        Returns:
            Dictionary of connection_id to FTPConnectionConfig.
        """
        return {
            cid: config
            for cid, config in self._connections.items()
            if isinstance(config, FTPConnectionConfig)
        }

    def __contains__(self, connection_id: str) -> bool:
        """Check if a connection ID is registered.

        Args:
            connection_id: The connection ID to check.

        Returns:
            True if the connection is registered.
        """
        return connection_id in self._connections

    def __len__(self) -> int:
        """Get the number of registered connections.

        Returns:
            Number of registered connections.
        """
        return len(self._connections)


def _parse_bool(value: str) -> bool:
    """Parse a boolean value from string.

    Args:
        value: String value to parse.

    Returns:
        Boolean value.
    """
    return value.lower() in ("true", "yes", "1", "on")


def _parse_connection_section(
    connection_id: str,
    section: dict[str, str],
) -> ConnectionConfig:
    """Parse a connection configuration section.

    Args:
        connection_id: The connection ID (section name).
        section: Dictionary of configuration values.

    Returns:
        Parsed connection configuration.

    Raises:
        ValueError: If connection type is unknown or configuration is invalid.
    """
    conn_type_str = section.get("type", "").lower()

    try:
        conn_type = ConnectionType(conn_type_str)
    except ValueError:
        raise ValueError(
            f"Unknown connection type '{conn_type_str}' "
            f"for connection: {connection_id}"
        )

    if conn_type == ConnectionType.FTP:
        return FTPConnectionConfig(
            connection_id=connection_id,
            type=conn_type,
            host=section.get("host", ""),
            port=int(section.get("port", "21")),
            user=section.get("user", "anonymous"),
            password=section.get("pass", section.get("password", "")),
            passive=_parse_bool(section.get("passive", "true")),
            timeout=int(section.get("timeout", "30")),
        )
    elif conn_type == ConnectionType.LOCAL:
        return LocalConnectionConfig(
            connection_id=connection_id,
            type=conn_type,
            base_path=section.get("base_path", "/"),
        )
    else:
        raise ValueError(f"Unsupported connection type: {conn_type}")


def load_connections(
    config_path: str | Path,
    registry: Optional[ConnectionRegistry] = None,
) -> ConnectionRegistry:
    """Load connection configurations from an INI file.

    Args:
        config_path: Path to the INI configuration file.
        registry: Optional existing registry to add connections to.

    Returns:
        ConnectionRegistry with loaded configurations.

    Raises:
        FileNotFoundError: If the configuration file does not exist.
        ValueError: If the configuration is invalid.
    """
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Connections file not found: {config_path}")

    if registry is None:
        registry = ConnectionRegistry()

    parser = configparser.ConfigParser()
    parser.read(config_path)

    for section_name in parser.sections():
        section_dict = dict(parser[section_name])
        config = _parse_connection_section(section_name, section_dict)
        registry.register(config)

    return registry
