"""Deliver notifications to an org's webhook.

There is no SMTP anywhere in this product and there is not going to be: the
draft-only toolkit's whole containment argument is that the worst outcome is a
bad draft a human reviews. So alerts reach people in-app and, optionally,
through a webhook the org's admin configures -- which is also how they reach
Slack, Teams or a rota system without this codebase knowing about any of them.

An admin-configured webhook is NOT the model-chosen egress that
`deploy_validation.find_email_egress_conflicts` refuses: the destination is
fixed by a human, not selected by an agent reading attacker-controlled mail.
It is still validated with `check_host_allowed`, because in a multi-tenant
deployment a tenant admin is not trusted to reach the host's internal network.

Stdlib only (`urllib.request`), like `bestteam.tools._oauth`, so the per-org
email path still needs no optional extra.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from bestteam.tools.http_client import check_host_allowed

from . import secret_store
from .db.models import Notification
from .db.notifications import (
    STATE_DELIVERED,
    STATE_FAILED,
    STATE_SKIPPED,
    get_notification_settings,
    pending_notifications,
)

_logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 10
MAX_DELIVERY_ATTEMPTS = 5
_MAX_ERROR_CHARS = 500

SIGNATURE_HEADER = "X-BestTeam-Signature"
DELIVERY_HEADER = "X-BestTeam-Delivery"


class WebhookUrlError(ValueError):
    """A webhook URL this deployment refuses to call."""


def validate_webhook_url(url: str) -> str:
    """Return the URL if it is one we are willing to POST to.

    HTTPS only -- a notification names the org and its fault, and this crosses
    the public internet. `check_host_allowed` then applies the same public-IP
    restriction the per-org IMAP path uses, so a tenant admin cannot point the
    deployment at its own internal network. Self-hosted internal endpoints are
    therefore unsupported, which is recorded in the deployment docs.
    """
    cleaned = (url or "").strip()
    if not cleaned:
        raise WebhookUrlError("Enter a webhook URL, or leave it blank to use in-app alerts only.")
    if not cleaned.lower().startswith("https://"):
        raise WebhookUrlError("The webhook URL must start with https://.")
    host = urlparse(cleaned).hostname
    if not host:
        raise WebhookUrlError("That doesn't look like a complete URL.")
    try:
        check_host_allowed(host)
    except Exception as exc:  # noqa: BLE001 -- surfaced to the admin as a 400
        raise WebhookUrlError(
            f"That webhook host can't be used: {exc}"
        ) from exc
    return cleaned


def _sign(secret: str, body: bytes) -> str:
    """`sha256=<hex>` over the exact bytes posted, so the receiver can verify."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _payload(notification: Notification) -> Dict[str, Any]:
    """Health information only.

    No subjects, addresses, bodies or snippets ever appear here. A webhook is
    an outbound channel to a third-party service, and email content leaving the
    deployment through it would undo the containment the whole capability rests
    on.
    """
    created = notification.created_at
    return {
        "id": notification.id,
        "org_id": notification.org_id,
        "kind": notification.kind,
        "severity": notification.severity,
        "title": notification.title,
        "body": notification.body,
        "fingerprint": notification.fingerprint,
        "created_at": created.isoformat() if created is not None else None,
    }


def _post(url: str, body: bytes, headers: Dict[str, str]) -> int:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
        return int(response.status or 0)


def _deliver(notification: Notification, url: str, secret: Optional[str]) -> None:
    body = json.dumps(_payload(notification), separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        DELIVERY_HEADER: str(notification.id),
    }
    if secret:
        headers[SIGNATURE_HEADER] = _sign(secret, body)
    status = _post(url, body, headers)
    if not 200 <= status < 300:
        raise urllib.error.HTTPError(url, status, "webhook rejected", None, None)


def dispatch_pending(db: Session, limit: int = 20) -> int:
    """Attempt delivery for up to `limit` pending notifications.

    Returns how many rows changed state. Called at the end of each poll cycle
    rather than from a thread of its own: the poller already runs on a timer
    and a notification that waits one cycle has still arrived far sooner than
    the customer noticing by themselves.

    Never raises. A notification that exhausts its attempts is marked `failed`
    and stays visible in-app -- the customer still learns, just not through
    the webhook.
    """
    changed = 0
    for notification in pending_notifications(db, limit):
        settings = get_notification_settings(db, notification.org_id)
        if settings is None or not settings.enabled or not settings.webhook_url:
            notification.delivery_state = STATE_SKIPPED
            changed += 1
            continue

        secret = None
        try:
            if settings.webhook_secret_encrypted:
                secret = secret_store.decrypt(settings.webhook_secret_encrypted)
            url = validate_webhook_url(settings.webhook_url)
            _deliver(notification, url, secret)
        except Exception as exc:  # noqa: BLE001 -- delivery is best effort
            notification.delivery_attempts = (notification.delivery_attempts or 0) + 1
            notification.last_delivery_error = str(exc)[:_MAX_ERROR_CHARS]
            if notification.delivery_attempts >= MAX_DELIVERY_ATTEMPTS:
                notification.delivery_state = STATE_FAILED
                _logger.warning(
                    "notification %s gave up after %s delivery attempts: %s",
                    notification.id, notification.delivery_attempts, exc,
                )
            changed += 1
            continue

        notification.delivery_state = STATE_DELIVERED
        notification.delivered_at = datetime.now(timezone.utc)
        changed += 1

    if changed:
        db.commit()
    return changed
