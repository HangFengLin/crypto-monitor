"""Compression for the VPS's finite responses, with unbuffered SSE."""

from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipMiddleware


class WebsiteGZipMiddleware:
    def __init__(self, app, minimum_size=1000, compresslevel=5):
        self.app = app
        self.compressed = GZipMiddleware(app, minimum_size=minimum_size, compresslevel=compresslevel)

    async def __call__(self, scope, receive, send):
        # The pinned Starlette gzip implementation buffers streaming output.
        # Leave the live event stream uncompressed so every frame flushes at once.
        if scope["type"] != "http" or scope.get("path") == "/api/events":
            return await self.app(scope, receive, send)
        preferences = {}
        for option in Headers(scope=scope).get("accept-encoding", "").split(","):
            coding, *parameters = option.lower().strip().split(";")
            quality = 1.0
            for parameter in parameters:
                name, separator, value = parameter.strip().partition("=")
                if name == "q" and separator:
                    try:
                        quality = float(value)
                    except ValueError:
                        quality = 0.0
            preferences[coding] = quality
        if not 0 < preferences.get("gzip", preferences.get("*", 0)) <= 1:
            return await self.app(scope, receive, send)
        # Normalize negotiation for the pinned middleware's substring check.
        normalized = {**scope, "headers": [
            (key, value) for key, value in scope["headers"] if key.lower() != b"accept-encoding"
        ] + [(b"accept-encoding", b"gzip")]}
        await self.compressed(normalized, receive, send)
