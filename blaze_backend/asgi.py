# blaze_backend/asgi.py
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "blaze_backend.settings")

# 1) INITIALISER DJANGO EN PREMIER
from django.core.asgi import get_asgi_application
django_asgi_app = get_asgi_application()

# 1bis) En DEBUG, servir /static/ via ASGI (admin CSS, etc.)
from django.conf import settings
if settings.DEBUG:
    # nécessite 'django.contrib.staticfiles' dans INSTALLED_APPS
    from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler
    django_asgi_app = ASGIStaticFilesHandler(django_asgi_app)

# 2) ENSUITE SEULEMENT, IMPORTER LE RESTE
from urllib.parse import parse_qs
from django.contrib.auth.models import AnonymousUser
from django.contrib.auth import get_user_model

from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from channels.middleware import BaseMiddleware

from rest_framework_simplejwt.tokens import AccessToken

# Conserver les routes existantes (BlazeMobile) + celles d’Analytics
import RideVTC.routing
import analytics.routing

User = get_user_model()

class QueryStringJWTAuthMiddleware(BaseMiddleware):
    """
    Auth WS via ?token=<JWT>. Si absent/invalide -> AnonymousUser.
    """
    async def __call__(self, scope, receive, send):
        try:
            query = parse_qs(scope.get("query_string", b"").decode())
            token = (query.get("token") or [None])[0]
            if token:
                try:
                    validated = AccessToken(token)
                    user_id = validated.get("user_id")
                    if user_id:
                        scope["user"] = await self._aget_user(user_id)
                    else:
                        scope["user"] = AnonymousUser()
                except Exception:
                    scope["user"] = AnonymousUser()
            else:
                scope["user"] = AnonymousUser()
        except Exception:
            scope["user"] = AnonymousUser()
        return await super().__call__(scope, receive, send)

    @staticmethod
    async def _aget_user(user_id):
        try:
            return await User.objects.aget(pk=user_id)
        except Exception:
            return AnonymousUser()

def QueryAuthStack(inner):
    # garde l’auth de session + support du token en query string
    return QueryStringJWTAuthMiddleware(AuthMiddlewareStack(inner))

all_ws_patterns = []
all_ws_patterns += getattr(RideVTC.routing, "websocket_urlpatterns", [])
all_ws_patterns += getattr(analytics.routing, "websocket_urlpatterns", [])

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": QueryAuthStack(URLRouter(all_ws_patterns)),
})