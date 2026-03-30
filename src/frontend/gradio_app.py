"""Gradio ChatInterface connected to FastAPI backend with SSE streaming."""

import json
import os

import gradio as gr
import httpx

API_URL = os.environ.get("API_URL", "http://localhost:8000")


def login(username: str, password: str) -> str | None:
    """Login and get JWT token."""
    try:
        response = httpx.post(f"{API_URL}/auth/login", json={"username": username, "password": password})
        if response.status_code == 200:
            return response.json()["access_token"]
        return None
    except Exception:
        return None


def _format_sources(sources: list) -> str:
    """Format source citations as markdown."""
    if not sources:
        return ""
    lines = "\n\n---\n**Sources:**\n"
    for s in sources:
        lines += f"- {s.get('file', 'Unknown')} (Page {s.get('page', '?')})\n"
    return lines


async def chat(message: str, history: list, token: str):
    """Stream response from RAG agent via SSE endpoint."""
    if not token:
        yield "Please login first using the Login tab."
        return

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                f"{API_URL}/chat/stream",
                json={"message": message},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "text/event-stream",
                },
            ) as response:
                if response.status_code == 401:
                    yield "Session expired. Please login again."
                    return
                if response.status_code != 200:
                    yield f"Server error (HTTP {response.status_code}). Please try again."
                    return

                partial_answer = ""
                status_line = ""

                async for line in response.aiter_lines():
                    # SSE format: "data: {...}"
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if not payload:
                        continue

                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue

                    event_type = event.get("type", "")

                    if event_type == "node_start":
                        status_line = f"*{event.get('label', '')}...*\n\n"
                        yield status_line + partial_answer

                    elif event_type == "token":
                        partial_answer += event.get("content", "")
                        yield status_line + partial_answer

                    elif event_type == "hitl_interrupt":
                        thread_id = event.get("thread_id", "")
                        reason = event.get("reason", "Approval required")
                        preview = event.get("answer_preview", partial_answer)
                        yield (
                            f"**Approval Required**\n\n"
                            f"{reason}\n\n"
                            f"**Answer preview:**\n{preview}\n\n"
                            f"---\n"
                            f"*Thread ID: `{thread_id}`*\n\n"
                            f"Use the approve/reject buttons below or call the API:\n"
                            f"- `POST /hitl/approve` with `{{\"thread_id\": \"{thread_id}\"}}`\n"
                            f"- `POST /hitl/reject` with `{{\"thread_id\": \"{thread_id}\"}}`"
                        )
                        return

                    elif event_type == "final":
                        final_text = event.get("response", partial_answer)
                        sources = _format_sources(event.get("sources", []))
                        yield final_text + sources

                    elif event_type == "error":
                        yield f"Error: {event.get('message', 'Unknown error')}"
                        return

                # If we never got a final event, yield what we have
                if partial_answer and not status_line.startswith("*Formatting"):
                    yield partial_answer

    except httpx.ReadTimeout:
        yield "Request timed out. The server may be overloaded — please try again."
    except Exception as e:
        yield f"Error communicating with the server: {e}"


def create_app():
    token_state = gr.State(value="")

    with gr.Blocks(title="RAG Agent - Financial Document Q&A") as app:
        gr.Markdown("# RAG Agent — Enterprise Financial Document Q&A")

        with gr.Tab("Login"):
            username = gr.Textbox(label="Username", placeholder="e.g., finance, hr, clevel, admin")
            password = gr.Textbox(label="Password", type="password")
            login_btn = gr.Button("Login")
            login_status = gr.Textbox(label="Status", interactive=False)

            def do_login(u, p):
                tok = login(u, p)
                if tok:
                    return tok, f"Logged in as {u}"
                return "", "Login failed. Check credentials."

            login_btn.click(do_login, inputs=[username, password], outputs=[token_state, login_status])

        with gr.Tab("Chat"):
            chatbot = gr.ChatInterface(
                fn=chat,
                title="Ask about financial documents",
                additional_inputs=[token_state],
                examples=[
                    ["What was Apple's total revenue in fiscal year 2023?"],
                    ["What is the maximum daily travel expense allowed?"],
                    ["Show me the breakdown of operating expenses."],
                ],
            )

    return app


if __name__ == "__main__":
    app = create_app()
    app.launch(server_name="0.0.0.0", server_port=7860)
