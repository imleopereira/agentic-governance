"""Pydantic models for agent presence."""
from __future__ import annotations

from enum import Enum


class AgentStatus(str, Enum):
    """Agent presence status."""

    LIVE = "live"
    IDLE = "idle"
    UNRESPONSIVE = "unresponsive"
