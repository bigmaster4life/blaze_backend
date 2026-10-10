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
from channels.routing import ProtocolTypeRouter, URLRouter
from blaze_backend.channels_jwt import JwtAuthMiddlewareStack
import RideVTC.routing
import analytics.routing
import Delivery.routing

QueryAuthStack = JwtAuthMiddlewareStack

all_ws_patterns = []
all_ws_patterns += getattr(RideVTC.routing, "websocket_urlpatterns", [])
all_ws_patterns += getattr(analytics.routing, "websocket_urlpatterns", [])
all_ws_patterns += getattr(Delivery.routing, "websocket_urlpatterns", [])

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": QueryAuthStack(URLRouter(all_ws_patterns)),
})