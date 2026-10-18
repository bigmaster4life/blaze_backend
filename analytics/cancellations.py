"""Cancellation reasons are observations, not proof of fraud."""
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from .models import CancellationRecord

CLIENT_REASONS = ('partner_requested', 'off_platform_payment', 'partner_absent', 'wait_too_long', 'plans_changed', 'wrong_address', 'other', 'search_timeout')


class ReasonSerializer(serializers.Serializer):
    cancellation_reason = serializers.ChoiceField(choices=CLIENT_REASONS)
    cancellation_note = serializers.CharField(required=False, allow_blank=True, max_length=500, default='')


def parse_reason(data, *, customer, status_before):
    # Older installed clients remain cancellable during the mobile rollout.
    # Their missing reason is explicit in the audit, never fabricated.
    if not any(k in data for k in ('cancellation_reason', 'cancellation_note', 'cancellation_version')):
        if customer and getattr(settings, 'CANCELLATION_REASON_REQUIRED', False):
            raise serializers.ValidationError({'cancellation_reason': 'Choisissez un motif d’annulation.'})
        return {'reason': 'legacy_unspecified', 'note': ''}
    serializer = ReasonSerializer(data=data)
    serializer.is_valid(raise_exception=True)
    values=serializer.validated_data
    if values['cancellation_reason'] == 'search_timeout' and status_before not in ('pending','searching','operator_required'):
        raise serializers.ValidationError({'detail': 'La demande a déjà été acceptée. Vérifiez son état avant de l’annuler.'})
    return {'reason': values['cancellation_reason'], 'note': values['cancellation_note']}


def record_cancellation(obj, actor, before, reason):
    from RideVTC.models import Ride, DriverPresence
    is_ride=isinstance(obj,Ride)
    partner_id=obj.driver_id if is_ride else (obj.assigned_driver.user_id if obj.assigned_driver_id else None)
    customer=actor.pk==obj.user_id
    context={}
    if is_ride:
        p=DriverPresence.objects.filter(driver_id=partner_id).first() if partner_id else None
        stamp=p.location_updated_at if p else None
        lat,lng=(p.lat,p.lng) if p else (None,None)
    else:
        p=obj.assigned_driver
        stamp=p.last_location_update if p else None
        lat,lng=(p.last_lat,p.last_lng) if p else (None,None)
    if stamp:
        age=max(0,(timezone.now()-stamp).total_seconds())
        context['position_age_seconds']=round(age)
        if age<=120 and None not in (lat,lng,obj.pickup_lat,obj.pickup_lng):
            import math
            coords=(lat,lng,obj.pickup_lat,obj.pickup_lng)
            valid=all(math.isfinite(x) for x in coords) and -90<=lat<=90 and -180<=lng<=180 and -90<=obj.pickup_lat<=90 and -180<=obj.pickup_lng<=180
            if valid:
                a,b,c,d=map(math.radians,coords)
                value=math.sin((c-a)/2)**2+math.cos(a)*math.cos(c)*math.sin((d-b)/2)**2
                distance=6371000*2*math.asin(math.sqrt(min(1,max(0,value))))
                if math.isfinite(distance): context['pickup_distance_m']=round(distance)
    event, created = CancellationRecord.objects.get_or_create(**({'ride':obj} if is_ride else {'delivery':obj}), defaults={
        'actor':actor,'partner_id':partner_id,'actor_role':'customer' if customer else 'partner' if partner_id==actor.pk else 'staff',
        'reason':reason['reason'],'note':reason['note'],'status_before':before,'context':context,
    })

    if created:
        from .detection_tasks import schedule_detection
        transaction.on_commit(schedule_detection, robust=True)
