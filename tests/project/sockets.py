"""Consumers for the Channels tests: a chat room on the in-memory channel layer, and workers on worker channels."""

import asyncio
import threading

from channels.consumer import AsyncConsumer, SyncConsumer
from channels.generic.websocket import AsyncJsonWebsocketConsumer, WebsocketConsumer
from channels.routing import ChannelNameRouter, ProtocolTypeRouter, URLRouter
from django.contrib.auth.models import User
from django.urls import path

from tests.profiles import busy
from tracepanel._compat import override


class ChatConsumer(AsyncJsonWebsocketConsumer):
    @override
    async def connect(self) -> None:
        self.room = self.scope["url_route"]["kwargs"]["room"]
        await self.channel_layer.group_add(self.room, self.channel_name)
        await self.accept()

    @override
    async def disconnect(self, code: int) -> None:
        await self.channel_layer.group_discard(self.room, self.channel_name)

    @override
    async def receive_json(self, content: dict[str, object], **kwargs: object) -> None:
        if content.get("text") == "boom":
            msg = "cannot post to this room"
            raise ValueError(msg)
        members = await User.objects.acount()
        await self.channel_layer.group_send(self.room, {"type": "chat.message", "text": content["text"]})
        await self.send_json({"members": members})

    async def chat_message(self, event: dict[str, object]) -> None:
        await self.send_json({"text": event["text"]})


class EchoConsumer(WebsocketConsumer):
    """A sync consumer: handlers run in a thread, with the ORM."""

    @override
    def connect(self) -> None:
        self.accept()

    @override
    def receive(self, text_data: str | None = None, bytes_data: bytes | None = None) -> None:
        self.send(text_data=f"{text_data} ({User.objects.count()} users)")


# What a test lets the thumbnail worker go on with: it waits for it while the test looks at the console.
release = threading.Event()
release.set()


class ThumbnailConsumer(AsyncConsumer):
    """A worker: `runworker thumbnails` runs it for the messages sent to the `thumbnails` channel."""

    async def thumbnails_generate(self, message: dict[str, object]) -> None:
        if message.get("image_id") == 0:
            msg = "no such image"
            raise ValueError(msg)
        members = await User.objects.acount()
        await self.channel_layer.send("reports", {"type": "report.build", "members": members})

    async def thumbnails_crunch(self, message: dict[str, object]) -> None:
        busy()

    async def thumbnails_wait(self, message: dict[str, object]) -> None:
        await asyncio.to_thread(release.wait, 10)


class ReportConsumer(SyncConsumer):
    """A sync worker: its handlers run in a thread, with the ORM."""

    def report_build(self, message: dict[str, object]) -> None:
        User.objects.count()


urls = [
    path("ws/chat/<str:room>/", ChatConsumer.as_asgi()),  # type: ignore[call-overload]  # Channels routes ASGI apps
    path("ws/echo/", EchoConsumer.as_asgi()),  # type: ignore[call-overload]
]

# The ASGI application (ASGI_APPLICATION): WebSockets, and the worker channels runworker listens on.
application = ProtocolTypeRouter(
    {
        "websocket": URLRouter(urls),
        "channel": ChannelNameRouter(
            {"thumbnails": ThumbnailConsumer.as_asgi(), "reports": ReportConsumer.as_asgi()}  # type: ignore[call-overload]
        ),
    }
)
