# analytics/server_ops.py
import psutil
import time
import platform
from django.http import JsonResponse
from django.utils import timezone


BOOT_TIME = psutil.boot_time()

def server_ops(request):
    """Retourne les métriques avancées du serveur."""
    disk = psutil.disk_usage('/')
    mem = psutil.virtual_memory()
    cpu_percent = psutil.cpu_percent(interval=0.3)

    net = psutil.net_io_counters()

    uptime_sec = int(time.time() - BOOT_TIME)

    return JsonResponse({
        "server_time": timezone.now(),
        "uptime_sec": uptime_sec,
        "cpu_percent": cpu_percent,
        "memory": {
            "percent": mem.percent,
            "used_mb": int(mem.used / 1024 / 1024),
            "total_mb": int(mem.total / 1024 / 1024),
        },
        "disk": {
            "percent": disk.percent,
            "used_gb": round(disk.used / 1024 / 1024 / 1024, 2),
            "total_gb": round(disk.total / 1024 / 1024 / 1024, 2),
        },
        "network": {
            "bytes_sent": net.bytes_sent,
            "bytes_recv": net.bytes_recv,
        },
        "python": platform.python_version(),
        "system": platform.platform(),
    })