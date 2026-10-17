from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate
from .models import CustomUser
from .staff import StaffUserCreateView
from .views import UserListView


class TeamAccountTests(TestCase):
    def setUp(self):
        self.manager = CustomUser.objects.create(email='manager@test.invalid', first_name='Gestionnaire', last_name='Test', user_type='manager_staff', is_staff=True)
        self.employee = CustomUser.objects.create(email='employee@test.invalid', user_type='employee_staff', is_staff=True)
        self.client_user = CustomUser.objects.create(email='client@test.invalid', user_type='client')
        self.owner = CustomUser.objects.create(email='owner@test.invalid', user_type='loueur')
        self.admin = CustomUser.objects.create(email='admin@test.invalid', is_superuser=True, is_staff=True)
        self.payload = {'first_name':'Alice','last_name':'Équipe','email':'NEW@TEST.INVALID','password':'Strong-Staff-589!','user_type':'employee_staff'}

    def create(self, actor, body=None):
        request=APIRequestFactory().post('/',body or self.payload,format='json')
        if actor: force_authenticate(request,actor)
        return StaffUserCreateView.as_view()(request)

    def directory(self, actor=None, params=None):
        request=APIRequestFactory().get('/', params or {})
        if actor: force_authenticate(request,actor)
        return UserListView.as_view()(request)

    def test_only_authorized_manager_can_create_an_employee(self):
        for actor in (None,self.client_user,self.employee,self.owner):
            self.assertIn(self.create(actor).status_code,(401,403))
        response=self.create(self.manager)
        self.assertEqual(response.status_code,201)
        self.assertNotIn('password',response.data)
        created=CustomUser.objects.get(pk=response.data['id'])
        self.assertEqual(created.email,'new@test.invalid')
        self.assertTrue(created.check_password(self.payload['password']))
        self.assertTrue(created.is_staff)
        self.assertFalse(created.is_superuser)
        self.assertFalse(created.email_verified)

    def test_only_superuser_can_create_manager_and_superuser_cannot_be_injected(self):
        payload={**self.payload,'user_type':'manager_staff','is_superuser':True}
        self.assertEqual(self.create(self.manager,payload).status_code,400)
        response=self.create(self.admin,payload)
        self.assertEqual(response.status_code,201)
        self.assertFalse(CustomUser.objects.get(pk=response.data['id']).is_superuser)

    def test_no_customer_or_driver_created_through_team_form(self):
        for role in ('client','customer','chauffeur','delivery_driver','loueur'):
            self.assertEqual(self.create(self.admin,{**self.payload,'user_type':role}).status_code,400)

    def test_password_validation_and_case_insensitive_unique_email(self):
        self.assertEqual(self.create(self.manager,{**self.payload,'password':'short'}).status_code,400)
        self.assertEqual(self.create(self.manager).status_code,201)
        self.assertEqual(self.create(self.manager,{**self.payload,'email':'New@test.invalid'}).status_code,400)

    def test_directory_is_not_public_and_does_not_expose_passwords(self):
        for actor in (None,self.client_user,self.owner):
            self.assertIn(self.directory(actor).status_code,(401,403))
        response=self.directory(self.employee)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.data['count'],5)
        self.assertNotIn('password',response.data['results'][0])
        self.assertIn('date_joined',response.data['results'][0])

    def test_team_filter_search_and_bounded_pagination(self):
        response=self.directory(self.manager,{'scope':'team','search':'GESTION','page_size':1})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.data['count'],1)
        self.assertEqual(response.data['results'][0]['id'],self.manager.pk)
        response=self.directory(self.manager,{'page_size':2})
        self.assertEqual(len(response.data['results']),2)
        self.assertIsNotNone(response.data['next'])

    def test_role_name_without_staff_flag_does_not_grant_management(self):
        self.manager.is_staff=False;self.manager.save()
        self.assertEqual(self.create(self.manager).status_code,403)
        self.assertEqual(self.directory(self.manager).status_code,403)
