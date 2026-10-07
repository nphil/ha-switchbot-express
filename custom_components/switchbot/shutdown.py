"""The process-lifetime "Home Assistant is shutting down" latch.

Home Assistant runs its shutdown jobs (``hass.async_add_shutdown_job``) before it fires
``EVENT_HOMEASSISTANT_STOP``, and ``hass.state`` is still ``running`` while they run, so
``hass.is_stopping`` cannot tell. It also reads its job list once, when the stage starts: a job
registered later (by an entry that is set up or reloaded during the stage) is never run. So the
latch lives in ``hass.data[DOMAIN]``, is set by a domain-level job registered in ``async_setup``
(never removed when an entry unloads), and is checked by entry setup. Once set it is never
cleared: nothing in this integration connects again in this process.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_KEY = "shutting_down"


def begin(hass: HomeAssistant) -> None:
    """Latch: Home Assistant is shutting down."""
    hass.data.setdefault(DOMAIN, {})[_KEY] = True


def in_progress(hass: HomeAssistant) -> bool:
    """Whether Home Assistant is shutting down (the latch is set)."""
    return bool(hass.data.get(DOMAIN, {}).get(_KEY))
