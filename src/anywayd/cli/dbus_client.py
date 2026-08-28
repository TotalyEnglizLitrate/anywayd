import re

from contextlib import asynccontextmanager
from typing import Any, final

from dbus_fast.aio import MessageBus
from dbus_fast.aio.proxy_object import ProxyInterface
from dbus_fast.constants import BusType


def _to_snake_case(name: str) -> str:
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.lower()


@final
class DBusClient:
    def __init__(self, interface: ProxyInterface) -> None:
        self._interface = interface

    def __getattr__(  # pyright: ignore[reportAny]
        self, name: str
    ) -> Any:  # pyright: ignore[reportExplicitAny]
        call_name = f"call_{_to_snake_case(name)}"
        return getattr(self._interface, call_name)  # pyright: ignore[reportAny]


@asynccontextmanager
async def dbus_client(
    service_name: str,
    object_path: str,
    interface_name: str,
    bus_type: BusType = BusType.SYSTEM,
):
    """Connect to a DBus service and yield a client for calling its methods.

    Usage:
        async with dbus_client(
            "com.anywayd.daemon",
            "/com/anywayd/daemon",
            "com.anywayd.daemon",
        ) as client:
            devices = await client.<call_method_in_snake_case>()
    """
    bus = await MessageBus(bus_type=bus_type).connect()
    try:
        introspection = await bus.introspect(service_name, object_path)
        proxy = bus.get_proxy_object(service_name, object_path, introspection)
        interface = proxy.get_interface(interface_name)
        yield DBusClient(interface)
    finally:
        bus.disconnect()
