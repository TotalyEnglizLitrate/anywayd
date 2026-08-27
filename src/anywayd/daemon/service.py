import contextvars
import os
import pwd
import shlex
import signal

from typing import Annotated, cast, final, override
from datetime import datetime, UTC
from uuid import UUID

from dbus_fast import DBusError, Variant
from dbus_fast.annotations import DBusBool, DBusSignature, DBusStr, DBusDict, DBusUInt32
from dbus_fast.constants import BusType, MessageType, NameFlag, RequestNameReply
from dbus_fast.service import ServiceInterface, dbus_method
from dbus_fast.aio.message_bus import MessageBus
from dbus_fast.message import Message
import psutil


from anywayd.daemon.models import Process
from anywayd.daemon.process_manager import ProcessManager, process_manager
from anywayd import __version__

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
    async def StartProcess(
        self,
        command: DBusStr,
        env: DBusDict,
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
        if run_as:
            try:
                _ = pwd.getpwnam(run_as)
            except KeyError:
                run_as = ""

        user = (await self.get_caller_info(curr_msg.get())).pw_name
        cmd = shlex.split(command)
        return str(
            await self.process_manager.spawn(
                command=cmd,
                invoked_by_user=user,
                run_as_user=run_as or user,
                env={k: cast(str, v.value) for (k, v) in env.items()},
                cwd=working_dir,
            )
        )

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
            True if process was stopped, Errors if process doesn't exist/not invoked by user
        """
        user = (await self.get_caller_info(curr_msg.get())).pw_name
        try:
            ret = await self.process_manager.kill(UUID(uuid), user, signal_num)
        except psutil.NoSuchProcess:
            self.process_not_found(user, uuid=UUID(uuid))

        if not ret:
            raise DBusError(
                "com.anywayd.KillFailed", f"Failed to kill process with {uuid=}"
            )

        return True

    @dbus_method()
    async def GetProcesses(
        self, uuids: Annotated[list[str], DBusSignature("as")]
    ) -> Annotated[list[DBusDict], DBusSignature("a{sv}")]:
        """
        Get information about processes.

        Args:
            uuids: List of process UUIDs

        Returns:
            List of Dicts representing a managed Process
        """
        user = (await self.get_caller_info(curr_msg.get())).pw_name
        procs = await self.process_manager.get_processes_by_uuid(
            set(UUID(uuid) for uuid in uuids), user
        )
        return [self._format_process(proc) for proc in procs]

    @dbus_method()
    async def GetProcessByPID(self, pid: DBusUInt32) -> DBusDict:
        """
        Get process information by PID.

        Args:
            pid: Process ID

        Returns:
            Process information dictionary
        """
        user = (await self.get_caller_info(curr_msg.get())).pw_name
        process = await self.process_manager.get_process_by_pid(pid, user)
        if process is None:
            self.process_not_found(user, pid=pid)
        return self._format_process(process)

    @dbus_method()
    async def GetProcessesByUser(self, limit: DBusUInt32 = 100) -> Annotated[list[DBusDict], DBusSignature("a{sv}")]:
        """
        List all tracked processes.

        Args:
            limit: Maximum number of processes to return

        Returns:
            List of Dicts representing managed Processes
        """
        user = (await self.get_caller_info(curr_msg.get())).pw_name
        procs = await self.process_manager.get_process_by_user(user, limit)
        return [self._format_process(proc) for proc in procs]


    @dbus_method()
    async def GetStats(self) -> DBusDict:
        """
        List Process stats concerning calling user
        """
        user = (await self.get_caller_info(curr_msg.get())).pw_name
        return {
            k: Variant("x", v)
            for (k, v) in (await self.process_manager.get_stats(user)).items()
        }

    @dbus_method()
    async def GetVersion(self) -> DBusStr:
        """Get daemon version."""
        return __version__

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

    def _format_process(self, proc: Process) -> DBusDict:
        """Format process status for D-Bus response."""
        return {
            "uuid": Variant("s", str(proc.uuid)),
            "pid": Variant("x", proc.pid if proc.pid is not None else -1),
            "command": Variant("s", proc.command),
            "env": Variant("s", proc.env),
            "cwd": Variant("s", proc.cwd),
            "invoked_by_user": Variant("s", proc.invoked_by_user),
            "run_as_user": Variant("s", proc.run_as_user),
            "started_at": Variant("d", proc.started_at.timestamp()),
            "ended_at": Variant(
                "d", proc.ended_at.timestamp() if proc.ended_at is not None else -1.0
            ),
            "exit_code": Variant(
                "x", proc.exit_code if proc.exit_code is not None else -1
            ),
            "boot_id": Variant("s", str(proc.boot_id)),
        }

    def process_not_found(
        self, user: str, pid: int | None = None, uuid: UUID | None = None
    ):
        if (pid, uuid) == (None, None):
            raise ValueError("One of uuid, pid must be specified")
        identifier = f"PID={pid}" if pid is not None else f"UUID={uuid}"
        raise DBusError(
            "com.anywayd.Error.ProcessNotFound",
            f"Process with {identifier} not found or not owned by {user=}",
        )


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
