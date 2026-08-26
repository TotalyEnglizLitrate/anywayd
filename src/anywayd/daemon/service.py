import asyncio

from typing import Annotated

from dbus_fast.annotations import DBusBool, DBusSignature, DBusStr, DBusDict, DBusUInt32
from dbus_fast.constants import NameFlag
from dbus_fast.service import ServiceInterface, dbus_method
from dbus_fast import RequestNameReply
from dbus_fast.aio import MessageBus

SERVICE_NAME = "com.anywayd.daemon"
OBJECT_PATH = "/com/anywayd/daemon"


class AnywaydService(ServiceInterface):
    def __init__(self):
        super().__init__(SERVICE_NAME)

    @dbus_method()
    def Echo(self, what: DBusStr) -> DBusStr:
        return what

    @dbus_method()
    async def StartProcess(
        self, command: DBusStr, run_as: DBusStr, shell: DBusStr, env: DBusDict
    ) -> DBusBool:
        raise NotImplementedError("idk")

    @dbus_method()
    async def StopProcess(self, pid: DBusUInt32) -> DBusBool:
        raise NotImplementedError("idk")

    @dbus_method()
    async def GetProcesses(
        self, pids: Annotated[list[int], DBusSignature("au")]
    ) -> DBusDict:
        raise NotImplementedError("idk")


async def main():
    bus = await MessageBus().connect()
    interface = AnywaydService()
    bus.export(OBJECT_PATH, interface)
    name_resp = await bus.request_name(SERVICE_NAME, flags=NameFlag.DO_NOT_QUEUE)
    if name_resp == RequestNameReply.EXISTS:
        raise RuntimeError(
            f"Unable to acquire {SERVICE_NAME=}, name already in use. (is anywayd already running?)"
        )

    await bus.wait_for_disconnect()


if __name__ == "__main__":
    asyncio.run(main())
