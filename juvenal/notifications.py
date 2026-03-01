"""Webhook notifications for workflow completion/failure."""

from __future__ import annotations

import json
import urllib.error
import urllib.request


def send_webhook(url: str, payload: dict, timeout: int = 10) -> bool:
    """Send a JSON POST to a webhook URL. Returns True on success, False on failure."""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except (urllib.error.URLError, OSError):
        return False


def build_notification_payload(
    workflow_name: str,
    success: bool,
    total_bounces: int,
    duration: float | None,
    total_input_tokens: int,
    total_output_tokens: int,
    phase_summaries: list[dict],
) -> dict:
    """Build the JSON payload for a webhook notification."""
    return {
        "workflow": workflow_name,
        "status": "success" if success else "failure",
        "total_bounces": total_bounces,
        "duration_seconds": round(duration, 1) if duration else None,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "phases": phase_summaries,
    }
