"""One error-reporting channel, opt-in by DSN (beta gate G4).

`BESTTEAM_SENTRY_DSN` set => `sentry_sdk` is initialised and exactly two
kinds of thing are reported: an unhandled exception in a request
(`main.unhandled_exception_handler`) and a failed run (`runtime.py`, both the
pipeline's own `run_failed` and the worker-thread catch-all). Nothing else --
no ERROR-log capture, no request bodies, no local variables, no performance
tracing -- because a report has to be safe to send off-box: the process
handles customers' email and documents, and a stack frame's locals or a
request body would carry them. `default_integrations=False` is what keeps the
SDK from adding capture points on its own; every report goes through the two
functions below, which are no-ops when the DSN is unset or the SDK is not
installed, and never raise.

Exception *messages* are scrubbed too (`_scrub_event`, the SDK's
`before_send`): a provider or parser error routinely quotes what it choked on
-- an output parser echoes the model's text, an HTTP error carries the URL a
tool fetched -- so a report keeps the exception type, the stack (file, line,
function; no locals) and our tags, and drops the message. The operator gets
the run id and reads the reason from the run's persisted trace, on-box.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

_logger = logging.getLogger(__name__)

# The imported `sentry_sdk` module once `init_from_env` succeeded; None means
# reporting is off and the helpers below do nothing.
_sdk: Any = None


def init_from_env() -> bool:
    """Initialise reporting from `BESTTEAM_SENTRY_DSN`; True if it is on."""
    global _sdk
    dsn = os.environ.get("BESTTEAM_SENTRY_DSN", "").strip()
    if not dsn:
        _sdk = None
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.dedupe import DedupeIntegration
    except ImportError:
        _logger.warning(
            "BESTTEAM_SENTRY_DSN is set but sentry-sdk is not installed "
            "(pip install 'bestteam[ui]'); error reporting is off"
        )
        _sdk = None
        return False
    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("BESTTEAM_ENVIRONMENT", "production"),
        release=os.environ.get("BESTTEAM_RELEASE") or None,
        # Deliberate: see the module docstring.
        send_default_pii=False,
        max_request_body_size="never",
        include_local_variables=False,
        traces_sample_rate=0.0,
        default_integrations=False,
        integrations=[DedupeIntegration()],
        before_send=_scrub_event,
    )
    _sdk = sentry_sdk
    return True


def is_enabled() -> bool:
    return _sdk is not None


def report_exception(exc: BaseException, **tags: Any) -> None:
    """Report `exc` with string tags (ids and names, never content)."""
    if _sdk is None:
        return
    try:
        _sdk.capture_exception(exc, tags=_stringify(tags))
    except Exception:  # noqa: BLE001 -- reporting must never be the failure
        _logger.warning("Could not report an exception", exc_info=True)


def report_message(message: str, **tags: Any) -> None:
    """Report an error-level message (a failed run that raised nothing)."""
    if _sdk is None:
        return
    try:
        _sdk.capture_message(message, level="error", tags=_stringify(tags))
    except Exception:  # noqa: BLE001
        _logger.warning("Could not report a message", exc_info=True)


def _stringify(tags: Dict[str, Any]) -> Dict[str, str]:
    return {key: str(value) for key, value in tags.items() if value is not None}


def _scrub_event(event: Dict[str, Any], hint: Any) -> Dict[str, Any]:
    """`before_send`: drop every exception message; keep type, stack and tags."""
    for entry in (event.get("exception") or {}).get("values") or []:
        entry.pop("value", None)
    return event
