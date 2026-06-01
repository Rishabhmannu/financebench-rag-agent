"""Suppress upstream deprecation warnings that fire at import time.

Each filter targets one specific upstream warning, audited and confirmed not
actionable from this repo. Filters match by message regex so a new warning
under the same category still surfaces.

Import this module FIRST in any entry point that loads the graph (FastAPI,
eval runners) so the filters install before the noisy upstream imports run.
"""
import warnings


def _install_filters() -> None:
    # langgraph 0.6.11's cache module imports JsonPlusSerializer; its Reviver
    # constructor emits a pending-deprecation about allowed_objects. Fixed only
    # in langgraph 1.x (major-version bump).
    warnings.filterwarnings(
        "ignore",
        message=r".*allowed_objects.*will change in a future version.*",
    )

    # uvicorn[standard]'s websockets adapter pulls websockets.legacy.
    warnings.filterwarnings(
        "ignore",
        message=r"websockets\.(server\.WebSocketServerProtocol|legacy)",
        category=DeprecationWarning,
    )

    # protobuf C-extension types — Python 3.14 prep, fix lives in protobuf upstream.
    warnings.filterwarnings(
        "ignore",
        message=r"Type google\.protobuf\.pyext\._message\.",
        category=DeprecationWarning,
    )


_install_filters()

# Both langchain_core/__init__.py and langchain/__init__.py call
# surface_langchain_deprecation_warnings() at import time, which inserts
# ("default", None, LangChain[Pending]DeprecationWarning) filters at
# position 0 — ahead of anything installed earlier. Force both packages to
# import now so their filters are in place, then re-install ours on top.
for _pkg in ("langchain_core", "langchain"):
    try:
        __import__(_pkg)
    except ImportError:
        pass
_install_filters()
