"""financebench chat — Phase 1 supports --no-stream only (Phase 2 adds REPL)."""

from __future__ import annotations

import typer

from cli import credentials
from cli.api_client import APIClient, APIError
from cli.render import render_error, render_info, render_response


def chat(
    message: str = typer.Argument(..., help="The query to ask the agent"),
    no_stream: bool = typer.Option(
        False,
        "--no-stream",
        help="Use the non-streaming /v1/chat endpoint. Required in Phase 1; streaming REPL ships in Phase 2.",
    ),
    thread_id: str = typer.Option(None, "--thread-id", help="Continue an existing thread"),
) -> None:
    """Ask the agent a question. Phase 1: --no-stream only."""
    if not no_stream:
        render_error(
            "Phase 1 supports --no-stream only. Streaming REPL ships in Phase 2.\n"
            "Try: financebench chat --no-stream 'your question here'"
        )
        raise typer.Exit(1)

    if credentials.load() is None:
        render_error("Not logged in. Run: financebench login -u analyst")
        raise typer.Exit(1)

    client = APIClient()
    try:
        body: dict = {"message": message}
        if thread_id:
            body["thread_id"] = thread_id
        render_info(f"Querying {client.base_url}/v1/chat ...")
        resp = client.post("/v1/chat", body)
    except APIError as e:
        if e.status_code == 401:
            render_error("Auth expired or invalid. Run: financebench login")
        else:
            render_error(f"Chat failed: {e.message}")
        raise typer.Exit(1)
    finally:
        client.close()

    render_response(
        text=resp.get("response", ""),
        sources=resp.get("sources", []),
        confidence=resp.get("confidence"),
    )
