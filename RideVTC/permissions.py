from rest_framework.permissions import BasePermission, SAFE_METHODS
from RideVTC.models import Ride

class IsDriverOrStaff(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        if getattr(u, "is_staff", False) or getattr(u, "is_superuser", False):
            return True
        # Adapte selon ton modèle user: user_type == "driver" ou role == "driver"
        raw_type = (getattr(u, "user_type", "") or "").lower()
        return raw_type in ("chauffeur", "driver")
    
class CanViewDriverProfile(BasePermission):
    """
    - Staff : accès total
    - Driver : accès à SON propre profil
    - Client : lecture possible si il a une course avec ce driver
    """

    def has_object_permission(self, request, view, obj):
        u = request.user
        if not u or not u.is_authenticated:
            return False

        # 1) Staff → OK
        if getattr(u, "is_staff", False) or getattr(u, "is_superuser", False):
            return True

        raw_type = (getattr(u, "user_type", "") or "").lower()

        # 2) Le driver lui-même → peut voir son propre profil
        # ⚠️ adapte "obj.user" si ton modèle Driver pointe vers l'user autrement
        if raw_type in ("chauffeur", "driver") and getattr(obj, "user_id", None) == u.id:
            return True

        # 3) Un client peut seulement LIRE si c'est un driver
        # avec lequel il a (eu) une course
        if request.method in SAFE_METHODS and raw_type in ("customer", "client"):
            driver_user_id = getattr(getattr(obj, "user", None), "id", None)
            if driver_user_id is None:
                return False
            # ⚠️ adapte:
            #   - champ driver sur Ride (driver / chauffeur / driver_id...)
            #   - champ customer sur Ride (customer / rider / user...)
            qs = (
                Ride.objects
                .filter(driver_id=driver_user_id, user_id=u.id)
                .exclude(status__in=["cancelled", "rejected"])
            )

            return qs.exists()

        return False