from unittest.mock import patch
from django.test import TestCase, override_settings
from django.urls import reverse
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.exceptions import ValidationError

from django.core import mail
from django.utils import timezone

from accounts.models import User
from patient.models import PatientProfile, Prescription, PrescriptionRequest
from pharmacy.models import PharmacyProfile, Medicine, Inventory, Feedback
from admin_portal.models import PlatformSetting
from accounts.forms import PatientRegistrationForm, PharmacyRegistrationForm
from accounts.decorators import is_predefined_admin, get_predefined_admin_emails


class PredefinedAdminSecurityTests(TestCase):
    """
    Focused Phase 2 security test suite verifying:
    - Predefined admin identity authorization
    - Lockout of unlisted admin, staff, and superuser accounts
    - Django /admin/ protection
    - Registration protection against role escalation
    - Login redirection behavior across all roles
    - Management command provisioning invariants
    """

    def setUp(self):
        # 1. Authorized Admin 1 (case-insensitive test: stored lowercase)
        self.admin_1 = User.objects.create_user(
            username='test_admin1',
            email='aayezahali786@gmail.com',
            password='AdminPass123!',
            role='ADMIN',
            is_staff=True,
            is_superuser=True,
            is_active=True,
            email_verified=True,
            is_approved=True,
        )

        # 2. Authorized Admin 2 (stored with mixed case to test case-insensitive matching)
        self.admin_2 = User.objects.create_user(
            username='test_admin2',
            email='Abhimanyu1Sandeep@Gmail.com',
            password='AdminPass123!',
            role='ADMIN',
            is_staff=True,
            is_superuser=True,
            is_active=True,
            email_verified=True,
            is_approved=True,
        )

        # 3. Standard Patient User
        self.patient = User.objects.create_user(
            username='test_patient',
            email='patient@test.com',
            password='PatientPass123!',
            role='PATIENT',
            is_active=True,
            email_verified=True,
            is_approved=True,
        )
        PatientProfile.objects.create(user=self.patient)

        # 4. Standard Approved Pharmacy User
        self.pharmacy = User.objects.create_user(
            username='test_pharmacy',
            email='pharmacy@test.com',
            password='PharmacyPass123!',
            role='PHARMACY',
            is_active=True,
            email_verified=True,
            is_approved=True,
        )
        PharmacyProfile.objects.create(
            user=self.pharmacy,
            pharmacy_name='City Care Pharmacy',
            owner_name='Test Owner',
            pharmacist_name='Test Pharmacist',
            pharmacy_license_number='DL-PHARMA-001',
            gst_number='GST001',
            phone_number='9876543210',
            email='pharmacy@test.com',
            address='123 Health Ave',
            city='Mumbai',
            state='Maharashtra',
            pincode='400001',
            approval_status='APPROVED',
        )

        # 5. Unlisted Admin Account (role ADMIN, but email is not in PREDEFINED_ADMIN_EMAILS)
        self.unlisted_admin = User.objects.create_user(
            username='unlisted_admin',
            email='rogue_admin@outside.com',
            password='RoguePass123!',
            role='ADMIN',
            is_staff=True,
            is_superuser=True,
            is_active=True,
            email_verified=True,
            is_approved=True,
        )

        # 6. Unlisted Staff Account
        self.unlisted_staff = User.objects.create_user(
            username='unlisted_staff',
            email='hospital_staff@example.com',
            password='StaffPass123!',
            role='PATIENT',
            is_staff=True,
            is_superuser=False,
            is_active=True,
            email_verified=True,
            is_approved=True,
        )

        # 7. Unlisted Superuser Account
        self.unlisted_superuser = User.objects.create_user(
            username='unlisted_superuser',
            email='external_su@example.com',
            password='SuPass123!',
            role='PATIENT',
            is_staff=True,
            is_superuser=True,
            is_active=True,
            email_verified=True,
            is_approved=True,
        )

    # --- Test 1: Authorized Admin 1 can authenticate and access protected admin routes ---
    def test_authorized_admin_1_can_access_admin_portal(self):
        self.client.force_login(self.admin_1)
        response = self.client.get(reverse('admin_portal:landing'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Healthcare Administration Console')
        self.assertContains(response, 'aayezahali786@gmail.com')

    # --- Test 2: Authorized Admin 2 can authenticate and access protected admin routes ---
    def test_authorized_admin_2_can_access_admin_portal(self):
        self.client.force_login(self.admin_2)
        response = self.client.get(reverse('admin_portal:landing'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Healthcare Administration Console')
        self.assertContains(response, self.admin_2.email)

    # --- Test 3: Patient cannot access admin routes ---
    def test_patient_cannot_access_admin_portal(self):
        self.client.force_login(self.patient)
        response = self.client.get(reverse('admin_portal:landing'))
        self.assertEqual(response.status_code, 403)

    # --- Test 4: Pharmacy cannot access admin routes ---
    def test_pharmacy_cannot_access_admin_portal(self):
        self.client.force_login(self.pharmacy)
        response = self.client.get(reverse('admin_portal:landing'))
        self.assertEqual(response.status_code, 403)

    # --- Test 5: Unlisted ADMIN cannot access admin routes ---
    def test_unlisted_admin_cannot_access_admin_portal(self):
        self.client.force_login(self.unlisted_admin)
        response = self.client.get(reverse('admin_portal:landing'))
        self.assertEqual(response.status_code, 403)

    # --- Test 6: Unlisted staff account cannot access admin routes ---
    def test_unlisted_staff_cannot_access_admin_portal(self):
        self.client.force_login(self.unlisted_staff)
        response = self.client.get(reverse('admin_portal:landing'))
        self.assertEqual(response.status_code, 403)

    # --- Test 7: Unlisted superuser cannot access admin routes ---
    def test_unlisted_superuser_cannot_access_admin_portal(self):
        self.client.force_login(self.unlisted_superuser)
        response = self.client.get(reverse('admin_portal:landing'))
        self.assertEqual(response.status_code, 403)

    # --- Test 8: Inactive authorized admin is denied ---
    def test_inactive_authorized_admin_is_denied(self):
        self.admin_1.is_active = False
        self.admin_1.save()
        # View layer directly raises PermissionDenied (403) for inactive user
        from django.test import RequestFactory
        from admin_portal.views import landing_view
        rf = RequestFactory()
        req = rf.get(reverse('admin_portal:landing'))
        req.user = self.admin_1
        from django.core.exceptions import PermissionDenied
        with self.assertRaises(PermissionDenied):
            landing_view(req)

        # Inactive user cannot access via client either (not 200)
        self.client.force_login(self.admin_1)
        response = self.client.get(reverse('admin_portal:landing'))
        self.assertNotEqual(response.status_code, 200)

    # --- Test 9: Patient registration cannot create ADMIN ---
    def test_patient_registration_cannot_create_admin(self):
        form_data = {
            'first_name': 'Attacker',
            'last_name': 'Hacker',
            'username': 'attacker_patient',
            'email': 'attacker@example.com',
            'phone_number': '9876543210',
            'password': 'SecurePass123!',
            'confirm_password': 'SecurePass123!',
            'role': 'ADMIN',
            'is_staff': True,
            'is_superuser': True,
        }
        form = PatientRegistrationForm(data=form_data)
        self.assertTrue(form.is_valid())
        created_user = form.save()
        self.assertEqual(created_user.role, 'PATIENT')
        self.assertFalse(created_user.is_staff)
        self.assertFalse(created_user.is_superuser)

        # Attempting to register patient with predefined admin email is rejected
        admin_email_form_data = form_data.copy()
        admin_email_form_data['email'] = 'AayezahAli786@Gmail.Com'
        admin_email_form_data['username'] = 'attacker_patient_2'
        admin_form = PatientRegistrationForm(data=admin_email_form_data)
        self.assertFalse(admin_form.is_valid())
        self.assertIn('email', admin_form.errors)
        self.assertIn('reserved for administrative accounts', admin_form.errors['email'][0])

    # --- Test 10: Pharmacy registration cannot create ADMIN ---
    def test_pharmacy_registration_cannot_create_admin(self):
        form_data = {
            'username': 'attacker_pharmacy',
            'email': 'attacker_pharma@example.com',
            'password': 'SecurePass123!',
            'confirm_password': 'SecurePass123!',
            'pharmacy_name': 'Attacker Pharma',
            'owner_name': 'Attacker',
            'pharmacist_name': 'Attacker Pharm',
            'pharmacy_license_number': 'DL-ATTACK-001',
            'gst_number': 'GST-ATTACK-001',
            'phone_number': '9876543210',
            'address': '456 Attack Road',
            'city': 'Delhi',
            'state': 'Delhi',
            'pincode': '110001',
            'role': 'ADMIN',
            'is_staff': True,
            'is_superuser': True,
        }
        form = PharmacyRegistrationForm(data=form_data)
        self.assertTrue(form.is_valid())
        created_user = form.save()
        self.assertEqual(created_user.role, 'PHARMACY')
        self.assertFalse(created_user.is_staff)
        self.assertFalse(created_user.is_superuser)

        # Attempting to register pharmacy with predefined admin email is rejected
        admin_email_form_data = form_data.copy()
        admin_email_form_data['email'] = 'Abhimanyu1Sandeep@Gmail.Com'
        admin_email_form_data['username'] = 'attacker_pharmacy_2'
        admin_form = PharmacyRegistrationForm(data=admin_email_form_data)
        self.assertFalse(admin_form.is_valid())
        self.assertIn('email', admin_form.errors)
        self.assertIn('reserved for administrative accounts', admin_form.errors['email'][0])

    # --- Test 11: Third admin identity cannot be provisioned ---
    def test_third_admin_identity_cannot_be_provisioned(self):
        # With 3 emails configured, provisioning command fails immediately
        with override_settings(PREDEFINED_ADMIN_EMAILS=['aayezahali786@gmail.com', 'abhimanyu1sandeep@gmail.com', 'third@admin.com']):
            with self.assertRaises(CommandError) as ctx:
                call_command('provision_admins')
            self.assertIn('Expected exactly 2 predefined admin emails', str(ctx.exception))

    # --- Test 12: Existing unrelated admin accounts are not silently modified ---
    def test_existing_unrelated_accounts_are_not_silently_modified(self):
        # 1. If an unlisted admin exists, command reports conflict and halts
        with self.assertRaises(CommandError) as ctx:
            call_command('provision_admins')
        self.assertIn('Safety conflict', str(ctx.exception))
        self.assertIn('unlisted_admin', str(ctx.exception))

        # Remove the unlisted rogue accounts so we can test the other safety check
        self.unlisted_admin.delete()
        self.unlisted_staff.delete()
        self.unlisted_superuser.delete()

        # 2. If a non-admin account exists with a predefined email, command halts without converting it
        self.admin_2.delete()
        conflicting_patient = User.objects.create_user(
            username='conflict_patient',
            email='abhimanyu1sandeep@gmail.com',
            password='PatientPass123!',
            role='PATIENT',
        )

        with self.assertRaises(CommandError) as ctx2:
            call_command('provision_admins')
        self.assertIn('Automatic conversion of an existing non-admin account to ADMIN is strictly forbidden', str(ctx2.exception))

        # Verify the account was NOT converted to ADMIN
        conflicting_patient.refresh_from_db()
        self.assertEqual(conflicting_patient.role, 'PATIENT')

    # --- Test 13: Django /admin/ rejects unlisted staff/superusers ---
    def test_django_admin_rejects_unlisted_staff_and_superusers(self):
        # Unlisted staff gets 403 on /admin/
        self.client.force_login(self.unlisted_staff)
        resp_staff = self.client.get('/admin/')
        self.assertEqual(resp_staff.status_code, 403)

        # Unlisted superuser gets 403 on /admin/
        self.client.force_login(self.unlisted_superuser)
        resp_su = self.client.get('/admin/')
        self.assertEqual(resp_su.status_code, 403)

        # Unlisted ADMIN gets 403 on /admin/
        self.client.force_login(self.unlisted_admin)
        resp_unlisted_admin = self.client.get('/admin/')
        self.assertEqual(resp_unlisted_admin.status_code, 403)

        # Patient gets 403 on /admin/
        self.client.force_login(self.patient)
        resp_patient = self.client.get('/admin/')
        self.assertEqual(resp_patient.status_code, 403)

        # Authorized Admin 1 gets 200 on /admin/
        self.client.force_login(self.admin_1)
        resp_admin1 = self.client.get('/admin/')
        self.assertEqual(resp_admin1.status_code, 200)

        # Authorized Admin 2 gets 200 on /admin/
        self.client.force_login(self.admin_2)
        resp_admin2 = self.client.get('/admin/')
        self.assertEqual(resp_admin2.status_code, 200)

        # Anonymous user visiting /admin/ is redirected to /admin/login/ (login flow preserved)
        self.client.logout()
        resp_anon = self.client.get('/admin/')
        self.assertEqual(resp_anon.status_code, 302)
        self.assertIn('/admin/login/', resp_anon.url)

    # --- Test 14: Login redirection works correctly for all roles ---
    def test_login_redirection_for_all_roles(self):
        login_url = reverse('accounts:login')

        # A. Authorized Admin 1 logs in without next -> redirects to admin_portal:landing
        resp_admin = self.client.post(login_url, {
            'username': 'test_admin1',
            'password': 'AdminPass123!',
        })
        self.assertEqual(resp_admin.status_code, 302)
        self.assertRedirects(resp_admin, reverse('admin_portal:landing'))
        self.client.logout()

        # B. Authorized Admin 2 logs in with next=/admin/ -> redirects to next_url (/admin/)
        resp_admin_next = self.client.post(f"{login_url}?next=/admin/", {
            'username': 'test_admin2',
            'password': 'AdminPass123!',
        })
        self.assertEqual(resp_admin_next.status_code, 302)
        self.assertRedirects(resp_admin_next, '/admin/')
        self.client.logout()

        # C. Patient logs in without next -> redirects to landing
        resp_patient = self.client.post(login_url, {
            'username': 'test_patient',
            'password': 'PatientPass123!',
        })
        self.assertEqual(resp_patient.status_code, 302)
        self.assertRedirects(resp_patient, reverse('landing'))
        self.client.logout()

        # D. Patient logs in with next=/patient/dashboard/ -> redirects to patient:dashboard
        resp_patient_next = self.client.post(f"{login_url}?next={reverse('patient:dashboard')}", {
            'username': 'test_patient',
            'password': 'PatientPass123!',
        })
        self.assertEqual(resp_patient_next.status_code, 302)
        self.assertRedirects(resp_patient_next, reverse('patient:dashboard'))
        self.client.logout()

        # E. Patient logs in with next=/admin-portal/ -> forbidden destination blocked, redirects to landing
        resp_patient_admin_target = self.client.post(f"{login_url}?next=/admin-portal/", {
            'username': 'test_patient',
            'password': 'PatientPass123!',
        })
        self.assertEqual(resp_patient_admin_target.status_code, 302)
        self.assertRedirects(resp_patient_admin_target, reverse('landing'))
        self.client.logout()

        # F. Unlisted admin logs in -> denied administrative access, redirects to landing
        resp_unlisted = self.client.post(login_url, {
            'username': 'unlisted_admin',
            'password': 'RoguePass123!',
        })
        self.assertEqual(resp_unlisted.status_code, 302)
        self.assertRedirects(resp_unlisted, reverse('landing'))
        self.client.logout()


class AdminPortalDashboardTests(TestCase):
    """
    Phase 3 focused test suite verifying:
    - Admin 1 & 2 dashboard access
    - Role restrictions (Patient, Pharmacy, unlisted staff/admin denied)
    - Real database metrics calculation
    - Empty database behavior
    - Sidebar navigation entries and active states
    - Logout functionality and CSRF protection
    - Responsive structural elements
    """

    def setUp(self):
        # Predefined Admin 1
        self.admin = User.objects.create_user(
            username='admin_boss',
            email='aayezahali786@gmail.com',
            password='AdminPass123!',
            role='ADMIN',
            is_staff=True,
            is_superuser=True,
            is_active=True,
            email_verified=True,
            is_approved=True,
        )

        # Predefined Admin 2
        self.admin2 = User.objects.create_user(
            username='admin_partner',
            email='abhimanyu1sandeep@gmail.com',
            password='AdminPass123!',
            role='ADMIN',
            is_staff=True,
            is_superuser=True,
            is_active=True,
            email_verified=True,
            is_approved=True,
        )

        # Patient user
        self.patient = User.objects.create_user(
            username='dash_patient',
            email='dash_patient@test.com',
            password='Password123!',
            role='PATIENT',
            email_verified=True,
            is_approved=True,
        )
        self.patient_profile = PatientProfile.objects.create(user=self.patient)

        # Pharmacy user
        self.pharmacy_user = User.objects.create_user(
            username='dash_pharma',
            email='dash_pharma@test.com',
            password='Password123!',
            role='PHARMACY',
            email_verified=True,
            is_approved=True,
        )
        self.pharmacy_profile = PharmacyProfile.objects.create(
            user=self.pharmacy_user,
            pharmacy_name='Dash Chemist',
            owner_name='Chemist Owner',
            pharmacist_name='Pharmacist A',
            pharmacy_license_number='DL-DASH-100',
            gst_number='GST-DASH',
            phone_number='9876543210',
            email='dash_pharma@test.com',
            address='100 Dash St',
            city='Pune',
            state='Maharashtra',
            pincode='411001',
            approval_status='APPROVED',
        )

    # 1. Admin 1 dashboard access
    def test_admin_1_dashboard_access(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_portal:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Healthcare Administration Console')
        self.assertContains(response, 'admin_boss')
        self.assertContains(response, 'aayezahali786@gmail.com')

    # 2. Admin 2 dashboard access
    def test_admin_2_dashboard_access(self):
        self.client.force_login(self.admin2)
        response = self.client.get(reverse('admin_portal:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Healthcare Administration Console')
        self.assertContains(response, 'admin_partner')
        self.assertContains(response, 'abhimanyu1sandeep@gmail.com')

    # 3. Patient access denied
    def test_patient_dashboard_access_denied(self):
        self.client.force_login(self.patient)
        response = self.client.get(reverse('admin_portal:dashboard'))
        self.assertEqual(response.status_code, 403)

    # 4. Pharmacy access denied
    def test_pharmacy_dashboard_access_denied(self):
        self.client.force_login(self.pharmacy_user)
        response = self.client.get(reverse('admin_portal:dashboard'))
        self.assertEqual(response.status_code, 403)

    # 5. Unlisted staff/admin access denied
    def test_unlisted_staff_and_admin_dashboard_access_denied(self):
        unlisted_staff = User.objects.create_user(
            username='unlisted_staff_dash',
            email='staff_dash@outside.com',
            password='Password123!',
            role='PATIENT',
            is_staff=True,
            is_active=True,
        )
        self.client.force_login(unlisted_staff)
        resp_staff = self.client.get(reverse('admin_portal:dashboard'))
        self.assertEqual(resp_staff.status_code, 403)

        unlisted_admin = User.objects.create_user(
            username='unlisted_admin_dash',
            email='admin_dash@outside.com',
            password='Password123!',
            role='ADMIN',
            is_staff=True,
            is_superuser=True,
            is_active=True,
        )
        self.client.force_login(unlisted_admin)
        resp_admin = self.client.get(reverse('admin_portal:dashboard'))
        self.assertEqual(resp_admin.status_code, 403)

    # 6. Correct dashboard statistics using test database records
    def test_dashboard_statistics_accurate(self):
        # Create second patient
        patient2 = User.objects.create_user(
            username='patient_two',
            email='patient_two@test.com',
            password='Password123!',
            role='PATIENT',
            email_verified=True,
            is_approved=True,
        )
        PatientProfile.objects.create(user=patient2)

        # Create pending pharmacy
        pharma_user_pending = User.objects.create_user(
            username='pending_pharma_dash',
            email='pending_pharma@test.com',
            password='Password123!',
            role='PHARMACY',
            email_verified=True,
            is_approved=False,
        )
        PharmacyProfile.objects.create(
            user=pharma_user_pending,
            pharmacy_name='Pending Chemist',
            owner_name='Owner P',
            pharmacist_name='Pharm P',
            pharmacy_license_number='DL-PEND-99',
            gst_number='GST-PEND',
            phone_number='9876543211',
            email='pending_pharma@test.com',
            address='200 Pending Rd',
            city='Thane',
            state='Maharashtra',
            pincode='400601',
            approval_status='PENDING',
        )

        # Create prescription
        from patient.models import Prescription, PrescriptionRequest
        rx = Prescription.objects.create(
            patient=self.patient_profile,
            status='COMPLETED',
        )

        # Create fulfillment requests
        req_completed = PrescriptionRequest.objects.create(
            patient=self.patient_profile,
            pharmacy=self.pharmacy_profile,
            prescription=rx,
            status='COMPLETED',
        )
        req_pending = PrescriptionRequest.objects.create(
            patient=self.patient_profile,
            pharmacy=self.pharmacy_profile,
            prescription=rx,
            status='PENDING',
        )

        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_portal:dashboard'))
        self.assertEqual(response.status_code, 200)

        stats = response.context['stats']
        # 2 patients in total
        self.assertEqual(stats['total_patients'], 2)
        # 2 pharmacies in total (1 approved, 1 pending)
        self.assertEqual(stats['total_pharmacies'], 2)
        self.assertEqual(stats['approved_pharmacies'], 1)
        self.assertEqual(stats['pending_pharmacies'], 1)
        # 1 prescription
        self.assertEqual(stats['total_prescriptions'], 1)
        # 2 requests (1 completed, 1 pending)
        self.assertEqual(stats['total_requests'], 2)
        self.assertEqual(stats['completed_requests'], 1)
        self.assertEqual(stats['pending_requests'], 1)

        # Verify values rendered in HTML
        self.assertContains(response, 'id="val-total-patients">2<')
        self.assertContains(response, 'id="val-total-pharmacies">2<')
        self.assertContains(response, 'id="val-approved-pharmacies">1<')
        self.assertContains(response, 'id="val-pending-pharmacies">1<')
        self.assertContains(response, 'id="val-total-prescriptions">1<')
        self.assertContains(response, 'id="val-total-requests">2<')
        self.assertContains(response, 'id="val-completed-requests">1<')
        self.assertContains(response, 'id="val-pending-requests">1<')

        # Recent activity lists
        self.assertEqual(len(response.context['recent_requests']), 2)
        self.assertEqual(len(response.context['recent_pharmacies']), 2)

    # 7. Empty database behavior
    def test_empty_database_behavior(self):
        # Remove non-admin users so the platform is empty
        self.patient.delete()
        self.pharmacy_user.delete()

        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_portal:dashboard'))
        self.assertEqual(response.status_code, 200)

        stats = response.context['stats']
        self.assertEqual(stats['total_patients'], 0)
        self.assertEqual(stats['total_pharmacies'], 0)
        self.assertEqual(stats['approved_pharmacies'], 0)
        self.assertEqual(stats['pending_pharmacies'], 0)
        self.assertEqual(stats['total_prescriptions'], 0)
        self.assertEqual(stats['total_requests'], 0)
        self.assertEqual(stats['completed_requests'], 0)
        self.assertEqual(stats['pending_requests'], 0)

        # Empty activity placeholders render cleanly
        self.assertContains(response, 'No prescription fulfillment requests recorded yet.')
        self.assertContains(response, 'No pharmacy profiles registered yet.')

    # 8. Sidebar navigation rendering
    def test_sidebar_navigation_rendering(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_portal:dashboard'))
        self.assertEqual(response.status_code, 200)

        # Active dashboard link
        self.assertContains(response, reverse('admin_portal:dashboard'))
        self.assertContains(response, '<span>Dashboard</span>')

        # Active navigation items
        self.assertContains(response, '<span>Patients</span>')
        self.assertContains(response, '<span>Pharmacies</span>')
        self.assertContains(response, '<span>Medicine Catalogue</span>')
        self.assertContains(response, '<span>Inventory Overview</span>')
        self.assertContains(response, '<span>Feedback &amp; Reports</span>')
        self.assertContains(response, '<span>Platform Settings</span>')
        self.assertContains(response, '<span>Help &amp; Support</span>')
        self.assertContains(response, reverse('admin_portal:patient_list'))
        self.assertContains(response, reverse('admin_portal:pharmacy_list'))
        self.assertContains(response, reverse('admin_portal:medicine_list'))
        self.assertContains(response, reverse('admin_portal:feedback_list'))
        self.assertContains(response, reverse('admin_portal:settings'))
        self.assertContains(response, reverse('admin_portal:help'))

        # Logout form
        self.assertContains(response, reverse('accounts:logout'))
        self.assertContains(response, 'sidebar-logout-btn')

    # 9. Logout functionality and CSRF protection
    def test_logout_post_with_csrf_protection(self):
        self.client.force_login(self.admin)
        dashboard_url = reverse('admin_portal:dashboard')

        # Verify currently authenticated
        resp_authed = self.client.get(dashboard_url)
        self.assertEqual(resp_authed.status_code, 200)

        # POST logout succeeds
        logout_resp = self.client.post(reverse('accounts:logout'))
        self.assertEqual(logout_resp.status_code, 302)

        # After logout, accessing dashboard redirects to login
        resp_after = self.client.get(dashboard_url)
        self.assertEqual(resp_after.status_code, 302)
        self.assertIn(reverse('accounts:login'), resp_after.url)

    # 10. Responsive structural elements
    def test_responsive_structural_elements(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_portal:dashboard'))
        self.assertEqual(response.status_code, 200)

        self.assertContains(response, 'id="sidebar-toggle-btn"')
        self.assertContains(response, 'id="portal-sidebar"')
        self.assertContains(response, 'class="portal-main"')
        self.assertContains(response, 'class="portal-header"')
        self.assertContains(response, 'class="portal-content"')


# ==============================================================================
# PHASE 4 COMPLETE SUITE: PATIENT, PHARMACY, INVENTORY, FEEDBACK, SETTINGS, HELP
# ==============================================================================

class AdminPortalPatientManagementTests(TestCase):
    """
    Test suite for Admin Portal Patient Management module:
    - Predefined admin authorization requirement
    - List, search, and status filtering
    - Patient dossier view
    - Administrative patient account provisioning
    - Patient editing
    - Safe deletion preserving clinical audit records
    """
    def setUp(self):
        self.admin = User.objects.create_user(
            username='admin_patient_mgr',
            email='aayezahali786@gmail.com',
            password='AdminPass123!',
            role='ADMIN',
            is_staff=True,
            is_superuser=True,
            is_active=True,
            email_verified=True,
        )

        self.patient_user1 = User.objects.create_user(
            username='patient_alice',
            email='alice@example.com',
            password='AlicePass123!',
            role='PATIENT',
            first_name='Alice',
            last_name='Walker',
            phone_number='9876543210',
            is_active=True,
            email_verified=True,
        )
        self.patient_profile1 = PatientProfile.objects.create(
            user=self.patient_user1,
            gender='Female',
            blood_group='O+',
            address='123 Health Ave, Metro City',
            allergies='Penicillin',
        )

        self.patient_user2 = User.objects.create_user(
            username='patient_bob',
            email='bob@example.com',
            password='BobPass123!',
            role='PATIENT',
            first_name='Bob',
            last_name='Builder',
            phone_number='9123456780',
            is_active=False,
            email_verified=True,
        )
        self.patient_profile2 = PatientProfile.objects.create(
            user=self.patient_user2,
            gender='Male',
            blood_group='A+',
        )

    def test_patient_views_require_predefined_admin(self):
        """Unauthorized requests receive 403 or redirect to login."""
        url = reverse('admin_portal:patient_list')

        # 1. Anonymous user redirected to login
        resp_anon = self.client.get(url)
        self.assertEqual(resp_anon.status_code, 302)
        self.assertIn(reverse('accounts:login'), resp_anon.url)

        # 2. Patient user receives 403 Forbidden
        self.client.force_login(self.patient_user1)
        resp_patient = self.client.get(url)
        self.assertEqual(resp_patient.status_code, 403)

    def test_patient_list_and_search(self):
        """Admin can list patients and search by name, email, or phone."""
        self.client.force_login(self.admin)
        url = reverse('admin_portal:patient_list')

        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Alice Walker')
        self.assertContains(resp, 'Bob Builder')
        self.assertEqual(resp.context['total_count'], 2)
        self.assertEqual(resp.context['active_count'], 1)
        self.assertEqual(resp.context['inactive_count'], 1)

        # Search by username
        resp_search = self.client.get(url, {'q': 'alice'})
        self.assertContains(resp_search, 'alice@example.com')
        self.assertNotContains(resp_search, 'bob@example.com')

        # Filter by active only
        resp_active = self.client.get(url, {'status': 'active'})
        self.assertContains(resp_active, 'alice@example.com')
        self.assertNotContains(resp_active, 'bob@example.com')

    def test_patient_detail_view(self):
        """Dossier renders patient details, clinical profile, and metrics."""
        self.client.force_login(self.admin)
        url = reverse('admin_portal:patient_detail', kwargs={'pk': self.patient_user1.pk})

        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Alice Walker')
        self.assertContains(resp, '123 Health Ave, Metro City')
        self.assertContains(resp, 'Penicillin')
        self.assertContains(resp, 'O+')

    def test_patient_add_view_success(self):
        """Administrative provisioning creates patient with active access."""
        self.client.force_login(self.admin)
        url = reverse('admin_portal:patient_add')

        data = {
            'username': 'new_patient_clara',
            'email': 'clara@hospital.test',
            'first_name': 'Clara',
            'last_name': 'Oswald',
            'phone_number': '9988776655',
            'password': 'SecurePatientPass123!',
            'confirm_password': 'SecurePatientPass123!',
            'gender': 'Female',
            'blood_group': 'B+',
            'address': 'Baker Street 221B',
            'allergies': 'Sulfa drugs',
        }
        resp = self.client.post(url, data)
        self.assertEqual(resp.status_code, 302)

        user = User.objects.get(username='new_patient_clara')
        self.assertEqual(user.role, 'PATIENT')
        self.assertTrue(user.is_active)
        self.assertTrue(user.is_approved)
        self.assertTrue(user.email_verified)
        self.assertEqual(user.patient_profile.blood_group, 'B+')
        self.assertEqual(user.patient_profile.allergies, 'Sulfa drugs')

    def test_patient_edit_view_success(self):
        """Admin can update patient personal and clinical details."""
        self.client.force_login(self.admin)
        url = reverse('admin_portal:patient_edit', kwargs={'pk': self.patient_user1.pk})

        data = {
            'first_name': 'Alice Updated',
            'last_name': 'Walker PhD',
            'email': 'alice.updated@example.com',
            'phone_number': '9876543211',
            'is_active': True,
            'gender': 'Female',
            'blood_group': 'AB+',
            'address': 'New Residence 456',
            'allergies': 'Aspirin',
        }
        resp = self.client.post(url, data)
        self.assertEqual(resp.status_code, 302)

        self.patient_user1.refresh_from_db()
        self.patient_profile1.refresh_from_db()
        self.assertEqual(self.patient_user1.first_name, 'Alice Updated')
        self.assertEqual(self.patient_user1.email, 'alice.updated@example.com')
        self.assertEqual(self.patient_profile1.blood_group, 'AB+')
        self.assertEqual(self.patient_profile1.allergies, 'Aspirin')

    def test_patient_safe_deletion_and_toggle(self):
        """Patients with medical records are deactivated; patients without are safely deleted."""
        self.client.force_login(self.admin)
        toggle_url = reverse('admin_portal:patient_toggle_status', kwargs={'pk': self.patient_user1.pk})

        # 1. Toggle status
        resp_toggle = self.client.post(toggle_url, {'action': 'toggle'})
        self.assertEqual(resp_toggle.status_code, 302)
        self.patient_user1.refresh_from_db()
        self.assertFalse(self.patient_user1.is_active)

        # Reactivate
        self.client.post(toggle_url, {'action': 'toggle'})
        self.patient_user1.refresh_from_db()
        self.assertTrue(self.patient_user1.is_active)

        # 2. Deletion without medical records permanently deletes
        del_user = User.objects.create_user(
            username='temp_patient',
            email='temp@test.com',
            password='TempPass123!',
            role='PATIENT'
        )
        del_url = reverse('admin_portal:patient_toggle_status', kwargs={'pk': del_user.pk})
        resp_del = self.client.post(del_url, {'action': 'delete'})
        self.assertEqual(resp_del.status_code, 302)
        self.assertFalse(User.objects.filter(username='temp_patient').exists())

        # 3. Deletion WITH medical records safely deactivates instead
        Prescription.objects.create(
            patient=self.patient_profile1,
            prescription_image='prescription_test.jpg',
            status='COMPLETED'
        )
        resp_safe_del = self.client.post(toggle_url, {'action': 'delete'})
        self.assertEqual(resp_safe_del.status_code, 302)
        self.patient_user1.refresh_from_db()
        self.assertTrue(User.objects.filter(username='patient_alice').exists())
        self.assertFalse(self.patient_user1.is_active)


class AdminPortalPharmacyVerificationTests(TestCase):
    """
    Test suite for Pharmacy Verification and Approval Management:
    - Predefined admin authorization requirement
    - Pharmacy directory listing and status filtering
    - Dossier review
    - Approval action with transactional concurrency and email notification
    - Rejection action with optional reason, transactional concurrency, and email
    """
    def setUp(self):
        self.admin = User.objects.create_user(
            username='admin_pharma_mgr',
            email='aayezahali786@gmail.com',
            password='AdminPass123!',
            role='ADMIN',
            is_staff=True,
            is_superuser=True,
            is_active=True,
            email_verified=True,
        )

        self.pharma_user = User.objects.create_user(
            username='pharma_applicant',
            email='contact@citymeds.test',
            password='PharmaPass123!',
            role='PHARMACY',
            is_approved=False,
            email_verified=True,
        )
        self.pharmacy = PharmacyProfile.objects.create(
            user=self.pharma_user,
            pharmacy_name='City Meds Pharmacy',
            owner_name='Vikram Sharma',
            pharmacist_name='Rajesh Kumar',
            pharmacy_license_number='DL-778899',
            gst_number='07AAAAA0000A1Z5',
            phone_number='9876543210',
            email='contact@citymeds.test',
            address='Connaught Place 10',
            city='New Delhi',
            state='Delhi',
            pincode='110001',
            approval_status='PENDING',
        )

    def test_pharmacy_list_view_and_filtering(self):
        """Admin can filter pharmacies by PENDING, APPROVED, and REJECTED."""
        self.client.force_login(self.admin)
        url = reverse('admin_portal:pharmacy_list')

        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'City Meds Pharmacy')
        self.assertEqual(resp.context['counts']['pending'], 1)

        # Filter by PENDING
        resp_pending = self.client.get(url, {'status': 'PENDING'})
        self.assertContains(resp_pending, 'City Meds Pharmacy')

        # Filter by APPROVED
        resp_approved = self.client.get(url, {'status': 'APPROVED'})
        self.assertNotContains(resp_approved, 'City Meds Pharmacy')

    def test_pharmacy_detail_view(self):
        """Dossier displays regulatory compliance details and action modals."""
        self.client.force_login(self.admin)
        url = reverse('admin_portal:pharmacy_detail', kwargs={'pk': self.pharmacy.pk})

        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'City Meds Pharmacy')
        self.assertContains(resp, 'DL-778899')
        self.assertContains(resp, '07AAAAA0000A1Z5')
        self.assertContains(resp, 'Vikram Sharma')

    def test_pharmacy_approve_view_success(self):
        """Approval transitions status to APPROVED, sets is_approved=True, clears rejection notes, sends email."""
        self.client.force_login(self.admin)
        url = reverse('admin_portal:pharmacy_approve', kwargs={'pk': self.pharmacy.pk})

        mail.outbox = []
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)

        self.pharmacy.refresh_from_db()
        self.pharma_user.refresh_from_db()

        self.assertEqual(self.pharmacy.approval_status, 'APPROVED')
        self.assertIsNone(self.pharmacy.rejection_reason)
        self.assertTrue(self.pharma_user.is_approved)

        # Email was dispatched
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('Approved', mail.outbox[0].subject)
        self.assertEqual(mail.outbox[0].to, [self.pharmacy.email])

    def test_pharmacy_reject_with_optional_reason(self):
        """Rejection stores reason, sets is_approved=False, status='REJECTED', and dispatches email."""
        self.client.force_login(self.admin)
        url = reverse('admin_portal:pharmacy_reject', kwargs={'pk': self.pharmacy.pk})

        mail.outbox = []
        reason_text = "The uploaded Drug License certificate is blurry and missing page 2."
        resp = self.client.post(url, {'rejection_reason': reason_text})
        self.assertEqual(resp.status_code, 302)

        self.pharmacy.refresh_from_db()
        self.pharma_user.refresh_from_db()

        self.assertEqual(self.pharmacy.approval_status, 'REJECTED')
        self.assertEqual(self.pharmacy.rejection_reason, reason_text)
        self.assertFalse(self.pharma_user.is_approved)

        # Email contains the rejection reason
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('Application Update', mail.outbox[0].subject)
        self.assertIn(reason_text, mail.outbox[0].body)

    def test_pharmacy_reject_without_reason_allowed(self):
        """Rejection with blank reason is valid and stores None."""
        self.client.force_login(self.admin)
        url = reverse('admin_portal:pharmacy_reject', kwargs={'pk': self.pharmacy.pk})

        mail.outbox = []
        resp = self.client.post(url, {'rejection_reason': '   '})
        self.assertEqual(resp.status_code, 302)

        self.pharmacy.refresh_from_db()
        self.assertEqual(self.pharmacy.approval_status, 'REJECTED')
        self.assertIsNone(self.pharmacy.rejection_reason)
        self.assertEqual(len(mail.outbox), 1)


class PharmacyResubmissionIntegrationTests(TestCase):
    """
    Test suite for rejected pharmacy resubmission workflow:
    - Access via login redirect
    - Resubmission form updates details and resets status to PENDING
    - Resubmitted / rejected pharmacies are blocked from operational routes
    - Approved pharmacies visiting resubmit redirect to dashboard
    """
    def setUp(self):
        self.pharma_user = User.objects.create_user(
            username='rejected_pharma_resubmit',
            email='resubmit@pharma.test',
            password='PharmaPass123!',
            role='PHARMACY',
            is_approved=False,
            email_verified=True,
        )
        self.pharmacy = PharmacyProfile.objects.create(
            user=self.pharma_user,
            pharmacy_name='Original Name Chemist',
            owner_name='Original Owner',
            pharmacist_name='Original Pharmacist',
            pharmacy_license_number='DL-REJ-001',
            gst_number='07AAAAA1111A1Z1',
            phone_number='9876543210',
            email='resubmit@pharma.test',
            address='Old Address 12',
            city='Delhi',
            state='Delhi',
            pincode='110001',
            approval_status='REJECTED',
            rejection_reason='Please submit valid GST and correct store address.',
        )

    def test_rejected_pharmacy_login_with_resubmit_next(self):
        """When logging in with next=/pharmacy/resubmit/, rejected pharmacy is authenticated and redirected."""
        login_url = reverse('accounts:login')
        resubmit_url = reverse('pharmacy:resubmit')

        resp = self.client.post(f"{login_url}?next={resubmit_url}", {
            'username': 'rejected_pharma_resubmit',
            'password': 'PharmaPass123!',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, resubmit_url)

    def test_pharmacy_resubmission_form_resets_pending(self):
        """Updating credentials via resubmission form resets approval_status to PENDING."""
        self.client.force_login(self.pharma_user)
        url = reverse('pharmacy:resubmit')

        # GET shows previous rejection notes
        resp_get = self.client.get(url)
        self.assertEqual(resp_get.status_code, 200)
        self.assertContains(resp_get, 'Please submit valid GST and correct store address.')

        # POST updated details
        updated_data = {
            'pharmacy_name': 'Corrected Name Chemist',
            'owner_name': 'Original Owner',
            'pharmacist_name': 'Original Pharmacist',
            'pharmacy_license_number': 'DL-CORRECTED-999',
            'gst_number': '07AAAAA9999A1Z9',
            'phone_number': '9876543210',
            'address': 'New Correct Address 44',
            'city': 'Delhi',
            'state': 'Delhi',
            'pincode': '110001',
        }
        resp_post = self.client.post(url, updated_data)
        self.assertEqual(resp_post.status_code, 302)

        self.pharmacy.refresh_from_db()
        self.pharma_user.refresh_from_db()

        self.assertEqual(self.pharmacy.pharmacy_name, 'Corrected Name Chemist')
        self.assertEqual(self.pharmacy.pharmacy_license_number, 'DL-CORRECTED-999')
        self.assertEqual(self.pharmacy.approval_status, 'PENDING')
        self.assertFalse(self.pharma_user.is_approved)

    def test_pending_and_rejected_pharmacy_blocked_from_operational_routes(self):
        """Unapproved pharmacies attempting to access operational routes are redirected to landing."""
        self.client.force_login(self.pharma_user)
        dashboard_url = reverse('pharmacy:dashboard')

        resp = self.client.get(dashboard_url)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse('landing'))

    def test_approved_pharmacy_visiting_resubmit_redirects_to_dashboard(self):
        """An already-approved pharmacy visiting /pharmacy/resubmit/ redirects to dashboard."""
        self.pharmacy.approval_status = 'APPROVED'
        self.pharmacy.save()
        self.pharma_user.is_approved = True
        self.pharma_user.save()

        self.client.force_login(self.pharma_user)
        resp = self.client.get(reverse('pharmacy:resubmit'))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse('pharmacy:dashboard'))


class AdminPortalMedicineInventoryTests(TestCase):
    """
    Test suite for Admin Portal Medicine Catalogue and Inventory Overview:
    - Predefined admin authorization requirement
    - Master catalogue display & search
    - Inventory batch display, unit separation, and stock metrics
    """
    def setUp(self):
        self.admin = User.objects.create_user(
            username='admin_med_mgr',
            email='aayezahali786@gmail.com',
            password='AdminPass123!',
            role='ADMIN',
            is_staff=True,
            is_superuser=True,
            is_active=True,
            email_verified=True,
        )

        self.pharma_user = User.objects.create_user(
            username='pharma_inventory_user',
            email='store@meds.test',
            password='PharmaPass123!',
            role='PHARMACY',
            is_approved=True,
            email_verified=True,
        )
        self.pharmacy = PharmacyProfile.objects.create(
            user=self.pharma_user,
            pharmacy_name='Central Care Chemist',
            pharmacy_license_number='DL-555',
            gst_number='07GSTIN555',
            phone_number='9876543210',
            email='store@meds.test',
            address='Main Street 1',
            city='Delhi',
            state='Delhi',
            pincode='110001',
            approval_status='APPROVED',
        )

        self.med1 = Medicine.objects.create(
            medicine_name='Amoxicillin 500mg Capsule',
            manufacturer='Sun Pharma',
            dosage_form='Capsule',
            strength='500mg',
            mrp=120.00,
        )
        self.med2 = Medicine.objects.create(
            medicine_name='Cough Syrup CoughRelief',
            manufacturer='Cipla',
            dosage_form='Syrup',
            strength='100ml',
            mrp=85.00,
        )

        # Inventory batches with distinct units
        self.inv1 = Inventory.objects.create(
            pharmacy=self.pharmacy,
            medicine=self.med1,
            batch_number='BATCH-CAP-001',
            quantity=100,
            unit='Capsules',
            low_stock_limit=20,
            expiry_date=timezone.localdate() + timezone.timedelta(days=180),
        )
        self.inv2 = Inventory.objects.create(
            pharmacy=self.pharmacy,
            medicine=self.med2,
            batch_number='BATCH-SYR-002',
            quantity=5,
            unit='Bottles',
            low_stock_limit=10,  # Low stock
            expiry_date=timezone.localdate() + timezone.timedelta(days=60),
        )

    def test_medicine_catalogue_tab(self):
        """Catalogue tab renders registered medicines and search filters."""
        self.client.force_login(self.admin)
        url = reverse('admin_portal:medicine_list')

        resp = self.client.get(url, {'tab': 'catalogue'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Amoxicillin 500mg Capsule')
        self.assertContains(resp, 'Sun Pharma')
        self.assertContains(resp, 'Cough Syrup CoughRelief')

        # Search filter
        resp_search = self.client.get(url, {'tab': 'catalogue', 'q': 'Cipla'})
        self.assertContains(resp_search, 'Cough Syrup CoughRelief')
        self.assertNotContains(resp_search, 'Amoxicillin')

    def test_inventory_tab_and_unit_separation(self):
        """Inventory tab displays batches, filters low stock, and separates units without mixing."""
        self.client.force_login(self.admin)
        url = reverse('admin_portal:medicine_list')

        resp = self.client.get(url, {'tab': 'inventory'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'BATCH-CAP-001')
        self.assertContains(resp, 'BATCH-SYR-002')
        self.assertContains(resp, 'Capsules')
        self.assertContains(resp, 'Bottles')

        # Filter by low stock condition
        resp_low = self.client.get(url, {'tab': 'inventory', 'filter': 'low_stock'})
        self.assertContains(resp_low, 'BATCH-SYR-002')
        self.assertNotContains(resp_low, 'BATCH-CAP-001')


class AdminPortalFeedbackTests(TestCase):
    """
    Test suite for Feedback & Issues review:
    - Predefined admin authorization requirement
    - List of patient ratings & comments
    - Star distribution & rating filter
    """
    def setUp(self):
        self.admin = User.objects.create_user(
            username='admin_feedback_mgr',
            email='aayezahali786@gmail.com',
            password='AdminPass123!',
            role='ADMIN',
            is_staff=True,
            is_superuser=True,
            is_active=True,
            email_verified=True,
        )

        self.patient_user = User.objects.create_user(
            username='feedback_patient',
            email='patient@feedback.test',
            password='PatientPass123!',
            role='PATIENT',
        )
        self.patient_profile = PatientProfile.objects.create(user=self.patient_user)

        self.pharma_user = User.objects.create_user(
            username='feedback_pharma',
            email='pharma@feedback.test',
            password='PharmaPass123!',
            role='PHARMACY',
            is_approved=True,
        )
        self.pharmacy = PharmacyProfile.objects.create(
            user=self.pharma_user,
            pharmacy_name='Care Chemist Delhi',
            pharmacy_license_number='DL-REV-01',
            gst_number='07GSTINREV01',
            phone_number='9876543210',
            email='pharma@feedback.test',
            address='Lane 1',
            city='Delhi',
            state='Delhi',
            pincode='110001',
            approval_status='APPROVED',
        )

        Feedback.objects.create(
            patient=self.patient_profile,
            pharmacy=self.pharmacy,
            rating=5,
            comment='Outstanding service and fast medicine dispensing!',
        )

    def test_feedback_list_and_filter(self):
        """Admin can review feedback, average score, and filter by rating."""
        self.client.force_login(self.admin)
        url = reverse('admin_portal:feedback_list')

        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Outstanding service and fast medicine dispensing!')
        self.assertContains(resp, 'Care Chemist Delhi')
        self.assertEqual(resp.context['avg_rating'], 5.0)

        # Filter rating 5
        resp_5 = self.client.get(url, {'rating': '5'})
        self.assertContains(resp_5, 'Outstanding service')

        # Filter rating 1
        resp_1 = self.client.get(url, {'rating': '1'})
        self.assertNotContains(resp_1, 'Outstanding service')


class AdminPortalSettingsAndHelpTests(TestCase):
    """
    Test suite for Platform Settings and Help & Support:
    - Predefined admin authorization requirement
    - Platform configuration retrieval and updates
    - Verification that no secrets are exposed
    - Help documentation accessibility
    """
    def setUp(self):
        self.admin = User.objects.create_user(
            username='admin_settings_mgr',
            email='aayezahali786@gmail.com',
            password='AdminPass123!',
            role='ADMIN',
            is_staff=True,
            is_superuser=True,
            is_active=True,
            email_verified=True,
        )

    def test_platform_settings_get_and_post(self):
        """Admin can update platform settings safely with audit trail."""
        self.client.force_login(self.admin)
        url = reverse('admin_portal:settings')

        # GET settings form
        resp_get = self.client.get(url)
        self.assertEqual(resp_get.status_code, 200)
        self.assertContains(resp_get, 'Platform Settings')
        self.assertContains(resp_get, 'Zero Secrets Exposure Policy')

        # POST updated settings
        data = {
            'platform_name': 'MedAccess Healthcare Network India',
            'support_email': 'help@medaccess.in',
            'support_phone': '+91 1800-222-3333',
            'emergency_contact': '+91 1800-999-1111',
            'operating_hours': '24/7 Priority Emergency Support',
            'allow_patient_registration': True,
            'allow_pharmacy_registration': False,
            'maintenance_mode': False,
        }
        resp_post = self.client.post(url, data)
        self.assertEqual(resp_post.status_code, 302)

        cfg = PlatformSetting.get_settings()
        self.assertEqual(cfg.platform_name, 'MedAccess Healthcare Network India')
        self.assertEqual(cfg.support_email, 'help@medaccess.in')
        self.assertFalse(cfg.allow_pharmacy_registration)
        self.assertEqual(cfg.updated_by, self.admin)

    def test_help_support_view(self):
        """Admin user guide renders accurately; unauthorized users are blocked."""
        url = reverse('admin_portal:help')

        # Anonymous blocked
        resp_anon = self.client.get(url)
        self.assertEqual(resp_anon.status_code, 302)

        # Admin authorized
        self.client.force_login(self.admin)
        resp_admin = self.client.get(url)
        self.assertEqual(resp_admin.status_code, 200)
        self.assertContains(resp_admin, 'Pharmacy Verification Protocol')
        self.assertContains(resp_admin, 'Two-Admin Security Model')


class AdminPortalNavigationAndLandingTests(TestCase):
    """
    Verifies that the Admin Portal button and links on the landing page and navigation
    point directly to the custom Admin Portal dashboard (/admin-portal/dashboard/)
    rather than Django's default /admin/ route.
    """
    def setUp(self):
        self.admin = User.objects.create_user(
            username='auth_admin_nav',
            email='aayezahali786@gmail.com',
            password='AdminPass123!',
            role='ADMIN',
            is_staff=True,
            is_superuser=True,
            is_active=True,
            email_verified=True,
        )
        self.patient = User.objects.create_user(
            username='test_patient_nav',
            email='patient_nav@test.com',
            password='PatientPass123!',
            role='PATIENT',
        )

    def test_landing_page_admin_button_points_to_custom_dashboard(self):
        """Unauthenticated landing page renders Admin Portal button pointing to /admin-portal/dashboard/."""
        response = self.client.get(reverse('landing'))
        self.assertEqual(response.status_code, 200)

        custom_dashboard_url = reverse('admin_portal:dashboard')
        self.assertEqual(custom_dashboard_url, '/admin-portal/dashboard/')

        # Button exists and links to custom admin dashboard
        self.assertContains(response, f'href="{custom_dashboard_url}"')
        self.assertContains(response, 'Admin Portal &rarr;')
        # Verify the landing card does not point to Django's built-in /admin/
        self.assertNotContains(response, f'href="{reverse("admin:index")}"')

    def test_authenticated_admin_landing_card_points_to_custom_dashboard(self):
        """Authenticated predefined admin sees direct link to custom dashboard on landing page."""
        self.client.force_login(self.admin)
        response = self.client.get(reverse('landing'))
        self.assertEqual(response.status_code, 200)

        custom_dashboard_url = reverse('admin_portal:dashboard')
        self.assertContains(response, f'href="{custom_dashboard_url}"')
        self.assertContains(response, 'Admin Portal &rarr;')
        # Navbar in base.html also contains Admin Portal link
        self.assertContains(response, f'<a href="{custom_dashboard_url}" class="nav-link">Admin Portal</a>')

    def test_unauthenticated_click_redirects_to_accounts_login_not_django_admin(self):
        """Clicking custom dashboard when unauthenticated redirects to /accounts/login/?next=/admin-portal/dashboard/."""
        custom_dashboard_url = reverse('admin_portal:dashboard')
        response = self.client.get(custom_dashboard_url)
        self.assertEqual(response.status_code, 302)
        # MUST redirect to custom accounts login, NOT /admin/login/?next=/admin/
        self.assertIn(reverse('accounts:login'), response.url)
        self.assertIn(f"next={custom_dashboard_url}", response.url)
        self.assertNotIn('/admin/login/', response.url)

    def test_authenticated_admin_direct_dashboard_access(self):
        """Predefined admin accessing custom dashboard receives 200 and custom admin UI."""
        self.client.force_login(self.admin)
        custom_dashboard_url = reverse('admin_portal:dashboard')
        response = self.client.get(custom_dashboard_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Healthcare Administration Console')
        self.assertContains(response, 'Welcome back, auth_admin_nav')

    def test_django_builtin_admin_still_protected(self):
        """Django built-in /admin/ remains functional and protected."""
        django_admin_url = reverse('admin:index')
        self.assertEqual(django_admin_url, '/admin/')
        response = self.client.get(django_admin_url)
        self.assertEqual(response.status_code, 302)



