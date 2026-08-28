import asyncio
import os

from rich.traceback import install

from anywayd.daemon.service import service


def _ensure_su():
    if os.geteuid() != 0:
        raise PermissionError("The anywayd daemon needs to be run as root.")


def main():
    _ = install(max_frames=3)
    _ensure_su()
    asyncio.run(service())


if __name__ == "__main__":
    main()
