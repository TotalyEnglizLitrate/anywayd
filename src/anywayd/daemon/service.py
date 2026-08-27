import contextvars
import os
import pwd
import signal

from typing import Annotated, cast, final, override
from datetime import datetime, UTC

from dbus_fast.annotations import DBusBool, DBusSignature, DBusStr, DBusDict, DBusUInt32
from dbus_fast.constants import BusType, MessageType, NameFlag, RequestNameReply
from dbus_fast.service import ServiceInterface, dbus_method
from dbus_fast.aio.message_bus import MessageBus
from dbus_fast.message import Message


from anywayd.daemon.models import Process

from anywayd.daemon.process_manager import ProcessManager, process_manager

SERVICE_NAME = "com.anywayd.daemon"
OBJECT_PATH = "/com/anywayd/daemon"
DB_PATH = "/var/lib/anywayd/anywayd.db"
DB_DIR = "/var/lib/anywayd"

curr_msg: contextvars.ContextVar[Message] = contextvars.ContextVar("message")


class AnywaydMessageBus(MessageBus):
    @override
    def _process_message(self, msg: Message) -> None:
        if msg.message_type is MessageType.METHOD_CALL and msg.path == OBJECT_PATH:
            token = curr_msg.set(msg)
            try:
                super()._process_message(msg)
            finally:
                curr_msg.reset(token)
        else:
            super()._process_message(msg)


@final
class AnywaydService(ServiceInterface):
    def __init__(
        self,
        process_manager: ProcessManager,
    ):
        super().__init__(SERVICE_NAME)
        self.process_manager = process_manager
        self._start_time = datetime.now(UTC)

    @dbus_method()
    async def Echo(self, what: DBusStr) -> DBusStr:
        """Echo back the input - simple test method"""
        return what

    @dbus_method()
    async def StartProcess(
        self,
        command: DBusStr,
        env: DBusDict,
        shell: DBusStr,
        working_dir: DBusStr,
        run_as: DBusStr = "",
    ) -> DBusStr:
        """
        Start a background process.

        Args:
            command: The command to run (full command line string)
            run_as: User to run as (username or UID), empty for current user
            shell: Which Shell to run the command via, empty for nothing
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
    async def GetVersion(self) -> DBusStr:
        """Get daemon version."""
        return "0.1.0"

    @dbus_method()
    async def GetCallerInfo(self) -> DBusUInt32:
        """
        Debug method to get caller information.
        Useful for testing credential retrieval.
        """
        return (await self.get_caller_info(curr_msg.get())).pw_uid

    async def get_caller_info(self, msg: Message) -> pwd.struct_passwd:
        bus = await MessageBus(
            bus_type=BusType.SYSTEM, negotiate_unix_fd=True
        ).connect()
        try:
            creds = await bus.call(
                Message(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus",
                    member="GetConnectionCredentials",
                    signature="s",
                    body=[msg.sender],
                )
            )
        finally:
            bus.disconnect()

        uid: int = cast(int, creds.body[0]["UnixUserID"].value)
        return pwd.getpwuid(uid)


    def _format_process_status(self, status: Process) -> DBusDict:
        """Format process status for D-Bus response."""
        raise NotImplementedError


async def service():
    os.makedirs(DB_DIR, exist_ok=True)
    async with process_manager(f"sqlite+aiosqlite:///{DB_PATH}") as pm:
        bus = await AnywaydMessageBus(bus_type=BusType.SYSTEM).connect()
        service = AnywaydService(pm)
        bus.export(OBJECT_PATH, service)
        name_resp = await bus.request_name(SERVICE_NAME, flags=NameFlag.DO_NOT_QUEUE)

        if name_resp == RequestNameReply.EXISTS:
            raise RuntimeError(
                f"Unable to acquire {SERVICE_NAME=}, name already in use."
            )

        print(f"Anywayd D-Bus service running on {OBJECT_PATH}")
        print(f"Database: {DB_PATH}")
        print(f"PID: {os.getpid()}")

        try:
            await bus.wait_for_disconnect()
        except:
            from rich.traceback import install

            _ = install()
            raise
        finally:
            print("Shutdown complete")
