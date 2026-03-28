"""Gradio ChatInterface connected to FastAPI backend."""

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


async def chat(message: str, history: list, token: str) -> str:
    """Send message to RAG agent via FastAPI and stream response."""
    if not token:
        return "Please login first using the Login tab."

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{API_URL}/chat",
                json={"message": message},
                headers={"Authorization": f"Bearer {token}"},
            )

        if response.status_code == 401:
            return "Session expired. Please login again."

        data = response.json()
        answer = data.get("response", "No response.")

        # Append sources if available
        sources = data.get("sources", [])
        if sources:
            answer += "\n\n---\n**Sources:**\n"
            for s in sources:
                answer += f"- {s.get('file', 'Unknown')} (Page {s.get('page', '?')})\n"

        return answer

    except Exception as e:
        return f"Error communicating with the server: {e}"


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
                fn=lambda msg, hist: chat(msg, hist, token_state.value),
                title="Ask about financial documents",
                examples=[
                    "What was Apple's total revenue in fiscal year 2023?",
                    "What is the maximum daily travel expense allowed?",
                    "Show me the breakdown of operating expenses.",
                ],
            )

    return app


if __name__ == "__main__":
    app = create_app()
    app.launch(server_name="0.0.0.0", server_port=7860)
