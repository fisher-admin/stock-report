#!/usr/bin/env python3
"""Lightweight immutable identities shared by publication and research paths."""
from __future__ import annotations


PREBREAKOUT_LEGACY_ALIAS = "prebreakout_v41"
PREBREAKOUT_CONTROL_ID = "prebreakout_v43_control"
PREBREAKOUT_CONTROL_CONFIG_VERSION = "4.3"
PREBREAKOUT_CONTROL_CONFIG_HASH = "8a5054a13fc32f0e"
PREBREAKOUT_CONTROL_STRATEGY_VERSION = (
    f"{PREBREAKOUT_CONTROL_CONFIG_VERSION}+{PREBREAKOUT_CONTROL_CONFIG_HASH}"
)


def prebreakout_identity() -> dict[str, str | bool]:
    return {
        "strategy_id": PREBREAKOUT_CONTROL_ID,
        "strategy_version": PREBREAKOUT_CONTROL_STRATEGY_VERSION,
        "config_hash": PREBREAKOUT_CONTROL_CONFIG_HASH,
        "legacy_source_alias": PREBREAKOUT_LEGACY_ALIAS,
        "legacy_alias_active": True,
    }


__all__ = [
    "PREBREAKOUT_LEGACY_ALIAS",
    "PREBREAKOUT_CONTROL_ID",
    "PREBREAKOUT_CONTROL_CONFIG_VERSION",
    "PREBREAKOUT_CONTROL_CONFIG_HASH",
    "PREBREAKOUT_CONTROL_STRATEGY_VERSION",
    "prebreakout_identity",
]
