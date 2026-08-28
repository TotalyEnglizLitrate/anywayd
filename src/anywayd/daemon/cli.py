import asyncio
import logging
import os
from typing import Annotated

import typer

from rich.traceback import install

from anywayd.daemon.service import service


def _ensure_su():
    if os.geteuid() != 0:
        raise PermissionError("The anywayd daemon needs to be run as root.")

app = typer.Typer()

@app.command()
def main(debug: Annotated[bool, typer.Option(help="Enable debug logging")] = False):
    _ = install(max_frames=3)
    _ensure_su()
    asyncio.run(service(logging.DEBUG if debug else logging.INFO))


if __name__ == "__main__":
    app()
