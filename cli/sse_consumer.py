"""Consume the /v1/chat/stream SSE events and drive the terminal UX.

Render contract (Phase 1 user feedback): the 60-90s "feels like hanging"
problem is solved by surfacing the backend's node_start labels as truthful
real-time spinner text (NOT generic "loading..." rotation), then streaming
tokens directly to stdout as they arrive. See DEPLOYMENT_PLAN.md Section 12
Phase 2 deliverables.

Returns the `final` payload (or hitl_interrupt event, or error event) so
the caller can extract thread_id for follow-up turns and cost data for
session accumulation.
"""

from __future__ import annotations

import logging
from typing import Iterator

from cli.render import (
    console,
    make_status,
    render_error,
    render_final_footer,
    render_final_response_text,
    render_hitl_panel,
)

logger = logging.getLogger(__name__)


def render_chat_stream(events: Iterator[dict]) -> dict:
    """Drive the spinner + token stream + final footer off an SSE event iterator.

    Returns the terminal payload (final, hitl_interrupt, or error event dict).
    Callers can read .get("thread_id"), .get("cost_usd"), .get("tokens"),
    .get("requires_approval") etc. off the returned dict.
    """
    status_ctx = make_status("Connecting...")
    status_ctx.start()

    streaming_started = False
    terminal: dict = {"type": "unknown"}

    try:
        for event in events:
            et = event.get("type")

            if et == "node_start":
                status_ctx.update(event.get("label", "..."))

            elif et == "node_end":
                pass  # next node_start replaces the label

            elif et == "token":
                content = event.get("content", "")
                if not content:
                    continue
                if not streaming_started:
                    status_ctx.stop()
                    streaming_started = True
                    console.print()
                console.print(content, end="", soft_wrap=True)

            elif et == "pending_approval":
                # Phase 3.5: requester is HITL-gated. Draft is suppressed.
                # The yellow panel shows reason + amount + threshold + approver
                # role list. The REPL caller polls /v1/chat/result for the
                # released answer.
                if not streaming_started:
                    status_ctx.stop()
                else:
                    console.print()
                render_hitl_panel(event)
                terminal = event
                return terminal

            elif et == "hitl_interrupt":
                # Phase 3 legacy event. Backend no longer emits it (replaced by
                # pending_approval) but keep the handler so older backends still
                # render something sensible.
                if not streaming_started:
                    status_ctx.stop()
                else:
                    console.print()
                render_hitl_panel(event)
                terminal = event
                return terminal

            elif et == "final":
                if not streaming_started:
                    status_ctx.stop()
                    # No tokens streamed during this turn — render the response
                    # text from the final event (e.g., RBAC-filtered empty-results
                    # path, or any fallback that sets final_response without going
                    # through the generator's astream).
                    render_final_response_text(event)
                else:
                    console.print()
                render_final_footer(event)
                terminal = event
                return terminal

            elif et == "error":
                status_ctx.stop()
                render_error(event.get("message", "(unknown error)"))
                terminal = event
                return terminal

            else:
                logger.debug("Unknown SSE event type %r; ignoring for forward-compat", et)

    finally:
        try:
            status_ctx.stop()
        except Exception:  # noqa: BLE001
            pass

    if not streaming_started:
        render_error("Stream ended without a final event.")
    return terminal
