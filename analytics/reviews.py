from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from django.shortcuts import get_object_or_404
from rest_framework import generics, serializers
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.pagination import PageNumberPagination
from blaze_backend.supervision import CanSupervise
from .models import CancellationAlert, CancellationRecord, CancellationReview
from .detection_tasks import schedule_detection

REASONS = {'partner_requested':'Annulation demandée par le partenaire','off_platform_payment':'Paiement hors Be proposé','partner_absent':'Partenaire absent','wait_too_long':'Attente trop longue','plans_changed':'Changement de besoin','wrong_address':'Erreur d’adresse','other':'Autre','search_timeout':'Recherche expirée','legacy_unspecified':'Motif absent (ancienne version)'}
STATUSES = ('open','reviewing','dismissed','action_required','resolved')


def record_data(record):
    ride=record.ride_id is not None
    return {'id':record.pk,'service':'vtc' if ride else 'delivery','job_id':record.ride_id if ride else record.delivery_id,'client_id':record.actor_id if record.actor_role=='customer' else None,'partner_id':record.partner_id,'reason':record.reason,'reason_label':REASONS.get(record.reason,record.reason),'note':record.note,'status_before':record.status_before,'created_at':record.created_at,'context':record.context}


class AlertSerializer(serializers.ModelSerializer):
    record = serializers.SerializerMethodField()
    partner_name = serializers.SerializerMethodField()
    partner_path = serializers.SerializerMethodField()
    class Meta:
        model=CancellationAlert
        fields=('id','record','score','signals','metrics','status','version','created_at','updated_at','partner_name','partner_path')
    def get_record(self,obj): return record_data(obj.record)
    def get_partner_name(self,obj):
        partner=obj.record.partner
        if not partner:return 'Partenaire supprimé'
        profile=getattr(partner,'driver_profile',None) if obj.record.ride_id else getattr(partner,'delivery_driver_profile',None)
        return (getattr(profile,'full_name','') or f'{partner.first_name} {partner.last_name}'.strip() or f'Partenaire #{partner.pk}')
    def get_partner_path(self,obj):
        partner=obj.record.partner
        if not partner:return None
        profile=getattr(partner,'driver_profile',None) if obj.record.ride_id else getattr(partner,'delivery_driver_profile',None)
        if not profile:return None
        return f'/drivers/{profile.pk}' if obj.record.ride_id else f'/delivery-drivers/{profile.pk}'


def alerts():
    return CancellationAlert.objects.select_related('record__partner__driver_profile','record__partner__delivery_driver_profile').order_by('-score','-created_at','-pk')


class AlertPagination(PageNumberPagination):
    page_size=25
    def get_paginated_response(self,data):
        response=super().get_paginated_response(data)
        response.data['pending_analysis']=CancellationRecord.objects.filter(analysis_pending=True).count()
        response.data['legacy_last_30_days']=CancellationRecord.objects.filter(created_at__gte=timezone.now()-timedelta(days=30),reason='legacy_unspecified').count()
        response['Cache-Control']='no-store, private'
        return response


class AlertListView(generics.ListAPIView):
    permission_classes=(CanSupervise,)
    serializer_class=AlertSerializer
    pagination_class=AlertPagination
    def get_queryset(self):
        schedule_detection()
        qs=alerts()
        status=self.request.query_params.get('status','open')
        service=self.request.query_params.get('service','all')
        if status not in (*STATUSES,'all'): raise serializers.ValidationError({'status':'Filtre invalide.'})
        if service not in ('vtc','delivery','all'): raise serializers.ValidationError({'service':'Filtre invalide.'})
        if status!='all':qs=qs.filter(status=status)
        if service=='vtc':qs=qs.filter(record__ride__isnull=False)
        if service=='delivery':qs=qs.filter(record__delivery__isnull=False)
        return qs


class ReviewInput(serializers.Serializer):
    status=serializers.ChoiceField(choices=STATUSES)
    note=serializers.CharField(min_length=3,max_length=1000)
    version=serializers.IntegerField(min_value=1)


class AlertDetailView(APIView):
    permission_classes=(CanSupervise,)
    def get(self,request,pk):
        alert=get_object_or_404(alerts(),pk=pk)
        data=AlertSerializer(alert).data
        data['reviews']=list(alert.reviews.order_by('created_at','pk').values('id','reviewer_id','previous_status','status','note','created_at'))
        data['related_records']=[record_data(r) for r in CancellationRecord.objects.filter(partner_id=alert.record.partner_id).exclude(pk=alert.record_id).order_by('-created_at')[:10]] if alert.record.partner_id else []
        response=Response(data);response['Cache-Control']='no-store, private';return response

    @transaction.atomic
    def patch(self,request,pk):
        values=ReviewInput(data=request.data);values.is_valid(raise_exception=True)
        alert=get_object_or_404(CancellationAlert.objects.select_for_update(),pk=pk)
        if alert.version!=values.validated_data['version']:
            return Response({'detail':'Ce dossier a été modifié par une autre personne. Rechargez-le avant de décider.'},status=409)
        CancellationReview.objects.create(alert=alert,reviewer=request.user,previous_status=alert.status,status=values.validated_data['status'],note=values.validated_data['note'])
        alert.status=values.validated_data['status'];alert.version+=1
        alert.save(update_fields=['status','version','updated_at'])
        return Response({'id':alert.pk,'status':alert.status,'version':alert.version})
