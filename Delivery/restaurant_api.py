"""Restaurant catalogue and operator workflow. No provider debit is simulated."""
import hashlib
import json
import math
import logging
from decimal import Decimal
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers, viewsets, mixins
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from blaze_backend.supervision import CanSupervise
from .models import Restaurant, RestaurantMenuItem, RestaurantOrder, RestaurantOrderEvent, RestaurantSettlement, DeliveryRequest


def validate_photo(value):
    if value and (value.size > 5 * 1024 * 1024 or getattr(value.image, 'format', '') not in ('JPEG', 'PNG', 'WEBP')):
        raise ValidationError('Photo JPG, PNG ou WebP de 5 Mo maximum.')
    return value


class RestaurantSerializer(serializers.ModelSerializer):
    validate_photo = staticmethod(validate_photo)
    class Meta:
        model = Restaurant
        fields = ('id','name','city','address','latitude','longitude','phone','photo','is_active','is_open','periodic_payment','settlement_terms')
    def validate(self, attrs):
        for key, limit in [('latitude',90),('longitude',180)]:
            value = attrs.get(key, getattr(self.instance,key,None))
            if value is None or not math.isfinite(value) or abs(value) > limit:
                raise ValidationError({key:'Position invalide.'})
        if attrs.get('periodic_payment',getattr(self.instance,'periodic_payment',False)) and not attrs.get('settlement_terms',getattr(self.instance,'settlement_terms','')).strip():
            raise ValidationError({'settlement_terms':'Précisez les modalités convenues avec le restaurant.'})
        return attrs


class PublicRestaurantSerializer(RestaurantSerializer):
    class Meta(RestaurantSerializer.Meta):
        fields = ('id','name','city','address','latitude','longitude','photo','is_open')


class MenuSerializer(serializers.ModelSerializer):
    price = serializers.IntegerField(min_value=1,max_value=1000000)
    validate_photo = staticmethod(validate_photo)
    class Meta:
        model = RestaurantMenuItem
        fields = ('id','restaurant','name','description','category','price','photo','is_available')


class RestaurantCatalog(viewsets.ReadOnlyModelViewSet):
    permission_classes = (IsAuthenticated,)
    serializer_class = PublicRestaurantSerializer
    pagination_class = None
    def get_queryset(self):
        qs = Restaurant.objects.filter(is_active=True).order_by('name','pk')
        if self.request.query_params.get('city'):
            qs = qs.filter(city=self.request.query_params['city'])
        return qs
    @action(detail=True,methods=['get'])
    def menu(self,request,pk=None):
        restaurant = self.get_object()
        return Response(MenuSerializer(restaurant.menu.filter(is_available=True).order_by('category','name'),many=True,context={'request':request}).data)


class RestaurantAdmin(viewsets.ModelViewSet):
    permission_classes = (CanSupervise,)
    serializer_class = RestaurantSerializer
    queryset = Restaurant.objects.order_by('name','pk')
    http_method_names = ['get','post','patch','head','options']
    pagination_class = None


class MenuAdmin(viewsets.ModelViewSet):
    permission_classes = (CanSupervise,)
    serializer_class = MenuSerializer
    http_method_names = ['get','post','patch','head','options']
    pagination_class = None
    def get_queryset(self):
        qs=RestaurantMenuItem.objects.order_by('restaurant','category','name')
        if self.request.query_params.get('restaurant'):
            qs=qs.filter(restaurant_id=self.request.query_params['restaurant'])
        return qs


class CartLine(serializers.Serializer):
    item_id=serializers.IntegerField(min_value=1)
    quantity=serializers.IntegerField(min_value=1,max_value=20)


def canonical_cart(restaurant_id, cart):
    restaurant=get_object_or_404(Restaurant,is_active=True,is_open=True,pk=restaurant_id)
    if not cart or len(cart)>30:
        raise ValidationError({'cart':'Choisissez entre 1 et 30 plats.'})
    ids=[line['item_id'] for line in cart]
    if len(set(ids))!=len(ids):
        raise ValidationError({'cart':'Un plat ne doit figurer qu’une fois ; utilisez la quantité.'})
    items={item.pk:item for item in restaurant.menu.filter(pk__in=ids,is_available=True)}
    if len(items)!=len(ids):
        raise ValidationError({'cart':'Un plat est indisponible ou appartient à un autre restaurant.'})
    lines=[{'item_id':line['item_id'],'name':items[line['item_id']].name,'quantity':line['quantity'],'unit_price':items[line['item_id']].price} for line in sorted(cart,key=lambda row:row['item_id'])]
    total=sum(row['quantity']*row['unit_price'] for row in lines)
    if total<1 or total>1000000:
        raise ValidationError({'cart':'Le total des repas doit être compris entre 1 et 1 000 000 FCFA.'})
    return restaurant,lines,total


def public_order(order):
    return {'id':order.pk,'restaurant_id':order.restaurant_id,'restaurant_name':order.restaurant_name,'lines':order.lines,'food_total':order.food_total,'state':order.state,'collected_at':order.collected_at,'collected_total':order.collected_total}


def food_order(req):
    try: return req.restaurant_order
    except RestaurantOrder.DoesNotExist: return None


def checkout_fingerprint(data):
    keys=('type','vehicle_type','pickup_zone','dropoff_zone','pickup_label','pickup_lat','pickup_lng','dropoff_label','dropoff_lat','dropoff_lng','contact_name','contact_phone','note','order_details')
    return hashlib.sha256(json.dumps({k:data.get(k) for k in keys},sort_keys=True,default=str).encode()).hexdigest()


class OperatorOrderSerializer(serializers.ModelSerializer):
    delivery = serializers.SerializerMethodField()
    events = serializers.SerializerMethodField()
    collected_blaze_due = serializers.SerializerMethodField()
    class Meta:
        model=RestaurantOrder
        fields=('id','restaurant','restaurant_name','lines','food_total','state','restaurant_reference','funding','settlement_terms','restaurant_paid_at','restaurant_payment_reference','collected_at','collected_total','version','created_at','delivery','events','collected_blaze_due','cash_remitted_at','cash_remitted_amount','cash_remittance_reference','settlement')
    def get_delivery(self,obj):
        req=obj.request
        return {'id':req.pk,'status':req.status,'assigned_driver':req.assigned_driver_id,'delivery_fee':req.final_price if req.final_price is not None else req.estimated_price,'is_paid':req.is_paid,'dropoff_label':req.dropoff_label,'contact_name':req.contact_name,'contact_phone':req.contact_phone}
    def get_events(self,obj):
        return list(obj.events.values('id','action','actor_id','details','created_at'))
    def get_collected_blaze_due(self,obj):
        if not obj.collected_at:return None
        allocation=getattr(obj.request,'payment_allocation',None)
        if allocation is None:return None
        due=Decimal(obj.food_total)+allocation.platform_amount
        return str(max(Decimal(0),due-(obj.cash_remitted_amount or 0)))


class SettlementLine(serializers.Serializer):
    id=serializers.IntegerField(min_value=1)
    version=serializers.IntegerField(min_value=1)


class SettlementInput(serializers.Serializer):
    restaurant=serializers.IntegerField(min_value=1)
    orders=SettlementLine(many=True,allow_empty=False,max_length=100)
    idempotency_key=serializers.UUIDField()
    payment_reference=serializers.CharField(max_length=160,allow_blank=False)
    paid=serializers.BooleanField()
    def validate_paid(self,value):
        if not value:raise ValidationError('Confirmez le paiement déjà effectué au restaurant.')
        return value


class OperatorOrders(mixins.ListModelMixin,mixins.RetrieveModelMixin,viewsets.GenericViewSet):
    permission_classes=(CanSupervise,)
    serializer_class=OperatorOrderSerializer
    queryset=RestaurantOrder.objects.select_related('request','request__payment_allocation','restaurant').prefetch_related('events').order_by('-pk')
    def get_queryset(self):
        qs=super().get_queryset()
        if self.request.query_params.get('restaurant'):
            field=serializers.IntegerField(min_value=1)
            qs=qs.filter(restaurant_id=field.run_validation(self.request.query_params['restaurant']))
        if self.request.query_params.get('unsettled')=='1':qs=qs.filter(funding='contract_due',restaurant_paid_at__isnull=True).exclude(request__status__in=['cancelled','failed'])
        return qs

    @action(detail=False,methods=['get'])
    def balances(self,request):
        from django.db.models import Sum, Count, F, DecimalField, ExpressionWrapper
        restaurant_due=list(RestaurantOrder.objects.filter(funding='contract_due',restaurant_paid_at__isnull=True).exclude(request__status__in=['cancelled','failed']).values('restaurant_id','restaurant_name').annotate(amount=Sum('food_total'),count=Count('pk')).order_by('restaurant_name'))
        cash_due=list(RestaurantOrder.objects.filter(collected_at__isnull=False,cash_remitted_at__isnull=True,request__payment_allocation__isnull=False).values('request__assigned_driver_id','request__assigned_driver__full_name').annotate(amount=Sum(ExpressionWrapper(F('food_total')+F('request__payment_allocation__platform_amount'),output_field=DecimalField(max_digits=14,decimal_places=2))),count=Count('pk')).order_by('request__assigned_driver_id'))
        return Response({'restaurants':restaurant_due,'couriers':cash_due})

    @action(detail=False,methods=['post'],url_path='settle-batch')
    def settle_batch(self,request):
        serializer=SettlementInput(data=request.data);serializer.is_valid(raise_exception=True)
        data=serializer.validated_data
        rows=sorted(data['orders'],key=lambda row:row['id'])
        ids=[row['id'] for row in rows]
        if len(set(ids))!=len(ids):raise ValidationError('Une commande ne peut apparaître qu’une fois.')
        with transaction.atomic():
            # Serialise submissions per restaurant, including retries with the same key.
            restaurant=get_object_or_404(Restaurant.objects.select_for_update(),pk=data['restaurant'])
            existing=RestaurantSettlement.objects.filter(idempotency_key=data['idempotency_key']).first()
            if existing:
                if existing.restaurant_id!=restaurant.pk or existing.order_versions!=rows or existing.payment_reference!=data['payment_reference']:
                    return Response({'detail':'Cette référence de traitement est déjà utilisée.'},status=409)
                return Response({'id':existing.pk,'amount':existing.amount,'orders':ids})
            candidate=list(RestaurantOrder.objects.filter(pk__in=ids).values_list('request_id',flat=True))
            deliveries={row.pk:row for row in DeliveryRequest.objects.select_for_update().filter(pk__in=candidate).order_by('pk')}
            orders=list(RestaurantOrder.objects.select_for_update().filter(pk__in=ids).order_by('pk'))
            if len(orders)!=len(ids):raise ValidationError('Commande inconnue.')
            for order,row in zip(orders,rows):
                if order.version!=row['version']:return Response({'detail':'Une commande a changé. Rechargez avant de confirmer.'},status=409)
                if order.restaurant_id!=restaurant.pk or order.funding!='contract_due' or order.restaurant_paid_at or deliveries[order.request_id].status in ('cancelled','failed'):
                    raise ValidationError('Les commandes doivent appartenir au même restaurant et être à régler selon contrat.')
            batch=RestaurantSettlement.objects.create(restaurant=restaurant,actor=request.user,idempotency_key=data['idempotency_key'],payment_reference=data['payment_reference'],amount=sum(order.food_total for order in orders),order_versions=rows)
            now=timezone.now()
            for order in orders:
                order.restaurant_paid_at=now;order.restaurant_payment_reference=data['payment_reference'];order.settlement=batch;order.version+=1
                order.save(update_fields=['restaurant_paid_at','restaurant_payment_reference','settlement','version'])
                RestaurantOrderEvent.objects.create(order=order,actor=request.user,action='settle_batch',details={'batch_id':batch.pk,'reference':batch.payment_reference,'amount':order.food_total})
        return Response({'id':batch.pk,'amount':batch.amount,'orders':ids},status=201)

    @action(detail=True,methods=['post'])
    def advance(self,request,pk=None):
        # Lock the delivery before the restaurant order everywhere (also cash and cancellation).
        order=self.get_object()
        with transaction.atomic():
            req=DeliveryRequest.objects.select_for_update().get(pk=order.request_id)
            order=RestaurantOrder.objects.select_for_update().get(pk=order.pk)
            action_name=request.data.get('action')
            if request.data.get('version')!=order.version:
                return Response({'detail':'Cette commande a changé. Rechargez-la.'},status=409)
            if req.status in ('cancelled','failed'):
                raise ValidationError('La commande n’est plus active.')
            details={}
            if action_name=='confirm':
                if order.state!='pending' or req.status!='operator_required':
                    raise ValidationError('Cette commande a déjà été traitée.')
                ref=str(request.data.get('reference','')).strip()
                funding=request.data.get('funding')
                if not ref or len(ref)>100:
                    raise ValidationError('Indiquez la référence de commande du restaurant (100 caractères maximum).')
                if funding=='contract_due':
                    if not order.restaurant.periodic_payment or not order.restaurant.settlement_terms:
                        raise ValidationError('Aucun accord de paiement périodique n’est configuré pour ce restaurant.')
                    order.settlement_terms=order.restaurant.settlement_terms
                elif funding=='paid':
                    payment_ref=str(request.data.get('payment_reference','')).strip()
                    if request.data.get('paid') is not True or not payment_ref or len(payment_ref)>160:
                        raise ValidationError('Confirmez le paiement réellement effectué et son justificatif.')
                    order.restaurant_paid_at=timezone.now()
                    order.restaurant_payment_reference=payment_ref
                else: raise ValidationError('Choisissez le paiement réel ou le contrat périodique.')
                order.state='confirmed';order.funding=funding;order.restaurant_reference=ref
                details={'funding':funding,'reference':ref,'payment_reference':order.restaurant_payment_reference,'food_total':order.food_total}
            elif action_name=='reject':
                if order.state!='pending' or req.status!='operator_required' or order.restaurant_paid_at or req.is_paid:
                    raise ValidationError('Le restaurant est déjà engagé. Traitez les montants avec l’assistance.')
                reason=str(request.data.get('reason','')).strip()
                if not reason or len(reason)>500:raise ValidationError('Précisez le motif du refus (500 caractères maximum).')
                req.status='failed';req.save(update_fields=['status','updated_at'])
                details={'reason':reason}
                from .services.dispatch import publish_delivery_status
                transaction.on_commit(lambda:publish_delivery_status(req),robust=True)
            elif action_name=='ready':
                if order.state!='confirmed':raise ValidationError('Confirmez d’abord la commande au restaurant.')
                order.state='ready'
                req.status='searching';req.save(update_fields=['status','updated_at'])
                transaction.on_commit(lambda: dispatch_restaurant(req.pk),robust=True)
            elif action_name=='retry_dispatch':
                if order.state!='ready' or req.status!='no_driver':raise ValidationError('La recherche ne peut pas être relancée.')
                req.status='searching';req.save(update_fields=['status','updated_at'])
                transaction.on_commit(lambda: dispatch_restaurant(req.pk),robust=True)
            elif action_name=='record_remittance':
                allocation=getattr(req,'payment_allocation',None)
                if not order.collected_at or not allocation or order.cash_remitted_at:
                    raise ValidationError('Aucun reversement à enregistrer pour cette commande.')
                amount=Decimal(order.food_total)+allocation.platform_amount
                ref=str(request.data.get('payment_reference','')).strip()
                try:received_amount=Decimal(str(request.data.get('amount','')))
                except Exception:raise ValidationError('Indiquez le montant reçu par Blaze.')
                if request.data.get('received') is not True or not ref or len(ref)>160 or not received_amount.is_finite() or received_amount!=amount:
                    raise ValidationError('Confirmez le montant exact reçu par Blaze et sa référence.')
                order.cash_remitted_at=timezone.now();order.cash_remitted_amount=amount;order.cash_remittance_reference=ref
                details={'reference':ref,'amount':str(amount),'driver_id':req.assigned_driver_id}
            elif action_name=='settle_restaurant':
                if order.funding!='contract_due' or order.restaurant_paid_at:raise ValidationError('Aucun règlement restaurant en attente.')
                payment_ref=str(request.data.get('payment_reference','')).strip()
                if request.data.get('paid') is not True or not payment_ref or len(payment_ref)>160:raise ValidationError('Confirmez le règlement réellement effectué et sa référence.')
                order.restaurant_paid_at=timezone.now();order.restaurant_payment_reference=payment_ref
                details={'reference':payment_ref,'amount':order.food_total}
            else:raise ValidationError('Action inconnue.')
            order.version+=1;order.save()
            RestaurantOrderEvent.objects.create(order=order,actor=request.user,action=action_name,details=details)
        return Response(OperatorOrderSerializer(self.queryset.get(pk=order.pk)).data)


def dispatch_restaurant(pk):
    from .services.dispatch import create_offers_for_request,publish_delivery_status
    try:
        with transaction.atomic():
            req=DeliveryRequest.objects.select_for_update().get(pk=pk)
            if req.status!='searching':return
            if create_offers_for_request(req)==0:
                req.status='no_driver';req.save(update_fields=['status','updated_at'])
            transaction.on_commit(lambda: publish_delivery_status(req),robust=True)
    except Exception:
        logging.getLogger(__name__).exception('Restaurant dispatch failed for delivery %s',pk)
        # Rollback partial offers and expose an explicit, retryable operator state.
        with transaction.atomic():
            req=DeliveryRequest.objects.select_for_update().get(pk=pk)
            if req.status=='searching':
                req.status='no_driver';req.save(update_fields=['status','updated_at'])
                transaction.on_commit(lambda: publish_delivery_status(req),robust=True)
