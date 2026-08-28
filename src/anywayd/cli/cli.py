from typing import Annotated, List, Optional  # pyright: ignore[reportDeprecated]

import typer

from anywayd.cli.tui import AnywaydApp

app = typer.Typer()


@app.command()
def main(
    command: Annotated[
        Optional[List[str]],  # pyright: ignore[reportDeprecated]
        typer.Argument(help="Command to execute, leave empty to launch tui"),
    ] = None,
):
    if command is not None:
        raise NotImplementedError
    else:
        AnywaydApp().run()

if __name__ == "__main__":
    app()
