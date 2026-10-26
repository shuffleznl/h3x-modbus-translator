"""Owned server/poller lifetime, with bounded I/O and immediate fault invalidation."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from .model import MeterState, Settings
from .server import MeterServer
from .source import ShellySource

_LOGGER = logging.getLogger(__name__)


class Runtime:
    def __init__(self, settings: Settings, notify=lambda: None):
        self.state = MeterState(settings)
        self.source = ShellySource(self.state)
        self.server = MeterServer(self.state)
        self.notify = notify
        self.task: asyncio.Task | None = None

    async def start(self):
        try:
            await self.server.serve_forever(background=True)
            self.state.listening = True
            self.task = asyncio.create_task(self._poll(), name="h3x-meter-source")
        except BaseException:
            await self.close()
            raise

    async def _poll(self):
        failures = 0
        while True:
            try:
                self.state.publish(await self.source.poll())
                failures = 0
            except Exception as err:
                # Do not serve the previous map after a failed read, even during reconnect.
                was_valid = self.state.valid
                self.state.fail(type(err).__name__ + ": " + str(err)[:180])
                self.source.close()
                failures += 1
                if was_valid:
                    _LOGGER.warning(
                        "Shelly measurement lost; meter responses invalidated: %s",
                        self.state.last_error,
                    )
            self.notify()
            await asyncio.sleep(min(30, self.state.settings.poll_seconds * 2 ** min(failures, 4)))

    async def close(self):
        self.state.valid = False
        self.state.listening = False
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        await self.server.shutdown()
        self.source.close()
