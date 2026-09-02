import os

from typing import Annotated, List, Optional, cast  # pyright: ignore[reportDeprecated]

import typer
from rich.console import Console

from anywayd.constants import OBJECT_PATH, SERVICE_NAME

app = typer.Typer()
console = Console()


def _client():
    from anywayd.cli.dbus_client import dbus_client

    return dbus_client(SERVICE_NAME, OBJECT_PATH, SERVICE_NAME)


def _parse_env(
    _ctx: typer.Context,
    _param: typer.CallbackParam,
    values: Optional[List[str]],  # pyright: ignore[reportDeprecated]
) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in values or []:
        if "=" not in item:
            raise typer.BadParameter(f"Expected KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        env[key] = value
    return env


async def _start_process(
    command: str,
    working_dir: str,
    run_as: str,
    env: dict[str, str],
    inherit_env: bool,
) -> None:
    from dbus_fast import Variant

    full_env = dict(os.environ) if inherit_env else {}
    full_env.update(env)
    env_arg = {k: Variant("s", v) for k, v in full_env.items()}

    async with _client() as client:
        try:
            uuid = cast(
                str,
                await client.start_process(  # pyright: ignore[reportAny]
                    command, env_arg, working_dir, run_as
                ),
            )
        except Exception as exc:
            console.print(f"[red]Failed to start process:[/red] {exc}")
            raise typer.Exit(code=1)
    console.print(f"Started process [bold]{uuid}[/]")


@app.command()
def main(
    working_dir: Annotated[
        str, typer.Option("--cwd", "-d", help="Working directory")
    ] = ".",
    run_as: Annotated[
        str, typer.Option("--run-as", "-u", help="User to run as (default: caller)")
    ] = "",
    env: Annotated[
        Optional[List[str]],  # pyright: ignore[reportDeprecated]
        typer.Option(
            "--env",
            "-e",
            help="Environment variable KEY=VALUE (repeatable)",
            callback=_parse_env,
        ),
    ] = None,
    inherit_env: Annotated[
        bool,
        typer.Option(
            "--inherit-env/--no-inherit-env", help="Start from caller's environment"
        ),
    ] = True,
    command: Annotated[
        Optional[str],  # pyright: ignore[reportDeprecated]
        typer.Argument(help="Command to execute, empty to launch TUI"),
    ] = None,
):
    if command:
        import asyncio

        wd = os.path.abspath(working_dir)
        if env is not None:
            assert isinstance(env, dict)
        asyncio.run(_start_process(command, wd, run_as, env or {}, inherit_env))
    else:
        from anywayd.cli.tui import AnywaydApp

        AnywaydApp().run()


if __name__ == "__main__":
    app()
