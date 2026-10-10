# analytics/consumers.py
from channels.generic.websocket import AsyncJsonWebsocketConsumer

class OpsConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated or not user.is_active:
            await self.close(code=4401)
            return
        if not (user.is_staff or user.is_superuser):
            await self.close(code=4403)
            return
        await self.accept()
        await self.send_json({"type": "system.hello", "message": "WS ok"})

    async def receive_json(self, content, **kwargs):
        # Echo simple
        await self.send_json({"type": "echo", "payload": content})

    async def disconnect(self, code):
        pass