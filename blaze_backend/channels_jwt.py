from urllib.parse import parse_qs
from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from django.contrib.auth.models import AnonymousUser
from rest_framework_simplejwt.authentication import JWTAuthentication

@database_sync_to_async
def get_user_for_token(token: str):
    auth = JWTAuthentication()
    validated = auth.get_validated_token(token)
    user = auth.get_user(validated)
    return user

class JwtAuthMiddleware(BaseMiddleware):
    async def __call__(self, scope, receive, send):
        scope = dict(scope)
        query = parse_qs(scope.get("query_string", b"").decode(errors="replace"))
        token = (query.get("token") or [None])[0]
        headers = dict(scope.get("headers", []))
        authorization = headers.get(b"authorization", b"").decode(errors="replace")
        if authorization:
            token = authorization[7:] if authorization.startswith("Bearer ") else "invalid"
        scope["user"] = AnonymousUser()
        if token:
            try:
                scope["user"] = await get_user_for_token(token)
            except Exception:
                scope["user"] = AnonymousUser()
        return await super().__call__(scope, receive, send)

def JwtAuthMiddlewareStack(inner):
    return JwtAuthMiddleware(inner)