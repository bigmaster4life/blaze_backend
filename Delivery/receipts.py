"""Private, immutable customer receipts. No tax identifier is fabricated."""
import base64
from io import BytesIO
from xml.sax.saxutils import escape
from pathlib import Path
from decimal import Decimal
from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from RideVTC.models import Ride
from .models import DeliveryRequest,CustomerReceipt
from .restaurant_api import food_order

TERMINAL=('delivered','delivered_by_driver','confirmed_by_client')


def receipt_text(receipt):
    data=receipt.snapshot
    lines=['BLAZE — REÇU DE PRESTATION',f"Référence : BE-{receipt.number}",f"Émis le : {receipt.issued_at.isoformat()}",data['issuer']['name'],data['issuer']['address'],data['issuer']['phone']]
    if data['issuer']['tax_id']:lines.append('Identifiant fiscal : '+data['issuer']['tax_id'])
    lines += [f"Client : {data['customer']}",f"{data['service']} #{data['job_id']}",f"Départ / retrait : {data['pickup']}",f"Arrivée / remise : {data['dropoff']}",f"Date de prestation : {data['service_date']}",'']
    for item in data['lines']:
        lines.append(f"{item['quantity']} x {item['label']} : {item['amount']} FCFA")
    lines += ['',f"TOTAL PAYÉ : {data['total']} FCFA",f"Mode de paiement : {data['payment_method']}",'Paiement confirmé dans Be.']
    if not data['issuer']['tax_id']:lines.append('Identifiant fiscal de l’émetteur à compléter. Ce reçu ne constitue pas une attestation de conformité fiscale.')
    return '\n'.join(lines)


def pdf_receipt(receipt):
    import reportlab
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    font_dir=Path(reportlab.__file__).parent/'fonts'
    for name,filename in [('Blaze','Vera.ttf'),('BlazeBold','VeraBd.ttf')]:
        if name not in pdfmetrics.getRegisteredFontNames():pdfmetrics.registerFont(TTFont(name,str(font_dir/filename)))
    buffer=BytesIO()
    document=SimpleDocTemplate(buffer,pagesize=A4,rightMargin=42,leftMargin=42,topMargin=42,bottomMargin=48,title=f'Reçu Blaze BE-{receipt.number}',author='Blaze')
    green=colors.HexColor('#186C40')
    body=ParagraphStyle('body',fontName='Blaze',fontSize=10,leading=16,textColor=colors.HexColor('#26372E'),spaceAfter=6,splitLongWords=True)
    heading=ParagraphStyle('heading',parent=body,fontName='BlazeBold',fontSize=23,leading=29,textColor=green,spaceAfter=12)
    small=ParagraphStyle('small',parent=body,fontSize=8,leading=12,textColor=colors.HexColor('#526158'))
    right=ParagraphStyle('right',parent=body,alignment=TA_RIGHT)
    def para(value,style=body):return Paragraph(escape(str(value)).replace('\n','<br/>'),style)
    def money(value):
        number=Decimal(str(value))
        text=format(number,',.0f' if number==number.to_integral_value() else ',.2f')
        return text.replace(',',' ').replace('.',',')+' FCFA'
    data=receipt.snapshot;issuer=data['issuer']
    story=[para(issuer['name'],heading),para('REÇU DE PRESTATION'),para(f'BE-{receipt.number}',small),para(f'Émis le {receipt.issued_at:%d/%m/%Y à %H:%M} UTC',small),Spacer(1,12),para(issuer['address']),para(issuer['phone'])]
    if issuer['tax_id']:story.append(para('Identifiant fiscal : '+issuer['tax_id']))
    story += [Spacer(1,16),para('CLIENT : '+data['customer']),para(('Course VTC' if data['service']=='vtc' else 'Livraison')+f" #{data['job_id']}"),para('Date de prestation : '+data['service_date']),para('Départ / retrait : '+data['pickup']),para('Arrivée / remise : '+data['dropoff']),Spacer(1,18)]
    rows=[[para('Désignation'),para('Qté'),para('Prix unitaire',right),para('Montant',right)]]
    for item in data['lines']:
        unit=item.get('unit_price',Decimal(item['amount'])/item['quantity'])
        rows.append([para(item['label']),para(item['quantity']),para(money(unit),right),para(money(item['amount']),right)])
    table=Table(rows,colWidths=[230,50,107,124],repeatRows=1,hAlign='LEFT')
    table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#EAF3ED')),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),9),('RIGHTPADDING',(0,0),(-1,-1),9),('TOPPADDING',(0,0),(-1,-1),9),('BOTTOMPADDING',(0,0),(-1,-1),9),('LINEBELOW',(0,0),(-1,0),1,green),('LINEBELOW',(0,1),(-1,-1),.4,colors.HexColor('#DCE4DF'))]))
    story += [table,Spacer(1,18),para('TOTAL PAYÉ : '+money(data['total']),ParagraphStyle('total',parent=heading,fontSize=17,leading=23,alignment=TA_RIGHT)),para('Paiement : '+('Espèces' if str(data['payment_method']).lower()=='cash' else str(data['payment_method']))),para('Paiement confirmé dans Be.'),Spacer(1,14)]
    if not issuer['tax_id']:story.append(para('Identifiant fiscal de l’émetteur à compléter. Ce reçu ne constitue pas une attestation de conformité fiscale.',small))
    def footer(canvas,doc):
        canvas.saveState();canvas.setFont('Blaze',8);canvas.setFillColor(green);canvas.drawString(42,25,'Blaze | Justificatif client');canvas.drawRightString(A4[0]-42,25,f'Page {doc.page}');canvas.restoreState()
    document.build(story,onFirstPage=footer,onLaterPages=footer)
    return buffer.getvalue()


class ReceiptList(APIView):
    permission_classes=(IsAuthenticated,)
    def get(self,request):
        rows=[]
        for ride in Ride.objects.filter(user=request.user,status='completed').annotate(receipt_total=Coalesce('final_price','price')).filter(Q(customerreceipt__isnull=False)|Q(payments__status='SUCCESS',payments__amount=F('receipt_total'))).distinct().order_by('-pk')[:100]:
            rows.append({'service':'vtc','id':ride.pk,'date':ride.completed_at,'label':ride.dropoff_location})
        for delivery in DeliveryRequest.objects.filter(user=request.user,is_paid=True,status__in=TERMINAL).order_by('-pk')[:100]:
            rows.append({'service':'delivery','id':delivery.pk,'date':delivery.updated_at,'label':delivery.dropoff_label})
        response=Response(sorted(rows,key=lambda row:str(row['date']),reverse=True));response['Cache-Control']='no-store, private';return response


class ReceiptView(APIView):
    permission_classes=(IsAuthenticated,)
    def get(self,request,service,pk):
        if service not in ('vtc','delivery'):return Response(status=404)
        with transaction.atomic():
            model=Ride if service=='vtc' else DeliveryRequest
            job=get_object_or_404(model.objects.select_for_update(),pk=pk,user=request.user)
            field='ride' if service=='vtc' else 'delivery'
            receipt=CustomerReceipt.objects.filter(**{field:job}).first()
            if receipt is None:
                lines=[]
                if service=='vtc':
                    total=job.final_price if job.final_price is not None else job.price
                    payment=job.payments.filter(status='SUCCESS',amount=total).order_by('-pk').first()
                    if job.status!='completed' or not payment:raise ValidationError('Le reçu sera disponible après la course et la confirmation du paiement.')
                    lines=[{'quantity':1,'label':'Course VTC','amount':str(total)}]
                    method=payment.wallet;date=job.completed_at
                    pickup=job.pickup_location;dropoff=job.dropoff_location
                else:
                    if job.status not in TERMINAL or not job.is_paid:raise ValidationError('Le reçu sera disponible après la remise et la confirmation du paiement.')
                    order=food_order(job)
                    total=Decimal(job.final_price if job.final_price is not None else job.estimated_price)
                    lines=[{'quantity':1,'label':'Livraison','amount':str(total)}]
                    if order:
                        if not order.collected_at:raise ValidationError('L’encaissement des repas doit être confirmé.')
                        for item in order.lines:
                            lines.append({'quantity':item['quantity'],'label':item['name'],'unit_price':item['unit_price'],'amount':str(item['quantity']*item['unit_price'])})
                        total+=order.food_total
                    elif (job.order_details or {}).get('purchase_mode')=='buy_for_me':
                        if job.purchase_actual_amount is None:raise ValidationError('Le montant réel des achats doit être enregistré.')
                        if (job.order_details or {}).get('funding_mode')=='driver_advance' and not job.purchase_reimbursed_at:raise ValidationError('Le remboursement des achats doit être confirmé.')
                        lines.append({'quantity':1,'label':'Achats pour le client (ticket commerçant à conserver)','amount':str(job.purchase_actual_amount)})
                        total+=job.purchase_actual_amount
                    method=job.payment_method;date=job.updated_at
                    pickup=job.pickup_label;dropoff=job.dropoff_label
                snapshot={'issuer':{'name':getattr(settings,'BLAZE_BILLING_NAME','Blaze'),'address':getattr(settings,'BLAZE_BILLING_ADDRESS','3e cité Angondjé, commune d’Akanda'),'phone':getattr(settings,'BLAZE_BILLING_PHONE','+24177020273'),'tax_id':getattr(settings,'BLAZE_BILLING_TAX_ID','')},'customer':job.user.get_full_name() or f'Client #{job.user_id}','service':service,'job_id':job.pk,'pickup':pickup,'dropoff':dropoff,'service_date':date.isoformat() if date else '', 'lines':lines,'total':str(total),'currency':'XAF','payment_method':method}
                receipt=CustomerReceipt.objects.create(user=request.user,snapshot=snapshot,**{field:job})
        if request.query_params.get('download')=='base64':
            response=Response({'filename':f'Blaze-BE-{receipt.number}.pdf','content_type':'application/pdf','base64':base64.b64encode(pdf_receipt(receipt)).decode('ascii')})
        elif request.query_params.get('download')=='pdf':
            response=HttpResponse(pdf_receipt(receipt),content_type='application/pdf')
            response['Content-Disposition']=f'attachment; filename="Blaze-BE-{receipt.number}.pdf"'
        else:
            response=Response({'number':f'BE-{receipt.number}','issued_at':receipt.issued_at,'snapshot':receipt.snapshot,'text':receipt_text(receipt)})
        response['Cache-Control']='no-store, private'
        return response
