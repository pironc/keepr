"""Server-Sent Events formatting."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

_DONE = object()


def format_event(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def merge_async_iterators[T](*iterators: AsyncIterator[T]) -> AsyncIterator[T]:
    """Fan in several independent live streams into one, in arrival order,
    until every one of them is exhausted — neither source can block the
    other. Used to combine document-status watching with message
    generation watching into a single SSE response (see
    routes_messages.py's `_stream_watch`): each runs on its own schedule,
    so this is what lets a conversation's Sources panel keep updating
    while that same conversation's answer is still streaming, and vice
    versa.
    """
    queue: asyncio.Queue[Any] = asyncio.Queue()

    async def _pump(iterator: AsyncIterator[T]) -> None:
        try:
            async for item in iterator:
                await queue.put(item)
        finally:
            await queue.put(_DONE)

    tasks = [asyncio.create_task(_pump(it)) for it in iterators]
    remaining = len(tasks)
    try:
        while remaining > 0:
            item = await queue.get()
            if item is _DONE:
                remaining -= 1
                continue
            yield item
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
