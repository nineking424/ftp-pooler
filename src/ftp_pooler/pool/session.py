"""FTP session wrapper for async FTP operations."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import AsyncIterator, Optional
from pathlib import Path

import aioftp

from ftp_pooler.config.connections import FTPConnectionConfig


class SessionState(str, Enum):
    """FTP session state."""

    IDLE = "idle"
    BUSY = "busy"
    DISCONNECTED = "disconnected"
    ERROR = "error"


@dataclass
class FTPSession:
    """Wrapper for an async FTP session.

    Manages a single FTP connection with state tracking and automatic
    reconnection capabilities.
    """

    config: FTPConnectionConfig
    session_id: str
    _client: Optional[aioftp.Client] = field(default=None, repr=False)
    _state: SessionState = field(default=SessionState.DISCONNECTED)
    _created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    _last_used_at: Optional[datetime] = field(default=None)
    _error_count: int = field(default=0)

    @property
    def state(self) -> SessionState:
        """Get current session state."""
        return self._state

    @property
    def is_connected(self) -> bool:
        """Check if session is connected."""
        return self._state in (SessionState.IDLE, SessionState.BUSY)

    @property
    def is_available(self) -> bool:
        """Check if session is available for use."""
        return self._state == SessionState.IDLE

    @property
    def created_at(self) -> datetime:
        """Get session creation time."""
        return self._created_at

    @property
    def last_used_at(self) -> Optional[datetime]:
        """Get last usage time."""
        return self._last_used_at

    @property
    def error_count(self) -> int:
        """Get error count."""
        return self._error_count

    async def connect(self) -> None:
        """Connect to the FTP server.

        Raises:
            ConnectionError: If connection fails.
        """
        if self.is_connected:
            return

        try:
            self._client = aioftp.Client()
            await self._client.connect(
                host=self.config.host,
                port=self.config.port,
            )
            await self._client.login(
                user=self.config.user,
                password=self.config.password,
            )
            self._state = SessionState.IDLE
            self._error_count = 0
        except Exception as e:
            self._state = SessionState.ERROR
            self._error_count += 1
            self._client = None
            raise ConnectionError(
                f"Failed to connect to {self.config.host}:{self.config.port}: {e}"
            ) from e

    async def disconnect(self) -> None:
        """Disconnect from the FTP server."""
        if self._client is not None:
            try:
                await self._client.quit()
            except Exception:
                pass
            finally:
                self._client = None
                self._state = SessionState.DISCONNECTED

    async def download(
        self,
        remote_path: str,
        local_path: str | Path,
    ) -> int:
        """Download a file from the FTP server.

        Args:
            remote_path: Path to the file on the FTP server.
            local_path: Local path to save the file.

        Returns:
            Number of bytes downloaded.

        Raises:
            RuntimeError: If session is not connected.
            IOError: If download fails.
        """
        if not self.is_connected or self._client is None:
            raise RuntimeError("Session is not connected")

        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)

        self._state = SessionState.BUSY
        self._last_used_at = datetime.now(timezone.utc)
        bytes_downloaded = 0

        try:
            async with self._client.download_stream(remote_path) as stream:
                with open(local_path, "wb") as f:
                    async for block in stream.iter_by_block():
                        f.write(block)
                        bytes_downloaded += len(block)

            self._state = SessionState.IDLE
            return bytes_downloaded

        except Exception as e:
            self._state = SessionState.ERROR
            self._error_count += 1
            raise IOError(f"Download failed: {remote_path} -> {local_path}: {e}") from e

    async def upload(
        self,
        local_path: str | Path,
        remote_path: str,
    ) -> int:
        """Upload a file to the FTP server.

        Args:
            local_path: Local path of the file to upload.
            remote_path: Path on the FTP server to save the file.

        Returns:
            Number of bytes uploaded.

        Raises:
            RuntimeError: If session is not connected.
            FileNotFoundError: If local file does not exist.
            IOError: If upload fails.
        """
        if not self.is_connected or self._client is None:
            raise RuntimeError("Session is not connected")

        local_path = Path(local_path)
        if not local_path.exists():
            raise FileNotFoundError(f"Local file not found: {local_path}")

        self._state = SessionState.BUSY
        self._last_used_at = datetime.now(timezone.utc)
        bytes_uploaded = 0

        try:
            # Ensure remote directory exists
            remote_dir = str(Path(remote_path).parent)
            if remote_dir and remote_dir != "/":
                try:
                    await self._client.make_directory(remote_dir)
                except Exception:
                    pass  # Directory may already exist

            async with self._client.upload_stream(remote_path) as stream:
                with open(local_path, "rb") as f:
                    while True:
                        chunk = f.read(65536)  # 64KB chunks
                        if not chunk:
                            break
                        await stream.write(chunk)
                        bytes_uploaded += len(chunk)

            self._state = SessionState.IDLE
            return bytes_uploaded

        except Exception as e:
            self._state = SessionState.ERROR
            self._error_count += 1
            raise IOError(f"Upload failed: {local_path} -> {remote_path}: {e}") from e

    async def check_health(self) -> bool:
        """Check if the session is healthy.

        Returns:
            True if session is healthy, False otherwise.
        """
        if not self.is_connected or self._client is None:
            return False

        try:
            # Try to get current directory as a health check
            await self._client.get_current_directory()
            return True
        except Exception:
            self._state = SessionState.ERROR
            return False

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator["FTPSession"]:
        """Context manager to acquire and release the session.

        Yields:
            The FTP session.

        Raises:
            RuntimeError: If session is not available.
        """
        if not self.is_available:
            raise RuntimeError(f"Session {self.session_id} is not available")

        self._state = SessionState.BUSY
        try:
            yield self
        finally:
            if self._state == SessionState.BUSY:
                self._state = SessionState.IDLE

    def to_dict(self) -> dict:
        """Convert session info to dictionary.

        Returns:
            Dictionary with session information.
        """
        return {
            "session_id": self.session_id,
            "connection_id": self.config.connection_id,
            "host": self.config.host,
            "state": self._state.value,
            "created_at": self._created_at.isoformat(),
            "last_used_at": self._last_used_at.isoformat() if self._last_used_at else None,
            "error_count": self._error_count,
        }
