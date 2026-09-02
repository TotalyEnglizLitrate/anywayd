import asyncio
import logging
import os
import sys

from typing import Annotated

import typer


def _ensure_gil() -> None:
    if hasattr(sys, "_is_gil_enabled") and not sys._is_gil_enabled():
        raise RuntimeError("anywayd does not support free-threaded Python builds")


def _ensure_su():
    if os.geteuid() != 0:
        raise PermissionError("The anywayd daemon needs to be run as root.")


app = typer.Typer()


@app.command()
def main(debug: Annotated[bool, typer.Option(help="Enable debug logging")] = False):
    from rich.traceback import install
    from anywayd.daemon.service import service

    _ = install(max_frames=3)
    _ensure_gil()
    _ensure_su()
    asyncio.run(service(logging.DEBUG if debug else logging.INFO))


if __name__ == "__main__":
    app()
