# analytics/consumers.py
from channels.generic.websocket import AsyncJsonWebsocketConsumer

class OpsConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        # (optionnel) tu peux vérifier self.scope["user"].is_authenticated ici
        await self.accept()
        await self.send_json({"type": "system.hello", "message": "WS ok"})

    async def receive_json(self, content, **kwargs):
        # Echo simple
        await self.send_json({"type": "echo", "payload": content})

    async def disconnect(self, code):
        pass