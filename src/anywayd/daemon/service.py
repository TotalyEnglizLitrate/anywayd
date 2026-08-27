import asyncio
import os
import signal
from typing import Annotated, final
from datetime import datetime, UTC

from dbus_fast.annotations import DBusBool, DBusSignature, DBusStr, DBusDict, DBusUInt32
from dbus_fast.constants import NameFlag
from dbus_fast.service import ServiceInterface, dbus_method
from dbus_fast import RequestNameReply
from dbus_fast.aio import MessageBus
from dbus_fast.message import Message

from anywayd.daemon.models import Process

from anywayd.daemon.process_manager import ProcessManager

SERVICE_NAME = "com.anywayd.daemon"
OBJECT_PATH = "/com/anywayd/daemon"
DB_URL = "sqlite+aiosqlite:///var/lib/anywayd/anywayd.db"

@final
class AnywaydService(ServiceInterface):
    def __init__(self, process_manager: ProcessManager):
        super().__init__(SERVICE_NAME)
        self.process_manager = process_manager
        self._start_time = datetime.now(UTC)

    def _get_caller_credentials(self, message: Message) -> tuple[int, int, str]:
        """
        Get caller's UID, GID, and PID from D-Bus message.
        """
        raise NotImplementedError

    @dbus_method()
    def Echo(self, what: DBusStr) -> DBusStr:
        """Echo back the input - simple test method"""
        return what

    @dbus_method()
    async def StartProcess(
        self,
        command: DBusStr,
        run_as: DBusStr = "",
        shell: DBusBool = False,
        env: DBusDict | None = None,
        working_dir: DBusStr = "",
    ) -> DBusStr:
        """
        Start a background process.

        Args:
            command: The command to run (full command line string)
            run_as: User to run as (username or UID), empty for current user
            shell: Whether to run through shell
            env: Environment variables as dict {key: value}
            working_dir: Working directory for the process

        Returns:
            UUID of the started process
        """
        raise NotImplementedError

    @dbus_method()
    async def StopProcess(
        self, uuid: DBusStr, signal_num: DBusUInt32 = signal.SIGTERM
    ) -> DBusBool:
        """
        Stop a running process by UUID.

        Args:
            uuid: Process UUID
            signal_num: Signal to send (default: SIGTERM)

        Returns:
            True if process was stopped, False if not found
        """
        raise NotImplementedError

    @dbus_method()
    async def GetProcesses(
        self, uuids: Annotated[list[str], DBusSignature("as")]
    ) -> DBusDict:
        """
        Get information about processes.

        Args:
            uuids: List of process UUIDs (empty list returns all)

        Returns:
            Dictionary mapping UUID to process info
        """
        raise NotImplementedError

    @dbus_method()
    async def GetProcessByPID(self, pid: DBusUInt32) -> DBusDict:
        """
        Get process information by PID.

        Args:
            pid: Process ID

        Returns:
            Process information dictionary
        """
        raise NotImplementedError

    @dbus_method()
    async def ListAllProcesses(self, limit: DBusUInt32 = 100) -> DBusDict:
        """
        List all tracked processes.

        Args:
            limit: Maximum number of processes to return

        Returns:
            Dictionary mapping UUID to process info
        """
        raise NotImplementedError

    @dbus_method()
    async def CleanupFinished(self) -> DBusBool:
        """Clean up references to finished processes."""
        raise NotImplementedError

    @dbus_method()
    async def GetVersion(self) -> DBusStr:
        """Get daemon version."""
        return "0.1.0"

    @dbus_method()
    async def GetStats(self) -> DBusDict:
        """Get daemon statistics."""
        raise NotImplementedError

    @dbus_method()
    async def GetCallerInfo(self) -> DBusDict:
        """
        Debug method to get caller information.
        Useful for testing credential retrieval.
        """
        raise NotImplementedError

    def _format_process_status(self, status: Process) -> DBusDict:
        """Format process status for D-Bus response."""
        raise NotImplementedError


async def service():
    """Main entry point for the D-Bus service."""
    process_manager = ProcessManager(DB_URL)
    await process_manager.initialize()
    bus = await MessageBus().connect()
    service = AnywaydService(process_manager)
    bus.export(OBJECT_PATH, service)
    name_resp = await bus.request_name(
        SERVICE_NAME, flags=NameFlag.DO_NOT_QUEUE | NameFlag.ALLOW_REPLACEMENT
    )

    if name_resp == RequestNameReply.EXISTS:
        raise RuntimeError(f"Unable to acquire {SERVICE_NAME=}, name already in use.")

    print(f"Anywayd D-Bus service running on {OBJECT_PATH}")
    print(f"Database: {DB_URL}")
    print(f"PID: {os.getpid()}")

    try:
        await bus.wait_for_disconnect()
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nShutting down...")
    finally:
        await process_manager.close()
        print("Shutdown complete")
