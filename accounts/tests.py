from django.test import TestCase, Client, RequestFactory
from django.urls import reverse
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.middleware import SessionMiddleware
from django.contrib.messages.middleware import MessageMiddleware
from django.http import HttpResponse
from django.core import mail
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes
from django.contrib.auth.tokens import default_token_generator

from accounts.models import User
from accounts.decorators import role_required
from patient.models import PatientProfile
from pharmacy.models import PharmacyProfile


# Dummy protected views for decorator isolation testing
@role_required('PHARMACY')
def dummy_pharmacy_view(request):
    return HttpResponse('Pharmacy Portal Content')


@role_required('ADMIN')
def dummy_admin_view(request):
    return HttpResponse('Admin Portal Content')


class AuthenticationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.factory = RequestFactory()

    def _setup_request(self, user, path='/dummy/'):
        request = self.factory.get(path)
        SessionMiddleware(lambda req: None).process_request(request)
        request.session.save()
        MessageMiddleware(lambda req: None).process_request(request)
        request.user = user
        return request

    # --- 1. Registration Tests ---
    def test_patient_registration_success(self):
        url = reverse('accounts:register_patient')
        data = {
            'first_name': 'Aarav',
            'last_name': 'Sharma',
            'username': 'aarav_patient',
            'email': 'aarav@example.com',
            'phone_number': '9876543210',
            'password': 'SecurePatientPass123!',
            'confirm_password': 'SecurePatientPass123!',
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'accounts/verify_email_status.html')
        self.assertEqual(response.context['status'], 'pending')

        user = User.objects.get(username='aarav_patient')
        self.assertEqual(user.role, 'PATIENT')
        self.assertEqual(user.first_name, 'Aarav')
        self.assertTrue(user.check_password('SecurePatientPass123!'))
        self.assertTrue(PatientProfile.objects.filter(user=user).exists())

    def test_patient_registration_duplicate_username_fails(self):
        User.objects.create_user(username='existing_user', email='first@example.com', password='Password123!')
        url = reverse('accounts:register_patient')
        data = {
            'first_name': 'John',
            'last_name': 'Doe',
            'username': 'existing_user',
            'email': 'second@example.com',
            'password': 'Password123!',
            'confirm_password': 'Password123!',
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response.context['form'], 'username', 'A user with this username already exists.')

    def test_patient_registration_duplicate_email_fails(self):
        User.objects.create_user(username='user_one', email='duplicate@example.com', password='Password123!')
        url = reverse('accounts:register_patient')
        data = {
            'first_name': 'Jane',
            'last_name': 'Doe',
            'username': 'user_two',
            'email': 'duplicate@example.com',
            'password': 'Password123!',
            'confirm_password': 'Password123!',
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response.context['form'], 'email', 'A user with this email address already exists.')

    def test_registration_password_mismatch_fails(self):
        url = reverse('accounts:register_patient')
        data = {
            'first_name': 'Aarav',
            'last_name': 'Sharma',
            'username': 'mismatch_user',
            'email': 'mismatch@example.com',
            'password': 'Password123!',
            'confirm_password': 'DifferentPassword456!',
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response.context['form'], 'confirm_password', 'Passwords do not match.')

    def test_registration_weak_password_fails(self):
        url = reverse('accounts:register_patient')
        data = {
            'first_name': 'Aarav',
            'last_name': 'Sharma',
            'username': 'weak_user',
            'email': 'weak@example.com',
            'password': '123',
            'confirm_password': '123',
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].errors)

    def test_pharmacy_registration_creates_pending_profile(self):
        url = reverse('accounts:register_pharmacy')
        data = {
            'username': 'city_meds',
            'email': 'info@citymeds.com',
            'password': 'SecurePharmacyPass123!',
            'confirm_password': 'SecurePharmacyPass123!',
            'pharmacy_name': 'City Meds Pharmacy',
            'owner_name': 'Rajesh Gupta',
            'pharmacist_name': 'Suresh Patel',
            'pharmacy_license_number': 'DL-MH-2026-99',
            'gst_number': '27ABCDE1234F1Z5',
            'phone_number': '022-25698741',
            'address': 'Shop 4, Market Road, Andheri West',
            'city': 'Mumbai',
            'state': 'Maharashtra',
            'pincode': '400058',
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'accounts/verify_email_status.html')
        self.assertEqual(response.context['status'], 'pending')

        user = User.objects.get(username='city_meds')
        self.assertEqual(user.role, 'PHARMACY')
        self.assertFalse(user.is_approved)

        profile = PharmacyProfile.objects.get(user=user)
        self.assertEqual(profile.pharmacy_name, 'City Meds Pharmacy')
        self.assertEqual(profile.approval_status, 'PENDING')
        self.assertEqual(profile.pharmacy_license_number, 'DL-MH-2026-99')

    # --- 2. Login Tests ---
    def test_patient_login_success(self):
        user = User.objects.create_user(
            username='patient_one', password='TestPassword123!', role='PATIENT', email_verified=True
        )
        PatientProfile.objects.create(user=user)

        url = reverse('accounts:login')
        response = self.client.post(url, {'username': 'patient_one', 'password': 'TestPassword123!'})
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('landing'))

    def test_pending_pharmacy_cannot_login(self):
        user = User.objects.create_user(
            username='pending_pharmacy', password='TestPassword123!', role='PHARMACY', email_verified=True
        )
        PharmacyProfile.objects.create(
            user=user,
            pharmacy_name='Pending Chemist',
            owner_name='Owner',
            pharmacist_name='Pharmacist',
            pharmacy_license_number='DL-01',
            gst_number='GST01',
            phone_number='1234567890',
            email='pending@chemist.com',
            address='Street 1',
            city='Delhi',
            state='Delhi',
            pincode='110001',
            approval_status='PENDING'
        )

        url = reverse('accounts:login')
        response = self.client.post(url, {'username': 'pending_pharmacy', 'password': 'TestPassword123!'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'pending admin approval')

    def test_rejected_pharmacy_cannot_login(self):
        user = User.objects.create_user(
            username='rejected_pharmacy', password='TestPassword123!', role='PHARMACY', email_verified=True
        )
        PharmacyProfile.objects.create(
            user=user,
            pharmacy_name='Rejected Chemist',
            owner_name='Owner',
            pharmacist_name='Pharmacist',
            pharmacy_license_number='DL-03',
            gst_number='GST03',
            phone_number='1234567890',
            email='rejected@chemist.com',
            address='Street 3',
            city='Delhi',
            state='Delhi',
            pincode='110001',
            approval_status='REJECTED'
        )

        url = reverse('accounts:login')
        response = self.client.post(url, {'username': 'rejected_pharmacy', 'password': 'TestPassword123!'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'rejected')

    def test_approved_pharmacy_login_success(self):
        user = User.objects.create_user(
            username='approved_pharmacy', password='TestPassword123!', role='PHARMACY', email_verified=True
        )
        PharmacyProfile.objects.create(
            user=user,
            pharmacy_name='Approved Chemist',
            owner_name='Owner',
            pharmacist_name='Pharmacist',
            pharmacy_license_number='DL-02',
            gst_number='GST02',
            phone_number='1234567890',
            email='approved@chemist.com',
            address='Street 2',
            city='Delhi',
            state='Delhi',
            pincode='110001',
            approval_status='APPROVED'
        )

        url = reverse('accounts:login')
        response = self.client.post(url, {'username': 'approved_pharmacy', 'password': 'TestPassword123!'})
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('landing'))

    # --- 3. Logout Tests ---
    def test_logout_post_success(self):
        user = User.objects.create_user(username='user_logout', password='TestPassword123!')
        self.client.login(username='user_logout', password='TestPassword123!')

        logout_url = reverse('accounts:logout')
        response = self.client.post(logout_url)
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('landing'))

    def test_logout_get_returns_405(self):
        logout_url = reverse('accounts:logout')
        response = self.client.get(logout_url)
        self.assertEqual(response.status_code, 405)

    # --- 4. Role Protection Decorator Tests ---
    def test_anonymous_access_to_protected_route_redirects_to_login(self):
        request = self._setup_request(AnonymousUser())
        response = dummy_pharmacy_view(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('accounts:login'))

    def test_patient_denied_access_to_pharmacy_route(self):
        patient_user = User.objects.create_user(username='patient_test', password='Password123!', role='PATIENT')
        request = self._setup_request(patient_user)
        response = dummy_pharmacy_view(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('landing'))

    def test_patient_denied_access_to_admin_route(self):
        patient_user = User.objects.create_user(username='patient_test_admin', password='Password123!', role='PATIENT')
        request = self._setup_request(patient_user)
        response = dummy_admin_view(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('landing'))

    def test_pending_pharmacy_denied_access_to_pharmacy_route(self):
        pharmacy_user = User.objects.create_user(username='pending_user', password='Password123!', role='PHARMACY')
        PharmacyProfile.objects.create(
            user=pharmacy_user,
            pharmacy_name='Pending Pharmacy',
            owner_name='Owner',
            pharmacist_name='Pharmacist',
            pharmacy_license_number='DL-P',
            gst_number='GST-P',
            phone_number='9876543210',
            email='p@p.com',
            address='Addr',
            city='City',
            state='State',
            pincode='000000',
            approval_status='PENDING'
        )
        request = self._setup_request(pharmacy_user)
        response = dummy_pharmacy_view(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('landing'))

    def test_rejected_pharmacy_denied_access_to_pharmacy_route(self):
        pharmacy_user = User.objects.create_user(username='rejected_user', password='Password123!', role='PHARMACY')
        PharmacyProfile.objects.create(
            user=pharmacy_user,
            pharmacy_name='Rejected Pharmacy',
            owner_name='Owner',
            pharmacist_name='Pharmacist',
            pharmacy_license_number='DL-R',
            gst_number='GST-R',
            phone_number='9876543210',
            email='r@r.com',
            address='Addr',
            city='City',
            state='State',
            pincode='000000',
            approval_status='REJECTED'
        )
        request = self._setup_request(pharmacy_user)
        response = dummy_pharmacy_view(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('landing'))

    def test_approved_pharmacy_granted_access_to_pharmacy_route(self):
        pharmacy_user = User.objects.create_user(username='approved_user', password='Password123!', role='PHARMACY')
        PharmacyProfile.objects.create(
            user=pharmacy_user,
            pharmacy_name='Approved Pharmacy',
            owner_name='Owner',
            pharmacist_name='Pharmacist',
            pharmacy_license_number='DL-A',
            gst_number='GST-A',
            phone_number='9876543210',
            email='a@a.com',
            address='Addr',
            city='City',
            state='State',
            pincode='000000',
            approval_status='APPROVED'
        )
        request = self._setup_request(pharmacy_user)
        response = dummy_pharmacy_view(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), 'Pharmacy Portal Content')

    # --- 5. Custom User Manager (Admin Creation) Test ---
    def test_create_superuser_assigns_admin_role(self):
        admin_user = User.objects.create_superuser(
            username='system_admin',
            email='admin@medaccess.com',
            password='AdminPassword123!'
        )
        self.assertEqual(admin_user.role, 'ADMIN')
        self.assertTrue(admin_user.is_staff)
        self.assertTrue(admin_user.is_superuser)
        self.assertTrue(admin_user.is_active)

    # --- 6. Patient Portal Navigation and Landing Tests ---
    def test_landing_page_for_authenticated_patient_has_dashboard_link(self):
        patient_user = User.objects.create_user(
            username='nav_patient',
            password='NavPassword123!',
            role='PATIENT'
        )
        PatientProfile.objects.create(user=patient_user)
        self.client.login(username='nav_patient', password='NavPassword123!')

        landing_url = reverse('landing')
        dashboard_url = reverse('patient:dashboard')
        response = self.client.get(landing_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'href="{dashboard_url}"')
        self.assertContains(response, 'id="patient-portal-btn"')
        self.assertContains(response, 'Go to Patient Dashboard')

        # Following the link opens patient dashboard with 200 OK
        dash_response = self.client.get(dashboard_url)
        self.assertEqual(dash_response.status_code, 200)

    def test_landing_page_for_unauthenticated_user_shows_login_with_next(self):
        response = self.client.get(reverse('landing'))
        self.assertEqual(response.status_code, 200)
        expected_login_target = f"{reverse('accounts:login')}?next={reverse('patient:dashboard')}"
        self.assertContains(response, expected_login_target)

    def test_login_with_next_parameter_redirects_to_dashboard(self):
        user = User.objects.create_user(
            username='next_patient',
            password='Password123!',
            role='PATIENT',
            email_verified=True,
        )
        PatientProfile.objects.create(user=user)

        login_url = f"{reverse('accounts:login')}?next={reverse('patient:dashboard')}"
        response = self.client.post(login_url, {
            'username': 'next_patient',
            'password': 'Password123!',
            'next': reverse('patient:dashboard')
        })
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('patient:dashboard'))

    def test_pharmacy_user_cannot_access_patient_dashboard(self):
        pharma_user = User.objects.create_user(
            username='pharma_nav',
            password='Password123!',
            role='PHARMACY'
        )
        self.client.login(username='pharma_nav', password='Password123!')

        response = self.client.get(reverse('patient:dashboard'))
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('landing'))


class PasswordResetTests(TestCase):
    """
    Focused test suite for MedAccess Password Reset functionality across Patient and Pharmacy roles.
    Covers link visibility, reset email dispatch, token validation, password constraints,
    login with updated credentials, and security preservation.
    """
    def setUp(self):
        self.client = Client()

        # Patient user
        self.patient_user = User.objects.create_user(
            username='reset_patient_user',
            email='patient_reset@medaccess.test',
            password='InitialPatientPass123!',
            role='PATIENT',
            email_verified=True,
        )
        self.patient_profile = PatientProfile.objects.create(user=self.patient_user)

        # Approved Pharmacy user
        self.pharmacy_user_approved = User.objects.create_user(
            username='reset_pharma_approved',
            email='pharma_reset@medaccess.test',
            password='InitialPharmaPass123!',
            role='PHARMACY',
            email_verified=True,
        )
        self.pharmacy_profile_approved = PharmacyProfile.objects.create(
            user=self.pharmacy_user_approved,
            pharmacy_name='Approved Test Pharmacy',
            approval_status='APPROVED'
        )

        # Pending Pharmacy user
        self.pharmacy_user_pending = User.objects.create_user(
            username='reset_pharma_pending',
            email='pharma_pending@medaccess.test',
            password='PendingPharmaPass123!',
            role='PHARMACY',
            email_verified=True,
        )
        self.pharmacy_profile_pending = PharmacyProfile.objects.create(
            user=self.pharmacy_user_pending,
            pharmacy_name='Pending Test Pharmacy',
            approval_status='PENDING'
        )

        # Rejected Pharmacy user
        self.pharmacy_user_rejected = User.objects.create_user(
            username='reset_pharma_rejected',
            email='pharma_rejected@medaccess.test',
            password='RejectedPharmaPass123!',
            role='PHARMACY',
            email_verified=True,
        )
        self.pharmacy_profile_rejected = PharmacyProfile.objects.create(
            user=self.pharmacy_user_rejected,
            pharmacy_name='Rejected Test Pharmacy',
            approval_status='REJECTED'
        )

    def _confirm_password_reset(self, user, token, new_password1, new_password2=None):
        """Helper to navigate the password reset confirmation dispatch and post new password."""
        if new_password2 is None:
            new_password2 = new_password1
        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        confirm_url = reverse('accounts:password_reset_confirm', kwargs={'uidb64': uidb64, 'token': token})
        get_resp = self.client.get(confirm_url, follow=True)
        set_pw_url = get_resp.redirect_chain[0][0] if get_resp.redirect_chain else confirm_url
        post_resp = self.client.post(set_pw_url, {
            'new_password1': new_password1,
            'new_password2': new_password2,
        })
        return get_resp, post_resp

    # 1. Forgot Password link visible on Patient Login
    def test_forgot_password_link_visible_on_patient_login(self):
        """Forgot Password? link is prominently visible when accessing login for Patient Portal."""
        login_url = f"{reverse('accounts:login')}?next={reverse('patient:dashboard')}"
        response = self.client.get(login_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Forgot Password?')
        self.assertContains(response, reverse('accounts:password_reset'))

    # 2. Forgot Password link visible on Pharmacy Login
    def test_forgot_password_link_visible_on_pharmacy_login(self):
        """Forgot Password? link is prominently visible when accessing login for Pharmacy Portal."""
        login_url = f"{reverse('accounts:login')}?next={reverse('pharmacy:dashboard')}"
        response = self.client.get(login_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Forgot Password?')
        self.assertContains(response, reverse('accounts:password_reset'))

    # 3. Forgot Password page loads successfully
    def test_forgot_password_page_loads_successfully(self):
        """Forgot Password page renders 200 OK with branding, form, and back to login link."""
        reset_url = reverse('accounts:password_reset')
        response = self.client.get(reset_url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'accounts/password_reset_form.html')
        self.assertContains(response, 'Forgot Password?')
        self.assertContains(response, 'Send Reset Link')
        self.assertContains(response, 'Back to Sign In')
        self.assertContains(response, reverse('accounts:login'))

    # 4. Valid registered email triggers reset email
    def test_valid_registered_email_triggers_reset_email(self):
        """Submitting a valid registered email sends a secure password reset email with valid link."""
        mail.outbox = []
        reset_url = reverse('accounts:password_reset')

        # Test Patient Reset
        resp = self.client.post(reset_url, {'email': 'patient_reset@medaccess.test'})
        self.assertEqual(resp.status_code, 302)
        self.assertRedirects(resp, reverse('accounts:password_reset_done'))
        self.assertEqual(len(mail.outbox), 1)

        sent_email = mail.outbox[0]
        self.assertIn('patient_reset@medaccess.test', sent_email.to)
        self.assertIn('Reset your MedAccess password', sent_email.subject)
        self.assertIn('/accounts/password-reset-confirm/', sent_email.body)

        # Test Pharmacy Reset
        resp_pharma = self.client.post(reset_url, {'email': 'pharma_reset@medaccess.test'})
        self.assertEqual(resp_pharma.status_code, 302)
        self.assertRedirects(resp_pharma, reverse('accounts:password_reset_done'))
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn('pharma_reset@medaccess.test', mail.outbox[1].to)

    # 5. Unknown email displays the same generic success message
    def test_unknown_email_displays_same_generic_success_message(self):
        """Submitting an unregistered email shows the generic success message and leaks no user existence info."""
        mail.outbox = []
        reset_url = reverse('accounts:password_reset')

        response = self.client.post(reset_url, {'email': 'completely_unknown_user@example.com'})
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('accounts:password_reset_done'))

        # No email sent
        self.assertEqual(len(mail.outbox), 0)

        # Done page displays exact generic success text
        done_resp = self.client.get(reverse('accounts:password_reset_done'))
        self.assertEqual(done_resp.status_code, 200)
        self.assertContains(done_resp, 'If an account with this email exists, a password reset link has been sent.')

    # 6. Reset link successfully changes the password
    def test_reset_link_successfully_changes_password(self):
        """A valid reset link allows setting a new password and immediately invalidates the token."""
        user = self.patient_user
        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)

        confirm_url = reverse('accounts:password_reset_confirm', kwargs={'uidb64': uidb64, 'token': token})

        # GET confirm page follows the dispatch redirect to set-password
        get_resp = self.client.get(confirm_url, follow=True)
        self.assertEqual(get_resp.status_code, 200)
        self.assertTrue(get_resp.context['validlink'])
        self.assertContains(get_resp, 'Set New Password')

        # POST new password to the set-password url
        new_password = 'BrandNewValidSecurePass999!'
        set_pw_url = get_resp.redirect_chain[0][0]
        post_resp = self.client.post(set_pw_url, {
            'new_password1': new_password,
            'new_password2': new_password,
        })
        self.assertEqual(post_resp.status_code, 302)
        self.assertRedirects(post_resp, reverse('accounts:password_reset_complete'))

        # Verify password changed
        user.refresh_from_db()
        self.assertTrue(user.check_password(new_password))
        self.assertFalse(user.check_password('InitialPatientPass123!'))

        # Verify token is now invalid (single-use constraint)
        self.assertFalse(default_token_generator.check_token(user, token))

        # Complete page renders 200 OK
        comp_resp = self.client.get(reverse('accounts:password_reset_complete'))
        self.assertEqual(comp_resp.status_code, 200)
        self.assertContains(comp_resp, 'Password Reset Complete')
        self.assertContains(comp_resp, reverse('accounts:login'))

    # 7. Password mismatch is rejected
    def test_password_mismatch_rejected(self):
        """Entering non-matching new passwords rejects submission and leaves current password unchanged."""
        user = self.patient_user
        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)

        confirm_url = reverse('accounts:password_reset_confirm', kwargs={'uidb64': uidb64, 'token': token})
        get_resp = self.client.get(confirm_url, follow=True)
        set_pw_url = get_resp.redirect_chain[0][0]

        resp = self.client.post(set_pw_url, {
            'new_password1': 'NewPass123!@#',
            'new_password2': 'DifferentPass456!@#',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn("The two password fields", str(resp.context['form'].errors.get('new_password2', '')))

        # Password remains unchanged
        user.refresh_from_db()
        self.assertTrue(user.check_password('InitialPatientPass123!'))

    # 8. Weak password is rejected by Django validators
    def test_weak_password_rejected_by_validators(self):
        """Weak passwords (e.g. too short or entirely numeric) are rejected by standard validators."""
        user = self.patient_user
        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)

        confirm_url = reverse('accounts:password_reset_confirm', kwargs={'uidb64': uidb64, 'token': token})
        get_resp = self.client.get(confirm_url, follow=True)
        set_pw_url = get_resp.redirect_chain[0][0]

        resp = self.client.post(set_pw_url, {
            'new_password1': '12345',
            'new_password2': '12345',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context['form'].errors)

        # Password remains unchanged
        user.refresh_from_db()
        self.assertTrue(user.check_password('InitialPatientPass123!'))

    # 9. Expired or invalid reset tokens are rejected
    def test_expired_or_invalid_reset_tokens_rejected(self):
        """Tampered or invalid tokens display the invalid link state and reject password changes."""
        user = self.patient_user
        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        invalid_token = 'invalid-tampered-token-123'

        confirm_url = reverse('accounts:password_reset_confirm', kwargs={'uidb64': uidb64, 'token': invalid_token})

        # GET invalid link does not redirect to set-password
        get_resp = self.client.get(confirm_url)
        self.assertEqual(get_resp.status_code, 200)
        self.assertFalse(get_resp.context['validlink'])
        self.assertContains(get_resp, 'Invalid or Expired Link')
        self.assertContains(get_resp, 'Request New Reset Link')

        # POST with invalid link fails safely
        post_resp = self.client.post(confirm_url, {
            'new_password1': 'AttemptedNewPassword123!',
            'new_password2': 'AttemptedNewPassword123!',
        })
        self.assertEqual(post_resp.status_code, 200)
        user.refresh_from_db()
        self.assertTrue(user.check_password('InitialPatientPass123!'))

    # 10. Existing login works with the new password
    def test_existing_login_works_with_new_password(self):
        """Users can log in through the standard login page using their updated password."""
        user = self.patient_user
        token = default_token_generator.make_token(user)
        new_password = 'BrandNewValidPass456!'

        self._confirm_password_reset(user, token, new_password)

        # Login with old password fails
        old_login_resp = self.client.post(reverse('accounts:login'), {
            'username': user.username,
            'password': 'InitialPatientPass123!',
        })
        self.assertEqual(old_login_resp.status_code, 200)
        self.assertContains(old_login_resp, 'Invalid username or password')

        # Login with new password succeeds
        new_login_resp = self.client.post(reverse('accounts:login'), {
            'username': user.username,
            'password': new_password,
        })
        self.assertEqual(new_login_resp.status_code, 302)
        self.assertRedirects(new_login_resp, reverse('landing'))

    # 11. Patient and Pharmacy roles remain unchanged
    def test_patient_and_pharmacy_roles_remain_unchanged(self):
        """Password resets preserve role integrity for both Patient and Pharmacy accounts."""
        # Patient
        p_tok = default_token_generator.make_token(self.patient_user)
        self._confirm_password_reset(self.patient_user, p_tok, 'NewPassPatient123!')
        self.patient_user.refresh_from_db()
        self.assertEqual(self.patient_user.role, 'PATIENT')

        # Pharmacy
        ph_tok = default_token_generator.make_token(self.pharmacy_user_approved)
        self._confirm_password_reset(self.pharmacy_user_approved, ph_tok, 'NewPassPharma123!')
        self.pharmacy_user_approved.refresh_from_db()
        self.assertEqual(self.pharmacy_user_approved.role, 'PHARMACY')

    # 12. Pharmacy approval restrictions remain unchanged
    def test_pharmacy_approval_restrictions_remain_unchanged(self):
        """Resetting password does NOT approve pending or rejected pharmacy accounts or bypass login checks."""
        # Pending Pharmacy
        pend_tok = default_token_generator.make_token(self.pharmacy_user_pending)
        new_pend_pass = 'NewPendingPass789!'
        self._confirm_password_reset(self.pharmacy_user_pending, pend_tok, new_pend_pass)

        self.pharmacy_user_pending.refresh_from_db()
        self.pharmacy_profile_pending.refresh_from_db()
        self.assertEqual(self.pharmacy_profile_pending.approval_status, 'PENDING')

        # Attempting login with new password still blocks with pending warning
        login_pend = self.client.post(reverse('accounts:login'), {
            'username': self.pharmacy_user_pending.username,
            'password': new_pend_pass,
        })
        self.assertEqual(login_pend.status_code, 200)
        self.assertContains(login_pend, 'Your pharmacy registration is currently pending admin approval')

        # Rejected Pharmacy
        rej_tok = default_token_generator.make_token(self.pharmacy_user_rejected)
        new_rej_pass = 'NewRejectedPass789!'
        self._confirm_password_reset(self.pharmacy_user_rejected, rej_tok, new_rej_pass)

        self.pharmacy_user_rejected.refresh_from_db()
        self.pharmacy_profile_rejected.refresh_from_db()
        self.assertEqual(self.pharmacy_profile_rejected.approval_status, 'REJECTED')

        # Attempting login with new password still blocks with rejected error
        login_rej = self.client.post(reverse('accounts:login'), {
            'username': self.pharmacy_user_rejected.username,
            'password': new_rej_pass,
        })
        self.assertEqual(login_rej.status_code, 200)
        self.assertContains(login_rej, 'Your pharmacy registration application was reviewed and rejected')


class EmailVerificationTests(TestCase):
    """
    Automated test suite for Gmail SMTP-ready email delivery and verification
    for Patient and Pharmacy registration.
    Covers registration email dispatch, token generation and validation, single-use,
    24-hour expiration, login gatekeeping, admin bypass, and rate-limited resend.
    """
    def setUp(self):
        self.client = Client()
        mail.outbox = []

        # Verified Patient
        self.patient_verified = User.objects.create_user(
            username='verified_patient',
            email='patient_verified@medaccess.test',
            password='ValidPassword123!',
            role='PATIENT',
            email_verified=True,
        )
        PatientProfile.objects.create(user=self.patient_verified)

        # Unverified Patient
        self.patient_unverified = User.objects.create_user(
            username='unverified_patient',
            email='patient_unverified@medaccess.test',
            password='ValidPassword123!',
            role='PATIENT',
            email_verified=False,
        )
        PatientProfile.objects.create(user=self.patient_unverified)

        # Unverified Pharmacy
        self.pharmacy_unverified = User.objects.create_user(
            username='unverified_pharma',
            email='pharma_unverified@medaccess.test',
            password='ValidPassword123!',
            role='PHARMACY',
            email_verified=False,
        )
        PharmacyProfile.objects.create(
            user=self.pharmacy_unverified,
            pharmacy_name='Unverified Pharmacy',
            approval_status='PENDING',
        )

        # Verified & Approved Pharmacy
        self.pharmacy_approved = User.objects.create_user(
            username='approved_pharma',
            email='pharma_approved@medaccess.test',
            password='ValidPassword123!',
            role='PHARMACY',
            email_verified=True,
        )
        PharmacyProfile.objects.create(
            user=self.pharmacy_approved,
            pharmacy_name='Approved Pharmacy',
            approval_status='APPROVED',
        )

        # Admin user
        self.admin_user = User.objects.create_user(
            username='admin_staff',
            email='admin@medaccess.test',
            password='ValidPassword123!',
            role='ADMIN',
            email_verified=False,  # Unverified admin must still be allowed
        )

    # 1. Patient registration sends a verification email and stays unverified
    def test_patient_registration_sends_verification_email(self):
        url = reverse('accounts:register_patient')
        data = {
            'first_name': 'Rohan',
            'last_name': 'Verma',
            'username': 'rohan_patient',
            'email': 'rohan.verma@medaccess.test',
            'phone_number': '9811122233',
            'password': 'SecurePatient123!',
            'confirm_password': 'SecurePatient123!',
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'accounts/verify_email_status.html')
        self.assertEqual(response.context['status'], 'pending')

        # User in database is unverified
        new_user = User.objects.get(username='rohan_patient')
        self.assertFalse(new_user.email_verified)
        self.assertEqual(new_user.role, 'PATIENT')

        # Verification email sent
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertIn('Verify your MedAccess account email', email.subject)
        self.assertIn('rohan.verma@medaccess.test', email.to)
        self.assertIn('/accounts/verify-email/', email.body)

    # 2. Pharmacy registration sends a verification email and stays unverified
    def test_pharmacy_registration_sends_verification_email(self):
        url = reverse('accounts:register_pharmacy')
        data = {
            'username': 'metro_chemist',
            'email': 'contact@metrochemist.test',
            'password': 'SecurePharmaPass123!',
            'confirm_password': 'SecurePharmaPass123!',
            'pharmacy_name': 'Metro Chemist Store',
            'owner_name': 'Kavita Rao',
            'pharmacist_name': 'Deepak Joshi',
            'pharmacy_license_number': 'DL-MH-2026-888',
            'gst_number': '27ABCDE9999F1Z9',
            'phone_number': '022-28994455',
            'address': 'Plot 12, Station Road',
            'city': 'Mumbai',
            'state': 'Maharashtra',
            'pincode': '400001',
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'accounts/verify_email_status.html')
        self.assertEqual(response.context['status'], 'pending')

        # User in database is unverified and pending
        new_pharma = User.objects.get(username='metro_chemist')
        self.assertFalse(new_pharma.email_verified)
        self.assertEqual(new_pharma.role, 'PHARMACY')
        self.assertEqual(new_pharma.pharmacy_profile.approval_status, 'PENDING')

        # Email sent
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertIn('contact@metrochemist.test', email.to)
        self.assertIn('/accounts/verify-email/', email.body)

    # 3. Valid verification links verify the correct account
    def test_valid_verification_link_verifies_account(self):
        from accounts.utils import generate_verification_token
        token = generate_verification_token(self.patient_unverified)

        verify_url = reverse('accounts:verify_email', kwargs={'token': token})
        response = self.client.get(verify_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Email Verified Successfully')

        self.patient_unverified.refresh_from_db()
        self.assertTrue(self.patient_unverified.email_verified)

    # 4. Verified pharmacy remains in PENDING status
    def test_verified_pharmacy_remains_pending_admin_approval(self):
        from accounts.utils import generate_verification_token
        token = generate_verification_token(self.pharmacy_unverified)

        verify_url = reverse('accounts:verify_email', kwargs={'token': token})
        response = self.client.get(verify_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Pharmacy Notice')

        self.pharmacy_unverified.refresh_from_db()
        self.assertTrue(self.pharmacy_unverified.email_verified)
        self.assertEqual(self.pharmacy_unverified.pharmacy_profile.approval_status, 'PENDING')

    # 5. Already verified account shows friendly status
    def test_already_verified_account_shows_friendly_message(self):
        from accounts.utils import generate_verification_token
        token = generate_verification_token(self.patient_verified)

        verify_url = reverse('accounts:verify_email', kwargs={'token': token})
        response = self.client.get(verify_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Email Already Verified')

    # 6. Expired token is safely rejected
    def test_expired_token_rejected(self):
        from accounts.utils import generate_verification_token, validate_verification_token
        token = generate_verification_token(self.patient_unverified)

        user, status = validate_verification_token(token, max_age=0)
        self.assertIsNone(user)
        self.assertEqual(status, 'expired')

        # GET request to verification view with invalid or expired token
        verify_url = reverse('accounts:verify_email', kwargs={'token': 'expired-token-format'})
        response = self.client.get(verify_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Verification Link')

        self.patient_unverified.refresh_from_db()
        self.assertFalse(self.patient_unverified.email_verified)

    # 7. Invalid or tampered token rejected
    def test_invalid_or_tampered_token_rejected(self):
        verify_url = reverse('accounts:verify_email', kwargs={'token': 'tampered_malformed_token_123'})
        response = self.client.get(verify_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Invalid Verification Link')

        self.patient_unverified.refresh_from_db()
        self.assertFalse(self.patient_unverified.email_verified)

    # 8. Unverified patient cannot log in
    def test_unverified_patient_cannot_login(self):
        url = reverse('accounts:login')
        response = self.client.post(url, {
            'username': self.patient_unverified.username,
            'password': 'ValidPassword123!',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Please verify your email address before logging in')
        self.assertContains(response, reverse('accounts:resend_verification'))

    # 9. Unverified pharmacy cannot log in
    def test_unverified_pharmacy_cannot_login(self):
        url = reverse('accounts:login')
        response = self.client.post(url, {
            'username': self.pharmacy_unverified.username,
            'password': 'ValidPassword123!',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Please verify your pharmacy official email address before logging in')
        self.assertContains(response, reverse('accounts:resend_verification'))

    # 10. Verified but pending pharmacy remains blocked by approval requirement
    def test_verified_pending_pharmacy_cannot_login(self):
        self.pharmacy_unverified.email_verified = True
        self.pharmacy_unverified.save()

        url = reverse('accounts:login')
        response = self.client.post(url, {
            'username': self.pharmacy_unverified.username,
            'password': 'ValidPassword123!',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'pending admin approval')

    # 11. Approved and verified pharmacy can log in successfully
    def test_approved_and_verified_pharmacy_can_login(self):
        url = reverse('accounts:login')
        response = self.client.post(url, {
            'username': self.pharmacy_approved.username,
            'password': 'ValidPassword123!',
        })
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('landing'))

    # 12. Admin login continues working without email verification restriction
    def test_admin_login_works_without_email_verification(self):
        url = reverse('accounts:login')
        response = self.client.post(url, {
            'username': self.admin_user.username,
            'password': 'ValidPassword123!',
        })
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('landing'))

    # 13. Resend verification works for unverified user
    def test_resend_verification_email_works(self):
        url = reverse('accounts:resend_verification')
        response = self.client.post(url, {'email': self.patient_unverified.email})
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('accounts:login'))

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.patient_unverified.email, mail.outbox[0].to)

    # 14. Resend verification request is throttled
    def test_resend_verification_throttling(self):
        url = reverse('accounts:resend_verification')
        # First request
        self.client.post(url, {'email': self.patient_unverified.email})
        self.assertEqual(len(mail.outbox), 1)

        # Immediate second request from the same session
        resp_throttled = self.client.post(url, {'email': self.patient_unverified.email})
        self.assertEqual(resp_throttled.status_code, 200)
        self.assertContains(resp_throttled, 'requested recently')
        # No extra email was sent
        self.assertEqual(len(mail.outbox), 1)

    # 15. Resend verification returns generic message for unknown email
    def test_resend_verification_unknown_email_returns_generic_message(self):
        url = reverse('accounts:resend_verification')
        response = self.client.post(url, {'email': 'completely_nonexistent@example.com'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'If an unverified account with this email exists')
        self.assertEqual(len(mail.outbox), 0)

    # 16. Existing accounts retain access after data migration
    def test_existing_accounts_retain_access_after_migration(self):
        import importlib
        migration_module = importlib.import_module('accounts.migrations.0003_mark_existing_users_verified')
        mark_existing_users_verified = migration_module.mark_existing_users_verified
        from django.apps import apps

        # Set user as unverified to test migration behavior
        User.objects.filter(pk=self.patient_unverified.pk).update(email_verified=False)
        self.patient_unverified.refresh_from_db()
        self.assertFalse(self.patient_unverified.email_verified)

        # Run migration function
        mark_existing_users_verified(apps, None)

        self.patient_unverified.refresh_from_db()
        self.assertTrue(self.patient_unverified.email_verified)

        # Login now succeeds
        url = reverse('accounts:login')
        response = self.client.post(url, {
            'username': self.patient_unverified.username,
            'password': 'ValidPassword123!',
        })
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('landing'))

    # 17. SMTP delivery failure during registration displays pending status gracefully
    def test_patient_registration_smtp_failure_handled_gracefully(self):
        from unittest.mock import patch
        url = reverse('accounts:register_patient')
        data = {
            'first_name': 'Kunal',
            'last_name': 'Mehta',
            'username': 'kunal_patient',
            'email': 'kunal@medaccess.test',
            'phone_number': '9822233344',
            'password': 'SecurePatient123!',
            'confirm_password': 'SecurePatient123!',
        }
        with patch('accounts.views.send_verification_email', return_value=False):
            response = self.client.post(url, data)
            self.assertEqual(response.status_code, 200)
            self.assertTemplateUsed(response, 'accounts/verify_email_status.html')
            self.assertEqual(response.context['status'], 'pending')
            self.assertFalse(response.context['email_sent'])
            self.assertContains(response, 'could not deliver the verification email right now')

        # User is still created in unverified state
        user = User.objects.get(username='kunal_patient')
        self.assertFalse(user.email_verified)

    # 18. SMTP delivery failure during resend verification handled gracefully
    def test_resend_verification_smtp_failure_handled_gracefully(self):
        from unittest.mock import patch
        url = reverse('accounts:resend_verification')
        with patch('accounts.views.send_verification_email', return_value=False):
            response = self.client.post(url, {'email': self.patient_unverified.email}, follow=True)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'could not deliver the email at this moment')

    # 19. Email verification supports token passed as query parameter
    def test_verification_via_query_parameter(self):
        from accounts.utils import generate_verification_token
        token = generate_verification_token(self.patient_unverified)
        verify_url = f"{reverse('accounts:verify_email_query')}?token={token}"
        response = self.client.get(verify_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Email Verified Successfully')

        self.patient_unverified.refresh_from_db()
        self.assertTrue(self.patient_unverified.email_verified)

    # 20. Verification page preserves anonymous navbar for unauthenticated visitors
    def test_verify_email_status_preserves_anonymous_navbar(self):
        from accounts.utils import generate_verification_token
        token = generate_verification_token(self.patient_unverified)
        verify_url = reverse('accounts:verify_email', kwargs={'token': token})
        response = self.client.get(verify_url)
        self.assertEqual(response.status_code, 200)
        # Should contain Login link, not Logout button
        self.assertContains(response, 'Login')
        self.assertNotContains(response, 'Logout')



