"""Explainable screening rules. Signals are not fraud findings or sanctions."""
import logging
from datetime import timedelta
logger = logging.getLogger(__name__)
from django.db import transaction
from django.db.models import Q, Count
from django.utils import timezone
from .models import CancellationRecord, CancellationAlert

RULE_VERSION = '2026-10-v1'
REPORTS = ('partner_requested', 'off_platform_payment')
EXCLUDED = ('search_timeout', 'legacy_unspecified')


def analyse(record_id):
    with transaction.atomic():
        record = CancellationRecord.objects.select_for_update().filter(pk=record_id, analysis_pending=True).first()
        if record is None:
            return None
        result = evaluate(record)
        alert = None
        if result['score'] >= 40 or any(s['code'] == 'explicit_report' for s in result['signals']):
            alert, _ = CancellationAlert.objects.get_or_create(record=record, defaults=result)
        record.analysis_pending = False
        record.save(update_fields=['analysis_pending'])
        return alert


def evaluate(record):
    from RideVTC.models import Ride
    from Delivery.models import DeliveryRequest
    now = record.created_at
    cutoff = now-timedelta(days=30)
    result = {'score':0, 'signals':[], 'metrics':{'rule_version':RULE_VERSION, 'window_days':30}}
    if not record.partner_id or record.actor_role != 'customer' or record.reason in EXCLUDED or record.status_before not in ('accepted','assigned'):
        return result
    signals=result['signals']
    def signal(code, points, label):
        signals.append({'code':code,'points':points,'label':label})
    if record.reason in REPORTS:
        signal('explicit_report',40,'Le client signale une demande d’annulation ou un paiement hors Be. Signalement à vérifier.')
    distance=record.context.get('pickup_distance_m')
    age=record.context.get('position_age_seconds')
    near=isinstance(distance,(int,float)) and isinstance(age,(int,float)) and 0 <= age <= 120 and 0 <= distance <= 300
    if near:
        signal('near_pickup',15,'Dernière position GPS récente à moins de 300 m du retrait. La proximité ne prouve pas une prise en charge.')
    events=CancellationRecord.objects.filter(partner_id=record.partner_id, actor_role='customer', status_before__in=['accepted','assigned'], created_at__gte=cutoff, created_at__lte=now).exclude(reason__in=EXCLUDED)
    counts=events.aggregate(total=Count('pk'), recent=Count('pk',filter=Q(created_at__gte=now-timedelta(days=7))), same_client=Count('pk',filter=Q(actor_id=record.actor_id)), reporters=Count('actor_id',filter=Q(reason__in=REPORTS),distinct=True))
    if counts['recent'] >= 3:
        signal('repeated_cancellations',20,f"{counts['recent']} annulations documentées après acceptation en 7 jours.")
    if record.actor_id and counts['same_client'] >= 2:
        signal('repeated_pair',20,f"{counts['same_client']} annulations documentées avec le même client en 30 jours.")
    if counts['reporters'] >= 2:
        signal('multiple_reporters',20,f"Signalements de {counts['reporters']} clients distincts en 30 jours.")
    # Compare the same creation cohort, not recent cancellations against older jobs.
    assigned = Ride.objects.filter(driver_id=record.partner_id, requested_at__gte=cutoff, requested_at__lte=now).count()
    assigned += DeliveryRequest.objects.filter(assigned_driver__user_id=record.partner_id,created_at__gte=cutoff,created_at__lte=now).count()
    documented=events.filter(Q(ride__requested_at__gte=cutoff)|Q(delivery__created_at__gte=cutoff)).count()
    ratio=round(documented*100/assigned,1) if assigned else None
    if assigned>=5 and ratio is not None and 40 <= ratio <= 100:
        signal('cancellation_ratio',20,f"{documented} annulations documentées sur {assigned} prestations assignées de la période ({ratio} %).")
    result['score']=min(100,sum(s['points'] for s in signals))
    result['metrics'].update(counts, assigned_jobs=assigned, documented_cancellations=documented, documented_ratio_percent=ratio)
    return result


def process_pending(limit=50):
    ids=list(CancellationRecord.objects.filter(analysis_pending=True).order_by('pk').values_list('pk',flat=True)[:limit])
    processed=0
    for pk in ids:
        try:
            analyse(pk)
            processed+=1
        except Exception:
            logger.exception('Cancellation analysis failed record=%s; retained for retry',pk)
    return processed
