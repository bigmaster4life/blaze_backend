# blaze_backend/views.py
from django.http import JsonResponse

def healthz(request):
    return JsonResponse({"status": "ok"})