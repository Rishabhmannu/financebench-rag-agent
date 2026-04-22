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
        page = s.get("page")
        page_str = str(page) if page else "?"
        lines += f"- {s.get('file', 'Unknown')} (Page {page_str})\n"
    return lines


async def chat(message: str, token: str, thread_id: str):
    """Stream response from RAG agent via SSE endpoint.

    thread_id persists across turns so the backend can load prior conversation
    state from the PostgresSaver checkpointer. Yields (response_text, new_thread_id).
    """
    if not token:
        yield "Please login first using the Login tab.", thread_id
        return

    payload = {"message": message}
    if thread_id:
        payload["thread_id"] = thread_id

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                f"{API_URL}/chat/stream",
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "text/event-stream",
                },
            ) as response:
                if response.status_code == 401:
                    yield "Session expired. Please login again.", thread_id
                    return
                if response.status_code != 200:
                    yield f"Server error (HTTP {response.status_code}). Please try again.", thread_id
                    return

                partial_answer = ""
                status_line = ""
                current_thread = thread_id

                async for line in response.aiter_lines():
                    # SSE format: "data: {...}"
                    if not line.startswith("data:"):
                        continue
                    raw = line[len("data:"):].strip()
                    if not raw:
                        continue

                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    event_type = event.get("type", "")

                    if event_type == "node_start":
                        status_line = f"*{event.get('label', '')}...*\n\n"
                        yield status_line + partial_answer, current_thread

                    elif event_type == "token":
                        partial_answer += event.get("content", "")
                        yield status_line + partial_answer, current_thread

                    elif event_type == "hitl_interrupt":
                        current_thread = event.get("thread_id", current_thread)
                        reason = event.get("reason", "Approval required")
                        preview = event.get("answer_preview", partial_answer)
                        yield (
                            f"**Approval Required**\n\n"
                            f"{reason}\n\n"
                            f"**Answer preview:**\n{preview}\n\n"
                            f"---\n"
                            f"*Thread ID: `{current_thread}`*\n\n"
                            f"Use the approve/reject buttons below or call the API:\n"
                            f"- `POST /hitl/approve` with `{{\"thread_id\": \"{current_thread}\"}}`\n"
                            f"- `POST /hitl/reject` with `{{\"thread_id\": \"{current_thread}\"}}`"
                        ), current_thread
                        return

                    elif event_type == "final":
                        current_thread = event.get("thread_id", current_thread)
                        final_text = event.get("response", partial_answer)
                        sources = _format_sources(event.get("sources", []))
                        yield final_text + sources, current_thread

                    elif event_type == "error":
                        yield f"Error: {event.get('message', 'Unknown error')}", current_thread
                        return

                if partial_answer and not status_line.startswith("*Formatting"):
                    yield partial_answer, current_thread

    except httpx.ReadTimeout:
        yield "Request timed out. The server may be overloaded — please try again.", thread_id
    except Exception as e:
        yield f"Error communicating with the server: {e}", thread_id


def create_app():
    token_state = gr.State(value="")
    thread_state = gr.State(value="")

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
                    # New login -> fresh conversation thread
                    return tok, "", f"Logged in as {u}"
                return "", "", "Login failed. Check credentials."

            login_btn.click(do_login, inputs=[username, password], outputs=[token_state, thread_state, login_status])

        with gr.Tab("Chat"):
            with gr.Row():
                new_chat_btn = gr.Button("New Conversation", size="sm")

            chatbot = gr.Chatbot(label="Chatbot", height=500)
            msg = gr.Textbox(label="Message", placeholder="Ask about financial documents...", autofocus=True)

            gr.Examples(
                examples=[
                    "What was Apple's total revenue in fiscal year 2023?",
                    "What about Microsoft?",
                    "What is the maximum daily travel expense allowed?",
                ],
                inputs=msg,
            )

            async def respond(user_msg, chat_history, tok, tid):
                """Handle a user turn: stream the response, updating history and thread_id."""
                if not user_msg:
                    yield chat_history, tid, ""
                    return
                chat_history = chat_history + [[user_msg, ""]]
                async for partial, new_tid in chat(user_msg, tok, tid):
                    chat_history[-1][1] = partial
                    yield chat_history, new_tid, ""

            msg.submit(
                respond,
                inputs=[msg, chatbot, token_state, thread_state],
                outputs=[chatbot, thread_state, msg],
            )

            def reset_thread():
                return [], ""

            new_chat_btn.click(reset_thread, outputs=[chatbot, thread_state])

    return app


if __name__ == "__main__":
    app = create_app()
    app.launch(server_name="0.0.0.0", server_port=7860)
