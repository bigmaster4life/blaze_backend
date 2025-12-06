from rest_framework import serializers
from .models import Vehicle, Promo, Rental, generate_ident_code
from django.utils import timezone

class VehicleSerializer(serializers.ModelSerializer):
    owner_phone = serializers.SerializerMethodField()
    owner_name  = serializers.SerializerMethodField()
    is_available = serializers.SerializerMethodField()

    class Meta:
        model = Vehicle
        fields = '__all__'           # inclut aussi owner_phone car déclaré ci-dessus
        read_only_fields = ['owner', 'created_at']  # ❌ ne pas mettre owner_phone ici

    def get_owner_phone(self, obj):
        user = obj.owner
        if not user:
            return obj.owner_phone or None
        
        phone = getattr(user, 'phone_number', None)
        if phone:
            return phone
        
        profile = getattr(user, 'profile', None)
        phone2 = getattr(profile, 'phone', None)
        if phone2:
            return phone2
        return obj.owner_phone or None
    
    def get_owner_name(self, obj):
        user = obj.owner
        if not user:
            return getattr(obj, 'owner_name', None)  # au cas où tu l’ajoutes plus tard côté modèle
        full = getattr(user, 'get_full_name', None)
        if callable(full):
            name = full().strip()
            if name:
                return name
        name = f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip()
        return name or getattr(user, 'username', None)

    def to_representation(self, instance):
        """Rendre l'URL de l'image absolue (utile côté mobile)."""
        rep = super().to_representation(instance)
        request = self.context.get('request')
        if request and rep.get('image'):
            rep['image'] = request.build_absolute_uri(rep['image'])
        return rep

    def get_is_available(self, obj):
        # Si la vue a annoté is_available_dyn, on l’utilise
        val = getattr(obj, 'is_available_dyn', None)
        if val is not None:
            return bool(val)
        return obj.is_available
    
class PromoSerializer(serializers.ModelSerializer):
    image = serializers.SerializerMethodField()

    class Meta:
        model = Promo
        fields = ["title", "subtitle", "cta", "url", "badge", "image"]

    def get_image(self, obj: Promo):
        request = self.context.get("request")
        if obj.image:
            url = obj.image.url
            return request.build_absolute_uri(url) if request else url
        return None
    
class RentalSerializer(serializers.ModelSerializer):
    customer_phone = serializers.SerializerMethodField()
    customer_name = serializers.SerializerMethodField()
    class Meta:
        model = Rental
        fields = [
            'id', 'vehicle', 'user', 'start_date', 'end_date', 'status',
            'payment_method', 'total_amount', 'hold_expires_at', 'created_at', 'identification_code',
            'customer_phone', 'customer_name',
        ]
        read_only_fields = ['status', 'hold_expires_at', 'created_at', 'user', 'identification_code', 'customer_phone', 'customer_name']
    
    def get_customer_phone(self, obj):
        u = getattr(obj, "user", None)
        if not u:
            return None
        phone = getattr(u, "phone_number", None)
        if phone:
            return phone
        return getattr(u, "phone", None)
    
    def get_customer_name(self, obj):
        u = getattr(obj, "user", None)
        if not u:
            return None
        full = getattr(u, "get_full_name", None)
        if callable(full):
            name = full().strip()
            if name:
                return name
        name = f"{getattr(u, 'first_name', '')} {getattr(u, 'last_name', '')}".strip()
        return name or u.email

    def validate(self, attrs):
        """
        Empêche les chevauchements sur des états actifs (pending/confirmed/in_progress).
        """
        vehicle = attrs['vehicle']
        start_date = attrs['start_date']
        end_date = attrs['end_date']
        if start_date >= end_date:
            raise serializers.ValidationError("La date de fin doit être > à la date de début.")

        qs = Rental.objects.filter(
            vehicle=vehicle,
            status__in=['pending', 'confirmed', 'in_progress'],
        ).filter(
            start_date__lt=end_date,
            end_date__gt=start_date,
        )
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("Véhicule indisponible sur ce créneau.")
        return attrs

    def create(self, validated):
        """
        Création en 'pending' avec un hold de 30 minutes par défaut.
        """
        request = self.context.get('request')
        user = request.user if request and request.user.is_authenticated else None
        if not user:
            raise serializers.ValidationError({"detail": "Authentification requise."})
        validated['user'] = user

        # hold 30 minutes
        validated['status'] = 'pending'
        validated['hold_expires_at'] = timezone.now() + timezone.timedelta(minutes=30)

        rental = super().create(validated)

        rental.recompute_total()
        rental.save(update_fields=['total_amount'])

        # met le véhicule en indispo
        from .models import set_vehicle_availability
        set_vehicle_availability(rental.vehicle)
        return rental
    