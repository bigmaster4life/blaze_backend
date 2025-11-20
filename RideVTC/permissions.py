from rest_framework.permissions import BasePermission, SAFE_METHODS
from RideVTC.models import Ride

class IsDriverOrStaff(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        if getattr(u, "is_staff", False):
            return True
        # Adapte selon ton modèle user: user_type == "driver" ou role == "driver"
        return getattr(u, "user_type", "") == "driver"
    
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
        if getattr(u, "is_staff", False):
            return True

        user_type = getattr(u, "user_type", "")

        # 2) Le driver lui-même → peut voir son propre profil
        # ⚠️ adapte "obj.user" si ton modèle Driver pointe vers l'user autrement
        if user_type == "driver" and getattr(obj, "user", None) == u:
            return True

        # 3) Un client peut seulement LIRE si c'est un driver
        # avec lequel il a (eu) une course
        if request.method in SAFE_METHODS and user_type == "customer":
            # ⚠️ adapte:
            #   - champ driver sur Ride (driver / chauffeur / driver_id...)
            #   - champ customer sur Ride (customer / rider / user...)
            qs = Ride.objects.filter(
                driver=obj,
                customer=u,
            ).exclude(status__in=["cancelled", "rejected"])  # adapte les statuts si besoin

            return qs.exists()

        return False