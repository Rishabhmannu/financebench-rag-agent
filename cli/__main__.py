"""financebench CLI entrypoint. Bound to `financebench` via pyproject.toml scripts."""

from __future__ import annotations

import typer

from cli import __version__
from cli.commands.approvals import app as approvals_app
from cli.commands.chat import chat
from cli.commands.doctor import doctor
from cli.commands.down import down
from cli.commands.login import login
from cli.commands.logout import logout
from cli.commands.logs import logs
from cli.commands.seed import seed
from cli.commands.setup import setup
from cli.commands.status import status
from cli.commands.threads import app as threads_app
from cli.commands.upgrade import upgrade

app = typer.Typer(
    name="financebench",
    help="CLI client for the FinanceBench RAG Agent.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)

app.command(name="setup")(setup)
app.command(name="seed")(seed)
app.command(name="login")(login)
app.command(name="chat")(chat)
app.command(name="logout")(logout)
app.command(name="logs")(logs)
app.command(name="status")(status)
app.command(name="upgrade")(upgrade)
app.command(name="down")(down)
app.command(name="doctor")(doctor)
app.add_typer(threads_app, name="threads")
app.add_typer(approvals_app, name="approvals")


@app.command(name="version")
def version_cmd() -> None:
    """Print the CLI version."""
    typer.echo(__version__)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
