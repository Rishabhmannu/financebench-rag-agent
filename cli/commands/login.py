"""financebench login — interactive password prompt, store JWT."""

from __future__ import annotations

from getpass import getpass

import typer

from cli import credentials
from cli.api_client import DEFAULT_BASE_URL, APIClient, APIError
from cli.render import render_error, render_success


def login(
    user: str = typer.Option(None, "--user", "-u", help="Username (will be prompted if not given)"),
    base_url: str = typer.Option(
        DEFAULT_BASE_URL, "--base-url", help="Backend API base URL (default: http://localhost:8000)"
    ),
) -> None:
    """Log in and store JWT for subsequent commands."""
    if not user:
        user = typer.prompt("Username")
    password = getpass("Password: ")

    client = APIClient(base_url=base_url, token=None)
    try:
        resp = client.post(
            "/v1/auth/login",
            {"username": user, "password": password},
            auth_required=False,
        )
    except APIError as e:
        render_error(f"Login failed: {e.message}")
        raise typer.Exit(1)
    finally:
        client.close()

    token = resp.get("access_token")
    if not token:
        render_error(f"Login response missing access_token: {resp}")
        raise typer.Exit(1)

    user_id = user
    role = "?"
    me_client = APIClient(base_url=base_url, token=token)
    try:
        me = me_client.get("/v1/auth/me")
        user_id = me.get("user_id", user)
        role = me.get("role", "?")
    except APIError:
        pass
    finally:
        me_client.close()

    credentials.save(token=token, user_id=user_id, base_url=base_url)
    render_success(f"Logged in as {user_id} (role={role}) -> {base_url}")
