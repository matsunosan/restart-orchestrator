"""Failure alerts to an external webhook (Slack / Mattermost / Rocket.Chat-compatible format {"text": ...})."""
from __future__ import annotations

import logging

import httpx

from .config import Settings

log = logging.getLogger(__name__)


async def send_alert(settings: Settings, text: str) -> None:
    if not settings.alert_webhook_url:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(settings.alert_webhook_url, json={"text": text})
            response.raise_for_status()
    except Exception as exc:  # an alert must never crash the orchestrator
        log.warning("Could not send alert: %s", exc)
