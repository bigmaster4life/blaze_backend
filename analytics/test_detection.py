from unittest.mock import patch
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate
from RideVTC.models import Ride
from .models import CancellationRecord, CancellationAlert, CancellationReview
from .detection import analyse, process_pending
from .reviews import AlertListView, AlertDetailView


class DetectionTests(TestCase):
    def setUp(self):
        User=get_user_model()
        self.customer=User.objects.create(email='client@detect.test')
        self.partner=User.objects.create(email='partner@detect.test',first_name='Partner')
        self.manager=User.objects.create(email='manager@detect.test',is_staff=True,user_type='manager_staff')
        self.employee=User.objects.create(email='employee@detect.test',is_staff=True,user_type='employee_staff')
        self.factory=APIRequestFactory()
        worker=patch('analytics.reviews.schedule_detection');worker.start();self.addCleanup(worker.stop)

    def event(self,reason='plans_changed',near=False,customer=None,role='customer'):
        customer=customer or self.customer
        ride=Ride.objects.create(user=customer,driver=self.partner,status='cancelled',pickup_location='A',dropoff_location='B',distance_km=1,price=1000)
        return CancellationRecord.objects.create(ride=ride,actor=customer,partner=self.partner,actor_role=role,reason=reason,status_before='accepted',note='Private report',context={'pickup_distance_m':100,'position_age_seconds':20} if near else {})

    def request(self,view,user=None,pk=None,body=None):
        req=self.factory.get('/alerts/') if body is None else self.factory.patch('/alerts/',body,format='json')
        if user:force_authenticate(req,user=user)
        return view.as_view()(req,**({'pk':pk} if pk else {}))

    def test_single_gps_signal_is_not_a_fraud_alert(self):
        event=self.event(near=True)
        self.assertIsNone(analyse(event.pk))
        event.refresh_from_db();self.assertFalse(event.analysis_pending)
        self.assertEqual(CancellationAlert.objects.count(),0)

    def test_direct_report_is_explained_and_analysis_is_idempotent(self):
        event=self.event(reason='partner_requested')
        alert=analyse(event.pk)
        self.assertEqual(alert.score,40)
        self.assertEqual(alert.signals[0]['code'],'explicit_report')
        self.assertIsNone(analyse(event.pk))
        self.assertEqual(CancellationAlert.objects.count(),1)

    def test_old_clients_timeouts_and_staff_actions_do_not_become_reports(self):
        for reason,role in [('legacy_unspecified','customer'),('search_timeout','customer'),('partner_requested','staff')]:
            self.assertIsNone(analyse(self.event(reason=reason,role=role,near=True).pk))

    def test_repeated_pair_and_cancellation_pattern_raise_alert_without_direct_report(self):
        self.event();self.event()
        alert=analyse(self.event().pk)
        self.assertIsNotNone(alert)
        self.assertEqual({s['code'] for s in alert.signals},{'repeated_cancellations','repeated_pair'})
        self.assertEqual(alert.score,40)

    def test_ratio_requires_a_minimum_sample_and_uses_matching_cohort(self):
        self.event();self.event()
        for _ in range(2):Ride.objects.create(user=self.customer,driver=self.partner,status='completed',pickup_location='A',dropoff_location='B',distance_km=1,price=1000)
        alert=analyse(self.event(near=True).pk)
        self.assertEqual(alert.metrics['assigned_jobs'],5)
        self.assertEqual(alert.metrics['documented_ratio_percent'],60)
        self.assertIn('cancellation_ratio',[s['code'] for s in alert.signals])

    def test_multiple_distinct_clients_strengthen_report(self):
        self.event(reason='partner_requested')
        other=get_user_model().objects.create(email='other@detect.test')
        alert=analyse(self.event(reason='off_platform_payment',customer=other).pk)
        self.assertIn('multiple_reporters',[s['code'] for s in alert.signals])

    def test_process_pending_retries_and_never_changes_partner_account(self):
        event=self.event(reason='partner_requested')
        self.assertEqual(process_pending(5),1)
        self.assertEqual(process_pending(5),0)
        self.partner.refresh_from_db();self.assertTrue(self.partner.is_active)
        self.assertEqual(CancellationReview.objects.count(),0)
        event.refresh_from_db();self.assertFalse(event.analysis_pending)

    def test_api_permissions_and_private_details(self):
        alert=analyse(self.event(reason='partner_requested').pk)
        for user in [None,self.customer,self.partner,self.employee]:
            self.assertIn(self.request(AlertListView,user).status_code,[401,403])
            self.assertIn(self.request(AlertDetailView,user,pk=alert.pk).status_code,[401,403])
        result=self.request(AlertListView,self.manager)
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.data['count'],1)
        self.assertEqual(result['Cache-Control'],'no-store, private')
        self.assertEqual(self.request(AlertDetailView,self.manager,pk=alert.pk).data['record']['note'],'Private report')

    def test_review_is_versioned_and_requires_justification(self):
        alert=analyse(self.event(reason='partner_requested').pk)
        self.assertEqual(self.request(AlertDetailView,self.manager,alert.pk,{'status':'dismissed','note':'','version':1}).status_code,400)
        data={'status':'reviewing','note':'Client contacté, vérification en cours.','version':1}
        self.assertEqual(self.request(AlertDetailView,self.manager,alert.pk,data).status_code,200)
        self.assertEqual(self.request(AlertDetailView,self.manager,alert.pk,data).status_code,409)
        self.assertEqual(CancellationReview.objects.count(),1)
        alert.refresh_from_db();self.assertEqual(alert.status,'reviewing');self.assertEqual(alert.version,2)
        self.partner.refresh_from_db();self.assertTrue(self.partner.is_active)

    def test_filter_validation(self):
        req=self.factory.get('/alerts/',{'status':'invalid'});force_authenticate(req,user=self.manager)
        self.assertEqual(AlertListView.as_view()(req).status_code,400)

    def test_failed_analysis_stays_pending_and_can_be_retried(self):
        event=self.event(reason='partner_requested')
        with patch('analytics.detection.evaluate',side_effect=RuntimeError('test failure')), self.assertLogs('analytics.detection',level='ERROR'):
            self.assertEqual(process_pending(),0)
        event.refresh_from_db();self.assertTrue(event.analysis_pending)
        self.assertEqual(process_pending(),1)
        self.assertEqual(CancellationAlert.objects.count(),1)
