"""financebench CLI entrypoint. Bound to `financebench` via pyproject.toml scripts."""

from __future__ import annotations

import typer

from cli import __version__
from cli.commands.chat import chat
from cli.commands.login import login
from cli.commands.logout import logout
from cli.commands.status import status
from cli.commands.threads import app as threads_app

app = typer.Typer(
    name="financebench",
    help="CLI client for the FinanceBench RAG Agent.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)

app.command(name="login")(login)
app.command(name="chat")(chat)
app.command(name="logout")(logout)
app.command(name="status")(status)
app.add_typer(threads_app, name="threads")


@app.command(name="version")
def version_cmd() -> None:
    """Print the CLI version."""
    typer.echo(__version__)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
