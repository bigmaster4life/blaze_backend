from django.urls import path
from . import views

urlpatterns = [
    path("summary/", views.summary),
    path("timeseries/", views.timeseries),
    path("revenue_daily/", views.revenue_daily),
    path("payment_split/", views.payment_split),
    path("top_drivers/", views.top_drivers),
    path("issues/", views.issues),
    path("live/", views.live),
]