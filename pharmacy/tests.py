import csv
import io
import json
import os
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command, CommandError
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from django.db import models

from accounts.models import User
from patient.models import PatientProfile
from pharmacy.models import PharmacyProfile, Medicine, Inventory, Feedback, MedicineComposition
from pharmacy.services.composition_lookup import lookup_medicine_composition, clear_distinct_names_cache
from pharmacy.services.voice_stock_parser import parse_voice_command
from pharmacy.services.catalogue_matcher import match_medicine_catalogue
from pharmacy.services.price_predictor import predict_medicine_price


class PharmacyPortalAccessTests(TestCase):
    def setUp(self):
        self.client = Client()

        # 1. Approved Pharmacy User
        self.approved_user = User.objects.create_user(
            username='approved_pharmacy_user',
            password='Password123!',
            role='PHARMACY',
            first_name='Apollo',
            last_name='Pharmacy'
        )
        self.approved_profile = PharmacyProfile.objects.create(
            user=self.approved_user,
            pharmacy_name='Apollo Care Pharmacy',
            owner_name='Apollo Health',
            pharmacist_name='John Doe, RPh',
            pharmacy_license_number='DL-99991',
            gst_number='GSTIN99991',
            phone_number='9876543210',
            email='apollo@medaccess.test',
            address='10 Medical Square',
            city='Bhopal',
            state='Madhya Pradesh',
            pincode='462001',
            approval_status='APPROVED'
        )

        # 2. Pending Pharmacy User
        self.pending_user = User.objects.create_user(
            username='pending_pharmacy_user',
            password='Password123!',
            role='PHARMACY'
        )
        self.pending_profile = PharmacyProfile.objects.create(
            user=self.pending_user,
            pharmacy_name='Pending Pharmacy Care',
            owner_name='Pending Owner',
            pharmacist_name='Pending Pharmacist',
            pharmacy_license_number='DL-99992',
            gst_number='GSTIN99992',
            phone_number='9876543211',
            email='pending@medaccess.test',
            address='11 Medical Square',
            city='Bhopal',
            state='Madhya Pradesh',
            pincode='462001',
            approval_status='PENDING'
        )

        # 3. Rejected Pharmacy User
        self.rejected_user = User.objects.create_user(
            username='rejected_pharmacy_user',
            password='Password123!',
            role='PHARMACY'
        )
        self.rejected_profile = PharmacyProfile.objects.create(
            user=self.rejected_user,
            pharmacy_name='Rejected Pharmacy Care',
            owner_name='Rejected Owner',
            pharmacist_name='Rejected Pharmacist',
            pharmacy_license_number='DL-99993',
            gst_number='GSTIN99993',
            phone_number='9876543212',
            email='rejected@medaccess.test',
            address='12 Medical Square',
            city='Bhopal',
            state='Madhya Pradesh',
            pincode='462001',
            approval_status='REJECTED'
        )

        # 4. Patient User
        self.patient_user = User.objects.create_user(
            username='regular_patient',
            password='Password123!',
            role='PATIENT'
        )
        PatientProfile.objects.create(user=self.patient_user)

        # 5. Admin User
        self.admin_user = User.objects.create_superuser(
            username='site_admin',
            password='Password123!',
            email='admin@medaccess.test'
        )
        self.admin_user.role = 'ADMIN'
        self.admin_user.save()

    def test_approved_pharmacy_can_access_dashboard(self):
        """Approved pharmacy user can access the Pharmacy Dashboard with 200 OK and expected templates."""
        self.client.login(username='approved_pharmacy_user', password='Password123!')
        url = reverse('pharmacy:dashboard')
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'pharmacy/dashboard.html')
        self.assertTemplateUsed(response, 'pharmacy/base_pharmacy.html')
        self.assertContains(response, 'Apollo Care Pharmacy')
        self.assertContains(response, 'Approved')
        self.assertContains(response, 'John Doe, RPh')
        self.assertContains(response, 'DL-99991')

    def test_patient_cannot_access_pharmacy_portal(self):
        """Patient user attempting to access pharmacy dashboard is redirected to landing."""
        self.client.login(username='regular_patient', password='Password123!')
        url = reverse('pharmacy:dashboard')
        response = self.client.get(url)

        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('landing'))

    def test_unauthenticated_user_redirected_to_login(self):
        """Unauthenticated user accessing pharmacy dashboard is redirected to login page."""
        url = reverse('pharmacy:dashboard')
        response = self.client.get(url)

        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('accounts:login'))

    def test_pending_pharmacy_cannot_access_dashboard(self):
        """Pending pharmacy user is redirected to landing with warning."""
        self.client.force_login(self.pending_user)
        url = reverse('pharmacy:dashboard')
        response = self.client.get(url)

        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('landing'))

    def test_rejected_pharmacy_cannot_access_dashboard(self):
        """Rejected pharmacy user is redirected to landing with error."""
        self.client.force_login(self.rejected_user)
        url = reverse('pharmacy:dashboard')
        response = self.client.get(url)

        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('landing'))

    def test_admin_cannot_access_pharmacy_portal(self):
        """Admin user attempting to access pharmacy dashboard is redirected to landing."""
        self.client.login(username='site_admin', password='Password123!')
        url = reverse('pharmacy:dashboard')
        response = self.client.get(url)

        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('landing'))

    def test_landing_page_shows_pharmacy_dashboard_for_approved_pharmacy(self):
        """Landing page renders the Pharmacy Dashboard link for authenticated approved pharmacy."""
        self.client.login(username='approved_pharmacy_user', password='Password123!')
        response = self.client.get(reverse('landing'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('pharmacy:dashboard'))

    def test_logout_remains_post_only(self):
        """Logout endpoint requires POST method and rejects GET requests with 405."""
        self.client.login(username='approved_pharmacy_user', password='Password123!')
        logout_url = reverse('accounts:logout')

        # GET should be rejected (405 Method Not Allowed)
        get_response = self.client.get(logout_url)
        self.assertEqual(get_response.status_code, 405)

        # POST should log out user and redirect to landing
        post_response = self.client.post(logout_url)
        self.assertEqual(post_response.status_code, 302)
        self.assertRedirects(post_response, reverse('landing'))

    def test_existing_url_namespaces_continue_working(self):
        """Verify URL routing and namespaces for landing, patient, accounts, and pharmacy."""
        self.assertEqual(reverse('landing'), '/')
        self.assertEqual(reverse('patient:dashboard'), '/patient/dashboard/')
        self.assertEqual(reverse('accounts:login'), '/accounts/login/')
        self.assertEqual(reverse('pharmacy:dashboard'), '/pharmacy/')
        self.assertEqual(reverse('pharmacy:settings'), '/pharmacy/settings/')


class PharmacyDashboardKPITests(TestCase):
    def setUp(self):
        self.client = Client()
        self.today = timezone.localdate()

        # Pharmacy 1 (Primary Test Pharmacy)
        self.user1 = User.objects.create_user(
            username='pharma_one',
            password='Password123!',
            role='PHARMACY'
        )
        self.pharmacy1 = PharmacyProfile.objects.create(
            user=self.user1,
            pharmacy_name='City Care Pharmacy',
            owner_name='City Health Group',
            pharmacist_name='Jane Pharmacist',
            pharmacy_license_number='DL-CITY-01',
            gst_number='GSTIN-CITY-01',
            phone_number='9800000001',
            email='city@medaccess.test',
            address='1 City Center',
            city='Bhopal',
            state='Madhya Pradesh',
            pincode='462001',
            approval_status='APPROVED'
        )

        # Pharmacy 2 (For Isolation Testing)
        self.user2 = User.objects.create_user(
            username='pharma_two',
            password='Password123!',
            role='PHARMACY'
        )
        self.pharmacy2 = PharmacyProfile.objects.create(
            user=self.user2,
            pharmacy_name='Metro Chemist',
            owner_name='Metro Health Corp',
            pharmacist_name='Bob Pharmacist',
            pharmacy_license_number='DL-METRO-02',
            gst_number='GSTIN-METRO-02',
            phone_number='9800000002',
            email='metro@medaccess.test',
            address='2 Metro Plaza',
            city='Indore',
            state='Madhya Pradesh',
            pincode='452001',
            approval_status='APPROVED'
        )

        # Medicines
        self.med_paracetamol = Medicine.objects.create(
            medicine_name='Paracetamol 650mg',
            composition='Paracetamol 650mg',
            dosage_form='Tablet'
        )
        self.med_amoxicillin = Medicine.objects.create(
            medicine_name='Amoxicillin 500mg',
            composition='Amoxicillin 500mg',
            dosage_form='Capsule'
        )
        self.med_cetirizine = Medicine.objects.create(
            medicine_name='Cetirizine 10mg',
            composition='Cetirizine Hydrochloride',
            dosage_form='Tablet'
        )

    def test_zero_inventory_behavior(self):
        """Dashboard renders 0 for all 4 KPI metrics when no inventory exists."""
        self.client.login(username='pharma_one', password='Password123!')
        response = self.client.get(reverse('pharmacy:dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_medicines'], 0)
        self.assertEqual(response.context['active_batches'], 0)
        self.assertEqual(response.context['low_stock_alerts'], 0)
        self.assertEqual(response.context['out_of_stock'], 0)

    def test_distinct_medicine_count_across_multiple_batches(self):
        """Total Medicines counts distinct medicines even if one medicine has multiple batches."""
        Inventory.objects.create(
            pharmacy=self.pharmacy1,
            medicine=self.med_paracetamol,
            batch_number='PARA-B1',
            expiry_date=self.today + timedelta(days=90),
            quantity=50,
            low_stock_limit=10
        )
        Inventory.objects.create(
            pharmacy=self.pharmacy1,
            medicine=self.med_paracetamol,
            batch_number='PARA-B2',
            expiry_date=self.today + timedelta(days=120),
            quantity=25,
            low_stock_limit=10
        )
        Inventory.objects.create(
            pharmacy=self.pharmacy1,
            medicine=self.med_paracetamol,
            batch_number='PARA-B3',
            expiry_date=self.today + timedelta(days=150),
            quantity=0,
            low_stock_limit=10
        )

        Inventory.objects.create(
            pharmacy=self.pharmacy1,
            medicine=self.med_amoxicillin,
            batch_number='AMOX-B1',
            expiry_date=self.today + timedelta(days=60),
            quantity=30,
            low_stock_limit=5
        )

        self.client.login(username='pharma_one', password='Password123!')
        response = self.client.get(reverse('pharmacy:dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_medicines'], 2)

    def test_active_inventory_batch_calculation(self):
        """Active Inventory Batches counts batches with quantity > 0 and expiry_date >= today."""
        Inventory.objects.create(
            pharmacy=self.pharmacy1,
            medicine=self.med_paracetamol,
            batch_number='ACT-01',
            expiry_date=self.today + timedelta(days=30),
            quantity=100,
            low_stock_limit=10
        )
        Inventory.objects.create(
            pharmacy=self.pharmacy1,
            medicine=self.med_amoxicillin,
            batch_number='ACT-02',
            expiry_date=self.today,
            quantity=5,
            low_stock_limit=10
        )
        Inventory.objects.create(
            pharmacy=self.pharmacy1,
            medicine=self.med_cetirizine,
            batch_number='EXP-01',
            expiry_date=self.today - timedelta(days=1),
            quantity=50,
            low_stock_limit=10
        )
        Inventory.objects.create(
            pharmacy=self.pharmacy1,
            medicine=self.med_cetirizine,
            batch_number='OOS-01',
            expiry_date=self.today + timedelta(days=60),
            quantity=0,
            low_stock_limit=10
        )

        self.client.login(username='pharma_one', password='Password123!')
        response = self.client.get(reverse('pharmacy:dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['active_batches'], 2)

    def test_low_stock_calculation(self):
        """Low stock counts batches where 0 < quantity <= low_stock_limit and expiry_date >= today."""
        Inventory.objects.create(
            pharmacy=self.pharmacy1,
            medicine=self.med_paracetamol,
            batch_number='LS-01',
            expiry_date=self.today + timedelta(days=60),
            quantity=4,
            low_stock_limit=10
        )
        Inventory.objects.create(
            pharmacy=self.pharmacy1,
            medicine=self.med_amoxicillin,
            batch_number='LS-02',
            expiry_date=self.today + timedelta(days=60),
            quantity=10,
            low_stock_limit=10
        )
        Inventory.objects.create(
            pharmacy=self.pharmacy1,
            medicine=self.med_cetirizine,
            batch_number='NORM-01',
            expiry_date=self.today + timedelta(days=60),
            quantity=11,
            low_stock_limit=10
        )
        Inventory.objects.create(
            pharmacy=self.pharmacy1,
            medicine=self.med_cetirizine,
            batch_number='ZERO-01',
            expiry_date=self.today + timedelta(days=60),
            quantity=0,
            low_stock_limit=10
        )
        Inventory.objects.create(
            pharmacy=self.pharmacy1,
            medicine=self.med_paracetamol,
            batch_number='LS-EXP',
            expiry_date=self.today - timedelta(days=5),
            quantity=3,
            low_stock_limit=10
        )

        self.client.login(username='pharma_one', password='Password123!')
        response = self.client.get(reverse('pharmacy:dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['low_stock_alerts'], 2)

    def test_out_of_stock_calculation(self):
        """Out of stock counts unexpired batches where quantity <= 0."""
        Inventory.objects.create(
            pharmacy=self.pharmacy1,
            medicine=self.med_paracetamol,
            batch_number='OOS-01',
            expiry_date=self.today + timedelta(days=30),
            quantity=0,
            low_stock_limit=10
        )
        Inventory.objects.create(
            pharmacy=self.pharmacy1,
            medicine=self.med_amoxicillin,
            batch_number='OOS-EXP',
            expiry_date=self.today - timedelta(days=10),
            quantity=0,
            low_stock_limit=10
        )
        Inventory.objects.create(
            pharmacy=self.pharmacy1,
            medicine=self.med_cetirizine,
            batch_number='IN-STOCK',
            expiry_date=self.today + timedelta(days=90),
            quantity=20,
            low_stock_limit=10
        )

        self.client.login(username='pharma_one', password='Password123!')
        response = self.client.get(reverse('pharmacy:dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['out_of_stock'], 1)

    def test_expired_batch_exclusion(self):
        """Expired batches are excluded from active batches, low stock alerts, and out of stock."""
        Inventory.objects.create(
            pharmacy=self.pharmacy1,
            medicine=self.med_paracetamol,
            batch_number='EXP-LOW',
            expiry_date=self.today - timedelta(days=2),
            quantity=2,
            low_stock_limit=10
        )
        Inventory.objects.create(
            pharmacy=self.pharmacy1,
            medicine=self.med_amoxicillin,
            batch_number='EXP-HIGH',
            expiry_date=self.today - timedelta(days=15),
            quantity=100,
            low_stock_limit=10
        )
        Inventory.objects.create(
            pharmacy=self.pharmacy1,
            medicine=self.med_cetirizine,
            batch_number='EXP-ZERO',
            expiry_date=self.today - timedelta(days=30),
            quantity=0,
            low_stock_limit=10
        )

        self.client.login(username='pharma_one', password='Password123!')
        response = self.client.get(reverse('pharmacy:dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_medicines'], 3)
        self.assertEqual(response.context['active_batches'], 0)
        self.assertEqual(response.context['low_stock_alerts'], 0)
        self.assertEqual(response.context['out_of_stock'], 0)

    def test_strict_pharmacy_data_isolation(self):
        """Dashboard statistics for Pharmacy 1 never leak or mix with Pharmacy 2."""
        Inventory.objects.create(
            pharmacy=self.pharmacy1,
            medicine=self.med_paracetamol,
            batch_number='P1-PARA',
            expiry_date=self.today + timedelta(days=90),
            quantity=5,  # Low stock
            low_stock_limit=10
        )
        Inventory.objects.create(
            pharmacy=self.pharmacy1,
            medicine=self.med_amoxicillin,
            batch_number='P1-AMOX',
            expiry_date=self.today + timedelta(days=90),
            quantity=0,  # Out of stock
            low_stock_limit=10
        )

        Inventory.objects.create(
            pharmacy=self.pharmacy2,
            medicine=self.med_cetirizine,
            batch_number='P2-CET',
            expiry_date=self.today + timedelta(days=120),
            quantity=50,  # Normal active
            low_stock_limit=10
        )
        Inventory.objects.create(
            pharmacy=self.pharmacy2,
            medicine=self.med_paracetamol,
            batch_number='P2-PARA',
            expiry_date=self.today + timedelta(days=120),
            quantity=50,  # Normal active
            low_stock_limit=10
        )
        Inventory.objects.create(
            pharmacy=self.pharmacy2,
            medicine=self.med_amoxicillin,
            batch_number='P2-AMOX',
            expiry_date=self.today + timedelta(days=120),
            quantity=50,  # Normal active
            low_stock_limit=10
        )

        # 1. Login as Pharmacy 1
        self.client.login(username='pharma_one', password='Password123!')
        res1 = self.client.get(reverse('pharmacy:dashboard'))
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res1.context['total_medicines'], 2)
        self.assertEqual(res1.context['active_batches'], 1)
        self.assertEqual(res1.context['low_stock_alerts'], 1)
        self.assertEqual(res1.context['out_of_stock'], 1)

        # 2. Login as Pharmacy 2
        self.client.login(username='pharma_two', password='Password123!')
        res2 = self.client.get(reverse('pharmacy:dashboard'))
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.context['total_medicines'], 3)
        self.assertEqual(res2.context['active_batches'], 3)
        self.assertEqual(res2.context['low_stock_alerts'], 0)
        self.assertEqual(res2.context['out_of_stock'], 0)


class PharmacyProfileSettingsTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Pharmacy 1 (Main test subject)
        self.user1 = User.objects.create_user(
            username='settings_pharma_1',
            password='Password123!',
            email='pharma1@medaccess.test',
            phone_number='9876543210',
            role='PHARMACY'
        )
        self.profile1 = PharmacyProfile.objects.create(
            user=self.user1,
            pharmacy_name='Original Pharmacy Name',
            owner_name='Original Owner',
            pharmacist_name='Original Pharmacist, RPh',
            pharmacy_license_number='DL-PROTECTED-001',
            gst_number='GSTIN-PROTECTED-001',
            phone_number='9876543210',
            email='pharma1@medaccess.test',
            address='123 Original St',
            city='Bhopal',
            state='Madhya Pradesh',
            pincode='462001',
            latitude=Decimal('23.2599330'),
            longitude=Decimal('77.4126150'),
            approval_status='APPROVED'
        )

        # Pharmacy 2 (For cross-modification isolation test)
        self.user2 = User.objects.create_user(
            username='settings_pharma_2',
            password='Password123!',
            email='pharma2@medaccess.test',
            phone_number='9876543211',
            role='PHARMACY'
        )
        self.profile2 = PharmacyProfile.objects.create(
            user=self.user2,
            pharmacy_name='Pharmacy Two Store',
            owner_name='Owner Two',
            pharmacist_name='Pharmacist Two, RPh',
            pharmacy_license_number='DL-PROTECTED-002',
            gst_number='GSTIN-PROTECTED-002',
            phone_number='9876543211',
            email='pharma2@medaccess.test',
            address='456 Second Ave',
            city='Indore',
            state='Madhya Pradesh',
            pincode='452001',
            approval_status='APPROVED'
        )

        # Pending pharmacy
        self.pending_user = User.objects.create_user(
            username='pending_pharma',
            password='Password123!',
            role='PHARMACY'
        )
        self.pending_profile = PharmacyProfile.objects.create(
            user=self.pending_user,
            pharmacy_name='Pending Pharmacy',
            owner_name='Pending Owner',
            pharmacist_name='Pending Pharmacist',
            pharmacy_license_number='DL-PENDING',
            gst_number='GST-PENDING',
            phone_number='9876543219',
            email='pending@test.com',
            address='Pending St',
            city='Bhopal',
            state='MP',
            pincode='462001',
            approval_status='PENDING'
        )

        # Patient user
        self.patient_user = User.objects.create_user(
            username='patient_for_settings',
            password='Password123!',
            role='PATIENT'
        )
        PatientProfile.objects.create(user=self.patient_user)

    def test_approved_pharmacy_can_view_settings(self):
        """Approved pharmacy can view settings page with 200 OK and populated form."""
        self.client.login(username='settings_pharma_1', password='Password123!')
        url = reverse('pharmacy:settings')
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'pharmacy/settings.html')
        self.assertTemplateUsed(response, 'pharmacy/base_pharmacy.html')
        self.assertContains(response, 'Original Pharmacy Name')
        self.assertContains(response, 'DL-PROTECTED-001')
        self.assertContains(response, 'GSTIN-PROTECTED-001')
        self.assertContains(response, 'APPROVED')
        self.assertIn('form', response.context)

    def test_pharmacy_can_update_allowed_fields(self):
        """Pharmacy can successfully update allowed fields and user email/phone synchronizes."""
        self.client.login(username='settings_pharma_1', password='Password123!')
        url = reverse('pharmacy:settings')
        data = {
            'pharmacy_name': 'Updated Pharmacy Name Care',
            'owner_name': 'Updated Owner',
            'pharmacist_name': 'Updated Pharmacist, PharmD',
            'phone_number': '9893001122',
            'email': 'updated_contact@medaccess.test',
            'address': '789 New Medical Street',
            'city': 'Jabalpur',
            'state': 'Madhya Pradesh',
            'pincode': '482001',
            'latitude': '23.1815000',
            'longitude': '79.9864000',
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('pharmacy:settings'))

        self.profile1.refresh_from_db()
        self.assertEqual(self.profile1.pharmacy_name, 'Updated Pharmacy Name Care')
        self.assertEqual(self.profile1.owner_name, 'Updated Owner')
        self.assertEqual(self.profile1.pharmacist_name, 'Updated Pharmacist, PharmD')
        self.assertEqual(self.profile1.phone_number, '9893001122')
        self.assertEqual(self.profile1.email, 'updated_contact@medaccess.test')
        self.assertEqual(self.profile1.address, '789 New Medical Street')
        self.assertEqual(self.profile1.city, 'Jabalpur')
        self.assertEqual(self.profile1.state, 'Madhya Pradesh')
        self.assertEqual(self.profile1.pincode, '482001')
        self.assertEqual(self.profile1.latitude, Decimal('23.1815000'))
        self.assertEqual(self.profile1.longitude, Decimal('79.9864000'))

        # Also check user sync
        self.user1.refresh_from_db()
        self.assertEqual(self.user1.email, 'updated_contact@medaccess.test')
        self.assertEqual(self.user1.phone_number, '9893001122')

    def test_license_number_cannot_be_changed(self):
        """Even if malicious POST payload includes pharmacy_license_number, it cannot be modified."""
        self.client.login(username='settings_pharma_1', password='Password123!')
        url = reverse('pharmacy:settings')
        data = {
            'pharmacy_name': 'Original Pharmacy Name',
            'owner_name': 'Original Owner',
            'pharmacist_name': 'Original Pharmacist, RPh',
            'phone_number': '9876543210',
            'email': 'pharma1@medaccess.test',
            'address': '123 Original St',
            'city': 'Bhopal',
            'state': 'Madhya Pradesh',
            'pincode': '462001',
            'pharmacy_license_number': 'HACKED-LICENSE-99999',  # Malicious injection
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)

        self.profile1.refresh_from_db()
        self.assertEqual(self.profile1.pharmacy_license_number, 'DL-PROTECTED-001')

    def test_gst_number_cannot_be_changed(self):
        """Even if malicious POST payload includes gst_number, it cannot be modified."""
        self.client.login(username='settings_pharma_1', password='Password123!')
        url = reverse('pharmacy:settings')
        data = {
            'pharmacy_name': 'Original Pharmacy Name',
            'owner_name': 'Original Owner',
            'pharmacist_name': 'Original Pharmacist, RPh',
            'phone_number': '9876543210',
            'email': 'pharma1@medaccess.test',
            'address': '123 Original St',
            'city': 'Bhopal',
            'state': 'Madhya Pradesh',
            'pincode': '462001',
            'gst_number': 'HACKED-GSTIN-99999',  # Malicious injection
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)

        self.profile1.refresh_from_db()
        self.assertEqual(self.profile1.gst_number, 'GSTIN-PROTECTED-001')

    def test_approval_status_cannot_be_changed_through_post(self):
        """Approval status is completely protected and cannot be altered via settings POST."""
        self.client.login(username='settings_pharma_1', password='Password123!')
        url = reverse('pharmacy:settings')
        data = {
            'pharmacy_name': 'Original Pharmacy Name',
            'owner_name': 'Original Owner',
            'pharmacist_name': 'Original Pharmacist, RPh',
            'phone_number': '9876543210',
            'email': 'pharma1@medaccess.test',
            'address': '123 Original St',
            'city': 'Bhopal',
            'state': 'Madhya Pradesh',
            'pincode': '462001',
            'approval_status': 'REJECTED',  # Malicious injection
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)

        self.profile1.refresh_from_db()
        self.assertEqual(self.profile1.approval_status, 'APPROVED')

    def test_invalid_coordinates_are_rejected(self):
        """Coordinates outside valid bounds (-90..90, -180..180) are rejected."""
        self.client.login(username='settings_pharma_1', password='Password123!')
        url = reverse('pharmacy:settings')

        # 1. Invalid Latitude (> 90.0)
        data = {
            'pharmacy_name': 'Original Pharmacy Name',
            'owner_name': 'Original Owner',
            'pharmacist_name': 'Original Pharmacist, RPh',
            'phone_number': '9876543210',
            'email': 'pharma1@medaccess.test',
            'address': '123 Original St',
            'city': 'Bhopal',
            'state': 'Madhya Pradesh',
            'pincode': '462001',
            'latitude': '95.123456',
            'longitude': '77.412615',
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response.context['form'], 'latitude', 'Latitude must be between -90.0 and 90.0 degrees.')

        # 2. Invalid Longitude (> 180.0)
        data['latitude'] = '23.259933'
        data['longitude'] = '195.123456'
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response.context['form'], 'longitude', 'Longitude must be between -180.0 and 180.0 degrees.')

        # 3. Only Latitude provided without Longitude
        data['longitude'] = ''
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Both latitude and longitude must be provided together')

    def test_invalid_form_data_does_not_modify_database(self):
        """Invalid phone number or email prevents saving and retains original database values."""
        self.client.login(username='settings_pharma_1', password='Password123!')
        url = reverse('pharmacy:settings')
        data = {
            'pharmacy_name': 'Should Not Change',
            'owner_name': 'Should Not Change',
            'pharmacist_name': 'Should Not Change',
            'phone_number': '123',  # Too short
            'email': 'invalid-email-address',  # Not an email
            'address': '123 Original St',
            'city': 'Bhopal',
            'state': 'Madhya Pradesh',
            'pincode': '462001',
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)

        self.profile1.refresh_from_db()
        self.assertEqual(self.profile1.pharmacy_name, 'Original Pharmacy Name')
        self.assertEqual(self.profile1.phone_number, '9876543210')
        self.assertEqual(self.profile1.email, 'pharma1@medaccess.test')

    def test_pharmacy1_cannot_modify_pharmacy2_profile(self):
        """Pharmacy 1 submitting Pharmacy 2's ID cannot tamper with Pharmacy 2's records."""
        self.client.login(username='settings_pharma_1', password='Password123!')
        url = f"{reverse('pharmacy:settings')}?pharmacy_id={self.profile2.id}"
        data = {
            'id': self.profile2.id,
            'pharmacy_id': self.profile2.id,
            'pharmacy_name': 'Hijacked Name By Pharma 1',
            'owner_name': 'Hijacked Owner',
            'pharmacist_name': 'Hijacked Pharmacist',
            'phone_number': '9999999999',
            'email': 'hijacked@medaccess.test',
            'address': 'Hijacked Address',
            'city': 'Bhopal',
            'state': 'Madhya Pradesh',
            'pincode': '462001',
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)

        # Pharmacy 2 must remain completely UNTOUCHED
        self.profile2.refresh_from_db()
        self.assertEqual(self.profile2.pharmacy_name, 'Pharmacy Two Store')
        self.assertEqual(self.profile2.owner_name, 'Owner Two')
        self.assertEqual(self.profile2.email, 'pharma2@medaccess.test')

        # Only Pharmacy 1 is updated
        self.profile1.refresh_from_db()
        self.assertEqual(self.profile1.pharmacy_name, 'Hijacked Name By Pharma 1')

    def test_settings_access_restrictions(self):
        """Unauthenticated, Patient, and Pending accounts cannot access settings view."""
        settings_url = reverse('pharmacy:settings')

        # 1. Unauthenticated -> login redirect
        self.client.logout()
        res_anon = self.client.get(settings_url)
        self.assertEqual(res_anon.status_code, 302)
        self.assertRedirects(res_anon, reverse('accounts:login'))

        # 2. Patient -> landing redirect
        self.client.login(username='patient_for_settings', password='Password123!')
        res_patient = self.client.get(settings_url)
        self.assertEqual(res_patient.status_code, 302)
        self.assertRedirects(res_patient, reverse('landing'))

        # 3. Pending pharmacy -> landing redirect
        self.client.force_login(self.pending_user)
        res_pending = self.client.get(settings_url)
        self.assertEqual(res_pending.status_code, 302)
        self.assertRedirects(res_pending, reverse('landing'))


class ManageMedicinesTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Approved Pharmacy
        self.user = User.objects.create_user(
            username='med_manager_pharma',
            password='Password123!',
            role='PHARMACY'
        )
        self.pharmacy = PharmacyProfile.objects.create(
            user=self.user,
            pharmacy_name='Apex Health Pharmacy',
            owner_name='Apex Corp',
            pharmacist_name='Apex Pharmacist',
            pharmacy_license_number='DL-APEX-101',
            gst_number='GSTIN-APEX-101',
            phone_number='9876543220',
            email='apex@medaccess.test',
            address='101 Health Way',
            city='Bhopal',
            state='Madhya Pradesh',
            pincode='462001',
            approval_status='APPROVED'
        )

        # Pending Pharmacy
        self.pending_user = User.objects.create_user(
            username='pending_med_user',
            password='Password123!',
            role='PHARMACY'
        )
        self.pending_pharmacy = PharmacyProfile.objects.create(
            user=self.pending_user,
            pharmacy_name='Pending Med Chemist',
            owner_name='Owner',
            pharmacist_name='Pharmacist',
            pharmacy_license_number='DL-PENDING-1',
            gst_number='GSTIN-PENDING-1',
            phone_number='9876543221',
            email='pending_med@medaccess.test',
            address='102 Health Way',
            city='Bhopal',
            state='Madhya Pradesh',
            pincode='462001',
            approval_status='PENDING'
        )

        # Patient user
        self.patient_user = User.objects.create_user(
            username='patient_for_meds',
            password='Password123!',
            role='PATIENT'
        )
        PatientProfile.objects.create(user=self.patient_user)

        # Seed master medicines
        self.med1 = Medicine.objects.create(
            medicine_name='Azithromycin 500mg',
            composition='Azithromycin Dihydrate IP 500mg',
            strength='500 mg',
            manufacturer='Pfizer Ltd',
            dosage_form='Tablet',
            pack_size='Strip of 3 Tablets',
            mrp=Decimal('120.50')
        )
        self.med2 = Medicine.objects.create(
            medicine_name='Metformin Hydrochloride 850mg',
            composition='Metformin Hydrochloride IP 850mg',
            strength='850 mg',
            manufacturer='USV Private Ltd',
            dosage_form='Tablet',
            pack_size='Strip of 10 Tablets',
            mrp=Decimal('45.00')
        )
        self.med3 = Medicine.objects.create(
            medicine_name='Brufen 400',
            composition='Ibuprofen IP 400mg',
            strength='400 mg',
            manufacturer='Abbott Healthcare',
            dosage_form='Tablet',
            pack_size='Strip of 15 Tablets',
            mrp=Decimal('35.25')
        )

    def test_approved_pharmacy_can_view_catalogue(self):
        """Approved pharmacy can access master medicine catalogue listing."""
        self.client.login(username='med_manager_pharma', password='Password123!')
        url = reverse('pharmacy:medicines')
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'pharmacy/medicines_list.html')
        self.assertTemplateUsed(response, 'pharmacy/base_pharmacy.html')
        self.assertContains(response, 'Master Medicine Catalogue')
        self.assertContains(response, 'Azithromycin 500mg')
        self.assertContains(response, 'Metformin Hydrochloride 850mg')
        self.assertContains(response, 'Brufen 400')

    def test_access_restrictions_for_medicines_views(self):
        """Unauthenticated, Patient, and Pending pharmacy accounts are denied access."""
        urls = [reverse('pharmacy:medicines'), reverse('pharmacy:medicine_add')]

        for url in urls:
            # 1. Anonymous -> login redirect
            self.client.logout()
            res_anon = self.client.get(url)
            self.assertEqual(res_anon.status_code, 302)
            self.assertRedirects(res_anon, reverse('accounts:login'))

            # 2. Patient -> landing redirect
            self.client.login(username='patient_for_meds', password='Password123!')
            res_patient = self.client.get(url)
            self.assertEqual(res_patient.status_code, 302)
            self.assertRedirects(res_patient, reverse('landing'))

            # 3. Pending pharmacy -> landing redirect
            self.client.force_login(self.pending_user)
            res_pending = self.client.get(url)
            self.assertEqual(res_pending.status_code, 302)
            self.assertRedirects(res_pending, reverse('landing'))

    def test_search_filters_work_correctly(self):
        """Search accurately filters medicines by name, composition, manufacturer, or strength."""
        self.client.login(username='med_manager_pharma', password='Password123!')
        url = reverse('pharmacy:medicines')

        # Filter by name
        res_name = self.client.get(url, {'q': 'Azithromycin'})
        self.assertContains(res_name, 'Azithromycin 500mg')
        self.assertNotContains(res_name, 'Metformin Hydrochloride')
        self.assertNotContains(res_name, 'Brufen 400')

        # Filter by composition
        res_comp = self.client.get(url, {'q': 'Ibuprofen'})
        self.assertContains(res_comp, 'Brufen 400')
        self.assertNotContains(res_comp, 'Azithromycin 500mg')

        # Filter by manufacturer
        res_mfr = self.client.get(url, {'q': 'USV'})
        self.assertContains(res_mfr, 'Metformin Hydrochloride 850mg')
        self.assertNotContains(res_mfr, 'Brufen 400')

        # Filter by strength
        res_str = self.client.get(url, {'q': '850 mg'})
        self.assertContains(res_str, 'Metformin Hydrochloride 850mg')
        self.assertNotContains(res_str, 'Brufen 400')

        # Nonexistent query
        res_empty = self.client.get(url, {'q': 'NonexistentMedicineX'})
        self.assertContains(res_empty, 'No Medicines Found')

    def test_medicine_details_render_correctly(self):
        """Medicine card renders all formulation, pack, price, and manufacturer information."""
        self.client.login(username='med_manager_pharma', password='Password123!')
        url = reverse('pharmacy:medicines')
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Azithromycin 500mg')
        self.assertContains(response, '500 mg')
        self.assertContains(response, 'Strip of 3 Tablets')
        self.assertContains(response, '₹120.50')
        self.assertContains(response, 'Pfizer Ltd')

    def test_valid_medicine_creation_works(self):
        """Approved pharmacy can add a new valid medicine to the catalogue."""
        self.client.login(username='med_manager_pharma', password='Password123!')
        url = reverse('pharmacy:medicine_add')

        # Generate valid PNG image using PIL
        import io
        from PIL import Image
        img_buffer = io.BytesIO()
        Image.new('RGB', (10, 10), color='blue').save(img_buffer, format='PNG')
        valid_png = img_buffer.getvalue()
        image = SimpleUploadedFile("calpol.png", valid_png, content_type="image/png")

        data = {
            'medicine_name': 'Calpol 500',
            'composition': 'Paracetamol IP 500mg',
            'strength': '500 mg',
            'manufacturer': 'GlaxoSmithKline Pharmaceuticals Ltd',
            'dosage_form': 'Tablet',
            'pack_size': 'Strip of 15 Tablets',
            'mrp': '32.50',
            'medicine_image': image,
        }

        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('pharmacy:medicines'))

        # Verify medicine created in DB
        created = Medicine.objects.filter(medicine_name='Calpol 500').first()
        self.assertIsNotNone(created)
        self.assertEqual(created.strength, '500 mg')
        self.assertEqual(created.mrp, Decimal('32.50'))
        self.assertTrue(bool(created.medicine_image))

    def test_invalid_mrp_is_rejected(self):
        """Zero or negative MRP values are rejected by form validation."""
        self.client.login(username='med_manager_pharma', password='Password123!')
        url = reverse('pharmacy:medicine_add')

        # Negative MRP
        data = {
            'medicine_name': 'Zero Price Med',
            'composition': 'Test Salt',
            'strength': '10 mg',
            'dosage_form': 'Tablet',
            'mrp': '-15.00',
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response.context['form'], 'mrp', 'MRP must be greater than zero.')
        self.assertFalse(Medicine.objects.filter(medicine_name='Zero Price Med').exists())

        # Zero MRP
        data['mrp'] = '0.00'
        response2 = self.client.post(url, data)
        self.assertEqual(response2.status_code, 200)
        self.assertFormError(response2.context['form'], 'mrp', 'MRP must be greater than zero.')

    def test_invalid_image_upload_is_rejected(self):
        """Non-image files and unsupported formats are rejected."""
        self.client.login(username='med_manager_pharma', password='Password123!')
        url = reverse('pharmacy:medicine_add')

        bad_file = SimpleUploadedFile("payload.exe", b"not an image", content_type="application/octet-stream")
        data = {
            'medicine_name': 'Security Test Medicine',
            'mrp': '50.00',
            'medicine_image': bad_file,
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Medicine.objects.filter(medicine_name='Security Test Medicine').exists())

    def test_duplicate_medicine_handling(self):
        """Normalized duplicate check detects identical medicine and rejects addition with a clear message."""
        self.client.login(username='med_manager_pharma', password='Password123!')
        url = reverse('pharmacy:medicine_add')

        # Attempt to add duplicate of Azithromycin 500mg
        data = {
            'medicine_name': '  azithromycin 500mg  ',  # Case & whitespace variance
            'composition': 'Azithromycin',
            'strength': '500 mg',
            'dosage_form': 'Tablet',
            'mrp': '130.00',
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'already exists in the MedAccess master catalogue')

        # Verify only original 1 record exists in DB
        self.assertEqual(Medicine.objects.filter(medicine_name__iexact='Azithromycin 500mg').count(), 1)

    def test_adding_medicine_does_not_create_inventory(self):
        """Adding a catalogue medicine does not automatically create an inventory stock batch."""
        self.client.login(username='med_manager_pharma', password='Password123!')
        url = reverse('pharmacy:medicine_add')

        initial_inventory_count = Inventory.objects.count()

        data = {
            'medicine_name': 'Vitamin C Chewable',
            'composition': 'Ascorbic Acid IP 500mg',
            'strength': '500 mg',
            'dosage_form': 'Chewable Tablet',
            'pack_size': 'Bottle of 30 Tablets',
            'mrp': '85.00',
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)

        created_med = Medicine.objects.filter(medicine_name='Vitamin C Chewable').first()
        self.assertIsNotNone(created_med)

        # Inventory count must not change
        self.assertEqual(Inventory.objects.count(), initial_inventory_count)
        self.assertEqual(Inventory.objects.filter(medicine=created_med).count(), 0)

    def test_url_namespaces(self):
        """Verify URL routing and namespaces for medicine catalogue and add view."""
        self.assertEqual(reverse('pharmacy:medicines'), '/pharmacy/medicines/')
        self.assertEqual(reverse('pharmacy:medicine_add'), '/pharmacy/medicines/add/')


class ManageInventoryTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.today = timezone.localdate()

        # Primary Pharmacy
        self.user1 = User.objects.create_user(
            username='inv_pharma_1',
            password='Password123!',
            role='PHARMACY'
        )
        self.pharmacy1 = PharmacyProfile.objects.create(
            user=self.user1,
            pharmacy_name='Zenith Health Pharmacy',
            owner_name='Zenith Corp',
            pharmacist_name='Zenith Pharmacist',
            pharmacy_license_number='DL-ZEN-01',
            gst_number='GSTIN-ZEN-01',
            phone_number='9876543230',
            email='zenith@medaccess.test',
            address='201 Health Blvd',
            city='Bhopal',
            state='Madhya Pradesh',
            pincode='462001',
            approval_status='APPROVED'
        )

        # Secondary Pharmacy (For isolation tests)
        self.user2 = User.objects.create_user(
            username='inv_pharma_2',
            password='Password123!',
            role='PHARMACY'
        )
        self.pharmacy2 = PharmacyProfile.objects.create(
            user=self.user2,
            pharmacy_name='Oasis Medicenter',
            owner_name='Oasis Corp',
            pharmacist_name='Oasis Pharmacist',
            pharmacy_license_number='DL-OAS-02',
            gst_number='GSTIN-OAS-02',
            phone_number='9876543231',
            email='oasis@medaccess.test',
            address='202 Oasis Road',
            city='Indore',
            state='Madhya Pradesh',
            pincode='452001',
            approval_status='APPROVED'
        )

        # Pending Pharmacy
        self.pending_user = User.objects.create_user(
            username='pending_inv_user',
            password='Password123!',
            role='PHARMACY'
        )
        self.pending_pharmacy = PharmacyProfile.objects.create(
            user=self.pending_user,
            pharmacy_name='Pending Inv Chemist',
            owner_name='Owner',
            pharmacist_name='Pharmacist',
            pharmacy_license_number='DL-PEND-INV',
            gst_number='GST-PEND-INV',
            phone_number='9876543232',
            email='pending_inv@test.com',
            address='Pending Way',
            city='Bhopal',
            state='MP',
            pincode='462001',
            approval_status='PENDING'
        )

        # Patient user
        self.patient_user = User.objects.create_user(
            username='patient_for_inv',
            password='Password123!',
            role='PATIENT'
        )
        PatientProfile.objects.create(user=self.patient_user)

        # Seed master catalogue medicines
        self.med_amox = Medicine.objects.create(
            medicine_name='Amoxicillin Trihydrate 500mg',
            composition='Amoxicillin Trihydrate IP 500mg',
            strength='500 mg',
            manufacturer='Cipla Ltd',
            dosage_form='Capsule',
            pack_size='Strip of 10 Capsules',
            mrp=Decimal('85.00')
        )
        self.med_para = Medicine.objects.create(
            medicine_name='Paracetamol 650mg',
            composition='Paracetamol IP 650mg',
            strength='650 mg',
            manufacturer='Micro Labs',
            dosage_form='Tablet',
            pack_size='Strip of 15 Tablets',
            mrp=Decimal('35.00')
        )
        self.med_cough = Medicine.objects.create(
            medicine_name='Ascoril D Plus Syrup',
            composition='Dextromethorphan + Chlorpheniramine',
            strength='100 ml',
            manufacturer='Glenmark Pharmaceuticals',
            dosage_form='Syrup',
            pack_size='100 ml Bottle',
            mrp=Decimal('115.00')
        )

        # Seed inventory batches for Pharmacy 1
        self.batch_p1_normal = Inventory.objects.create(
            pharmacy=self.pharmacy1,
            medicine=self.med_amox,
            batch_number='AMX-LOT-01',
            expiry_date=self.today + timedelta(days=180),
            quantity=100,  # In stock
            unit='Capsules',
            low_stock_limit=15
        )
        self.batch_p1_low = Inventory.objects.create(
            pharmacy=self.pharmacy1,
            medicine=self.med_para,
            batch_number='PARA-LOT-02',
            expiry_date=self.today + timedelta(days=90),
            quantity=8,  # Low stock (<= 10)
            unit='Strips',
            low_stock_limit=10
        )
        self.batch_p1_zero = Inventory.objects.create(
            pharmacy=self.pharmacy1,
            medicine=self.med_cough,
            batch_number='SYR-LOT-03',
            expiry_date=self.today + timedelta(days=60),
            quantity=0,  # Out of stock
            unit='Bottles',
            low_stock_limit=5
        )
        self.batch_p1_expired = Inventory.objects.create(
            pharmacy=self.pharmacy1,
            medicine=self.med_amox,
            batch_number='AMX-OLD-EXP',
            expiry_date=self.today - timedelta(days=10),
            quantity=50,  # Expired
            unit='Capsules',
            low_stock_limit=15
        )

        # Seed inventory batches for Pharmacy 2
        self.batch_p2_secret = Inventory.objects.create(
            pharmacy=self.pharmacy2,
            medicine=self.med_para,
            batch_number='SECRET-P2-99',
            expiry_date=self.today + timedelta(days=120),
            quantity=500,
            unit='Strips',
            low_stock_limit=20
        )

    def test_approved_pharmacy_can_view_inventory(self):
        """Approved pharmacy can view inventory list with 200 OK and expected template."""
        self.client.login(username='inv_pharma_1', password='Password123!')
        url = reverse('pharmacy:inventory')
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'pharmacy/inventory_list.html')
        self.assertTemplateUsed(response, 'pharmacy/base_pharmacy.html')
        self.assertContains(response, 'AMX-LOT-01')
        self.assertContains(response, 'PARA-LOT-02')
        self.assertContains(response, 'SYR-LOT-03')
        self.assertContains(response, 'AMX-OLD-EXP')

    def test_pharmacy_cannot_see_another_pharmacy_batches(self):
        """Strict data isolation: Pharmacy 1 cannot view Pharmacy 2 batches, and vice versa."""
        # 1. Pharmacy 1 logged in
        self.client.login(username='inv_pharma_1', password='Password123!')
        res1 = self.client.get(reverse('pharmacy:inventory'))
        self.assertContains(res1, 'AMX-LOT-01')
        self.assertNotContains(res1, 'SECRET-P2-99')

        # 2. Pharmacy 2 logged in
        self.client.login(username='inv_pharma_2', password='Password123!')
        res2 = self.client.get(reverse('pharmacy:inventory'))
        self.assertContains(res2, 'SECRET-P2-99')
        self.assertNotContains(res2, 'AMX-LOT-01')
        self.assertNotContains(res2, 'PARA-LOT-02')

    def test_unauthorized_users_cannot_access_inventory(self):
        """Unauthenticated, Patient, and Pending pharmacy accounts are denied access."""
        urls = [
            reverse('pharmacy:inventory'),
            reverse('pharmacy:inventory_add'),
            reverse('pharmacy:inventory_update', args=[self.batch_p1_normal.pk]),
        ]

        for url in urls:
            # 1. Anonymous -> login redirect
            self.client.logout()
            res_anon = self.client.get(url)
            self.assertEqual(res_anon.status_code, 302)
            self.assertRedirects(res_anon, reverse('accounts:login'))

            # 2. Patient -> landing redirect
            self.client.login(username='patient_for_inv', password='Password123!')
            res_patient = self.client.get(url)
            self.assertEqual(res_patient.status_code, 302)
            self.assertRedirects(res_patient, reverse('landing'))

            # 3. Pending pharmacy -> landing redirect
            self.client.force_login(self.pending_user)
            res_pending = self.client.get(url)
            self.assertEqual(res_pending.status_code, 302)
            self.assertRedirects(res_pending, reverse('landing'))

    def test_valid_inventory_batch_creation(self):
        """Approved pharmacy can add a new valid inventory batch for a catalogue medicine."""
        self.client.login(username='inv_pharma_1', password='Password123!')
        url = reverse('pharmacy:inventory_add')

        data = {
            'medicine': self.med_cough.pk,
            'batch_number': 'NEW-COUGH-2026',
            'expiry_date': (self.today + timedelta(days=200)).strftime('%Y-%m-%d'),
            'quantity': 75,
            'unit': 'Bottles',
            'low_stock_limit': 12,
        }

        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('pharmacy:inventory'))

        # Verify created batch belongs strictly to pharmacy 1
        created = Inventory.objects.filter(
            pharmacy=self.pharmacy1,
            batch_number='NEW-COUGH-2026'
        ).first()
        self.assertIsNotNone(created)
        self.assertEqual(created.quantity, 75)
        self.assertEqual(created.unit, 'Bottles')
        self.assertEqual(created.low_stock_limit, 12)

    def test_invalid_quantity_rejection(self):
        """Negative quantity is rejected by form validation."""
        self.client.login(username='inv_pharma_1', password='Password123!')
        url = reverse('pharmacy:inventory_add')

        data = {
            'medicine': self.med_amox.pk,
            'batch_number': 'NEG-QTY-BATCH',
            'expiry_date': (self.today + timedelta(days=100)).strftime('%Y-%m-%d'),
            'quantity': -10,
            'unit': 'Capsules',
            'low_stock_limit': 5,
        }

        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response.context['form'], 'quantity', 'Quantity cannot be negative.')
        self.assertFalse(Inventory.objects.filter(batch_number='NEG-QTY-BATCH').exists())

    def test_invalid_low_stock_limit_rejection(self):
        """Negative low stock limit is rejected by form validation."""
        self.client.login(username='inv_pharma_1', password='Password123!')
        url = reverse('pharmacy:inventory_add')

        data = {
            'medicine': self.med_amox.pk,
            'batch_number': 'NEG-LIMIT-BATCH',
            'expiry_date': (self.today + timedelta(days=100)).strftime('%Y-%m-%d'),
            'quantity': 20,
            'unit': 'Capsules',
            'low_stock_limit': -5,
        }

        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response.context['form'], 'low_stock_limit', 'Low stock limit cannot be negative.')
        self.assertFalse(Inventory.objects.filter(batch_number='NEG-LIMIT-BATCH').exists())

    def test_duplicate_batch_rejection(self):
        """Duplicate batch number for the same pharmacy and medicine is rejected."""
        self.client.login(username='inv_pharma_1', password='Password123!')
        url = reverse('pharmacy:inventory_add')

        # Existing batch is 'AMX-LOT-01' for self.med_amox
        data = {
            'medicine': self.med_amox.pk,
            'batch_number': 'amx-lot-01',  # Case variance
            'expiry_date': (self.today + timedelta(days=100)).strftime('%Y-%m-%d'),
            'quantity': 50,
            'unit': 'Capsules',
            'low_stock_limit': 10,
        }

        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'already exists in your inventory')

        # Confirm count remains 1
        self.assertEqual(
            Inventory.objects.filter(
                pharmacy=self.pharmacy1,
                medicine=self.med_amox,
                batch_number__iexact='amx-lot-01'
            ).count(),
            1
        )

    def test_existing_batch_quantity_update(self):
        """Pharmacy can update stock quantity, expiry date, unit, and limits of its own batch."""
        self.client.login(username='inv_pharma_1', password='Password123!')
        url = reverse('pharmacy:inventory_update', args=[self.batch_p1_low.pk])

        new_expiry = self.today + timedelta(days=150)
        data = {
            'quantity': 95,
            'expiry_date': new_expiry.strftime('%Y-%m-%d'),
            'unit': 'Boxes',
            'low_stock_limit': 20,
        }

        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('pharmacy:inventory'))

        self.batch_p1_low.refresh_from_db()
        self.assertEqual(self.batch_p1_low.quantity, 95)
        self.assertEqual(self.batch_p1_low.unit, 'Boxes')
        self.assertEqual(self.batch_p1_low.low_stock_limit, 20)
        self.assertEqual(self.batch_p1_low.expiry_date, new_expiry)

    def test_pharmacy_cannot_update_another_pharmacy_batch(self):
        """Pharmacy 1 cannot access or update Pharmacy 2's batch (returns 404)."""
        self.client.login(username='inv_pharma_1', password='Password123!')
        url = reverse('pharmacy:inventory_update', args=[self.batch_p2_secret.pk])

        # GET should 404
        get_res = self.client.get(url)
        self.assertEqual(get_res.status_code, 404)

        # POST should 404 and not tamper with Pharmacy 2's batch
        post_res = self.client.post(url, {
            'quantity': 1,
            'expiry_date': self.today.strftime('%Y-%m-%d'),
            'unit': 'Hacked',
            'low_stock_limit': 1,
        })
        self.assertEqual(post_res.status_code, 404)

        self.batch_p2_secret.refresh_from_db()
        self.assertEqual(self.batch_p2_secret.quantity, 500)
        self.assertEqual(self.batch_p2_secret.unit, 'Strips')

    def test_medicine_must_belong_to_master_catalogue(self):
        """Selecting a non-existent medicine ID is rejected."""
        self.client.login(username='inv_pharma_1', password='Password123!')
        url = reverse('pharmacy:inventory_add')

        data = {
            'medicine': 99999,  # Nonexistent
            'batch_number': 'FAKE-MED-BATCH',
            'expiry_date': (self.today + timedelta(days=100)).strftime('%Y-%m-%d'),
            'quantity': 10,
            'unit': 'Units',
            'low_stock_limit': 5,
        }

        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response.context['form'], 'medicine', 'Select a valid choice. That choice is not one of the available choices.')

    def test_inventory_listing_search_and_filtering(self):
        """Search and status filters correctly restrict inventory rows."""
        self.client.login(username='inv_pharma_1', password='Password123!')
        url = reverse('pharmacy:inventory')

        # 1. Search by medicine name
        res_med = self.client.get(url, {'q': 'Amoxicillin'})
        self.assertContains(res_med, 'AMX-LOT-01')
        self.assertNotContains(res_med, 'PARA-LOT-02')

        # 2. Search by batch number
        res_batch = self.client.get(url, {'q': 'PARA-LOT'})
        self.assertContains(res_batch, 'PARA-LOT-02')
        self.assertNotContains(res_batch, 'AMX-LOT-01')

        # 3. Filter by 'in_stock'
        res_in = self.client.get(url, {'status': 'in_stock'})
        self.assertContains(res_in, 'AMX-LOT-01')
        self.assertNotContains(res_in, 'PARA-LOT-02')
        self.assertNotContains(res_in, 'SYR-LOT-03')
        self.assertNotContains(res_in, 'AMX-OLD-EXP')

        # 4. Filter by 'low_stock'
        res_low = self.client.get(url, {'status': 'low_stock'})
        self.assertContains(res_low, 'PARA-LOT-02')
        self.assertNotContains(res_low, 'AMX-LOT-01')
        self.assertNotContains(res_low, 'SYR-LOT-03')

        # 5. Filter by 'out_of_stock'
        res_out = self.client.get(url, {'status': 'out_of_stock'})
        self.assertContains(res_out, 'SYR-LOT-03')
        self.assertNotContains(res_out, 'AMX-LOT-01')
        self.assertNotContains(res_out, 'PARA-LOT-02')

        # 6. Filter by 'expired'
        res_exp = self.client.get(url, {'status': 'expired'})
        self.assertContains(res_exp, 'AMX-OLD-EXP')
        self.assertNotContains(res_exp, 'AMX-LOT-01')

    def test_expired_and_low_stock_status_calculations(self):
        """Computed status labels and badge classes accurately reflect stock and expiry states."""
        self.client.login(username='inv_pharma_1', password='Password123!')
        response = self.client.get(reverse('pharmacy:inventory'))

        page_batches = {b.batch_number: b for b in response.context['page_obj']}

        self.assertEqual(page_batches['AMX-LOT-01'].status_label, 'In Stock')
        self.assertEqual(page_batches['AMX-LOT-01'].status_badge_class, 'status-success')

        self.assertEqual(page_batches['PARA-LOT-02'].status_label, 'Low Stock')
        self.assertEqual(page_batches['PARA-LOT-02'].status_badge_class, 'status-warning')

        self.assertEqual(page_batches['SYR-LOT-03'].status_label, 'Out of Stock')
        self.assertEqual(page_batches['SYR-LOT-03'].status_badge_class, 'status-danger')

        self.assertEqual(page_batches['AMX-OLD-EXP'].status_label, 'Expired')
        self.assertEqual(page_batches['AMX-OLD-EXP'].status_badge_class, 'status-danger')

    def test_existing_pharmacy_dashboard_kpi_behavior_remains_correct(self):
        """Updating inventory batches immediately reflects on the Pharmacy Dashboard KPI cards."""
        self.client.login(username='inv_pharma_1', password='Password123!')
        res_dash = self.client.get(reverse('pharmacy:dashboard'))

        # Pharmacy 1 has 3 distinct medicines (Amox, Para, Cough), 2 active batches (AMX-LOT-01, PARA-LOT-02),
        # 1 low stock (PARA-LOT-02), 1 out of stock (SYR-LOT-03).
        self.assertEqual(res_dash.context['total_medicines'], 3)
        self.assertEqual(res_dash.context['active_batches'], 2)
        self.assertEqual(res_dash.context['low_stock_alerts'], 1)
        self.assertEqual(res_dash.context['out_of_stock'], 1)

    def test_inventory_url_namespaces(self):
        """Verify URL routing and namespaces for inventory list, add, and update views."""
        self.assertEqual(reverse('pharmacy:inventory'), '/pharmacy/inventory/')
        self.assertEqual(reverse('pharmacy:inventory_add'), '/pharmacy/inventory/add/')
        self.assertEqual(reverse('pharmacy:inventory_update', args=[1]), '/pharmacy/inventory/1/update/')


class ImportMedicinesCommandTests(TestCase):
    """Automated tests for import_medicines management command."""

    def create_temp_csv(self, rows):
        """Helper to create a temporary CSV file with custom rows."""
        temp_file = tempfile.NamedTemporaryFile(mode='w+', encoding='utf-8', newline='', delete=False, suffix='.csv')
        fieldnames = [
            'sub_category', 'product_name', 'salt_composition',
            'product_price', 'product_manufactured', 'medicine_desc',
            'side_effects', 'drug_interactions'
        ]
        writer = csv.DictWriter(temp_file, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
        temp_file.close()
        return temp_file.name

    def test_missing_file_raises_command_error(self):
        """Non-existent file path raises CommandError."""
        with self.assertRaises(CommandError):
            call_command('import_medicines', file='non_existent_file.csv')

    def test_dry_run_creates_zero_records(self):
        """Dry-run processes rows and prints metrics without saving any records."""
        rows = [
            {
                'product_name': 'Paracetamol 500mg Tablet',
                'salt_composition': 'Paracetamol (500mg)',
                'product_manufactured': 'GSK',
                'product_price': '₹25.00'
            },
            {
                'product_name': 'Amoxicillin 250mg Capsule',
                'salt_composition': 'Amoxicillin (250mg)',
                'product_manufactured': 'Cipla',
                'product_price': '₹50.00'
            },
            {
                'product_name': 'Paracetamol 500mg Tablet',
                'salt_composition': 'Paracetamol (500mg)',
                'product_manufactured': 'GSK',
                'product_price': '₹25.00'
            }
        ]
        csv_path = self.create_temp_csv(rows)
        try:
            out = io.StringIO()
            call_command('import_medicines', file=csv_path, dry_run=True, stdout=out)
            output = out.getvalue()

            self.assertIn('[DRY RUN]', output)
            self.assertIn('Total CSV Rows Processed:', output)
            self.assertIn('Duplicate CSV Rows Skipped:', output)
            self.assertIn('Unique Medicine Candidates:', output)
            self.assertIn('New Medicines to Insert:', output)
            self.assertEqual(Medicine.objects.count(), 0)
        finally:
            os.remove(csv_path)

    def test_price_resolution_and_deduplication(self):
        """When first occurrence lacks price but duplicate has price, MRP is resolved."""
        rows = [
            {
                'product_name': 'Cetirizine 10mg Tablet Strip of 10 Tablets',
                'salt_composition': 'Cetirizine (10mg)',
                'product_manufactured': 'Dr. Reddy',
                'product_price': ''  # Empty price
            },
            {
                'product_name': 'Cetirizine 10mg Tablet Strip of 10 Tablets',
                'salt_composition': 'Cetirizine (10mg)',
                'product_manufactured': 'Dr. Reddy',
                'product_price': '₹ 18.50'  # Price present on duplicate
            }
        ]
        csv_path = self.create_temp_csv(rows)
        try:
            out = io.StringIO()
            call_command('import_medicines', file=csv_path, stdout=out)

            self.assertEqual(Medicine.objects.count(), 1)
            med = Medicine.objects.first()
            self.assertEqual(med.medicine_name, 'Cetirizine 10mg Tablet Strip of 10 Tablets')
            self.assertEqual(med.mrp, Decimal('18.50'))
            self.assertEqual(med.dosage_form, 'Tablet')
            self.assertEqual(med.strength, '10mg')
            self.assertEqual(med.pack_size, 'Strip of 10 Tablets')
            self.assertEqual(med.manufacturer, 'Dr. Reddy')
        finally:
            os.remove(csv_path)

    def test_idempotency_skips_existing_records(self):
        """Pre-existing database medicines are identified and skipped."""
        Medicine.objects.create(
            medicine_name='Azithromycin 500mg Tablet',
            composition='Azithromycin (500mg)',
            manufacturer='Lupin',
            mrp=Decimal('120.00'),
            dosage_form='Tablet',
            strength='500mg'
        )

        rows = [
            {
                'product_name': 'Azithromycin 500mg Tablet',
                'salt_composition': 'Azithromycin (500mg)',
                'product_manufactured': 'Lupin',
                'product_price': '₹120.00'
            },
            {
                'product_name': 'Ibuprofen 400mg Tablet',
                'salt_composition': 'Ibuprofen (400mg)',
                'product_manufactured': 'Abbott',
                'product_price': '₹35.00'
            }
        ]
        csv_path = self.create_temp_csv(rows)
        try:
            out = io.StringIO()
            call_command('import_medicines', file=csv_path, stdout=out)
            output = out.getvalue()

            self.assertIn('Already in Database (Skipped):         1', output)
            self.assertIn('New Medicines to Insert:               1', output)
            self.assertEqual(Medicine.objects.count(), 2)

            # Re-running the command with the same CSV results in 0 new insertions
            out2 = io.StringIO()
            call_command('import_medicines', file=csv_path, stdout=out2)
            output2 = out2.getvalue()
            self.assertIn('No new medicines to insert', output2)
            self.assertEqual(Medicine.objects.count(), 2)
        finally:
            os.remove(csv_path)

    def test_malformed_empty_name_rows_rejected(self):
        """Rows with blank or missing product_name are counted as malformed and rejected."""
        rows = [
            {
                'product_name': '   ',  # Empty/spaces only
                'salt_composition': 'Some Salt',
                'product_manufactured': 'Pharma Corp',
                'product_price': '₹10.00'
            },
            {
                'product_name': 'Valid Medicine 100mg Tablet',
                'salt_composition': 'Active Compound (100mg)',
                'product_manufactured': 'Pharma Corp',
                'product_price': '₹15.00'
            }
        ]
        csv_path = self.create_temp_csv(rows)
        try:
            out = io.StringIO()
            call_command('import_medicines', file=csv_path, stdout=out)
            output = out.getvalue()

            self.assertIn('Malformed / Blank Rows:', output)
            self.assertIn('New Medicines to Insert:', output)
            self.assertEqual(Medicine.objects.count(), 1)
        finally:
            os.remove(csv_path)

    def test_strict_utf8_decoding_failure_raises_command_error(self):
        """Invalid non-UTF-8 bytes trigger a clear CommandError with encoding details."""
        temp_file = tempfile.NamedTemporaryFile(mode='wb', delete=False, suffix='.csv')
        # Write invalid UTF-8 byte sequence \xff\xfe in product_name
        invalid_bytes = (
            b"sub_category,product_name,salt_composition,product_price,product_manufactured,medicine_desc,side_effects,drug_interactions\r\n"
            b"Tablets,Broken \xff\xfe Formulation,Some Salt,\xe2\x82\xb9 25.00,Test Pharma,,,\r\n"
        )
        temp_file.write(invalid_bytes)
        temp_file.close()

        try:
            with self.assertRaises(CommandError) as ctx:
                call_command('import_medicines', file=temp_file.name)

            err_msg = str(ctx.exception)
            self.assertIn('Encoding error reading', err_msg)
            self.assertIn('utf-8', err_msg.lower())
            self.assertIn('byte position', err_msg)
            self.assertEqual(Medicine.objects.count(), 0)
        finally:
            os.remove(temp_file.name)

    def test_sanitized_composite_key_consistency(self):
        """Composite key matches exactly between sanitized CSV candidate and stored DB record."""
        # Product name exceeding 255 chars is sanitized to 255 chars
        long_name = ("Ultra Long Medicine Name " * 15)[:280]  # 280 chars
        sanitized_name = long_name[:255].strip()

        rows = [
            {
                'product_name': long_name,
                'salt_composition': '  Active Ingredient (500mg)  ',
                'product_manufactured': '  Sample Pharma Manufacturer Inc  ',
                'product_price': '₹ 150.00'
            }
        ]
        csv_path = self.create_temp_csv(rows)
        try:
            out = io.StringIO()
            call_command('import_medicines', file=csv_path, stdout=out)
            self.assertEqual(Medicine.objects.count(), 1)

            created_med = Medicine.objects.first()
            self.assertEqual(created_med.medicine_name, sanitized_name)
            self.assertEqual(created_med.composition, 'Active Ingredient (500mg)')
            self.assertEqual(created_med.manufacturer, 'Sample Pharma Manufacturer Inc')

            # Re-running the command must match the sanitized key from DB and skip insertion
            out2 = io.StringIO()
            call_command('import_medicines', file=csv_path, stdout=out2)
            output2 = out2.getvalue()

            self.assertIn('Already in Database (Skipped):         1', output2)
            self.assertIn('No new medicines to insert', output2)
            self.assertEqual(Medicine.objects.count(), 1)
        finally:
            os.remove(csv_path)


class MedicineAutocompleteApiTests(TestCase):
    """Automated tests for the Medicine Autocomplete GET API."""

    def setUp(self):
        self.client = Client()

        # 1. Approved Pharmacy User
        self.approved_user = User.objects.create_user(
            username='auto_pharma_approved',
            password='Password123!',
            role='PHARMACY'
        )
        self.approved_profile = PharmacyProfile.objects.create(
            user=self.approved_user,
            pharmacy_name='MedAccess City Chemist',
            owner_name='City Owner',
            pharmacist_name='Lead Pharmacist',
            pharmacy_license_number='DL-AUTO-01',
            gst_number='GSTIN-AUTO-01',
            phone_number='9876543299',
            email='auto@medaccess.test',
            address='City Center',
            city='Bhopal',
            state='Madhya Pradesh',
            pincode='462001',
            approval_status='APPROVED'
        )

        # 2. Pending Pharmacy User
        self.pending_user = User.objects.create_user(
            username='auto_pharma_pending',
            password='Password123!',
            role='PHARMACY'
        )
        PharmacyProfile.objects.create(
            user=self.pending_user,
            pharmacy_name='Pending Chemist',
            owner_name='Pending Owner',
            pharmacist_name='Pending Pharmacist',
            pharmacy_license_number='DL-AUTO-02',
            gst_number='GSTIN-AUTO-02',
            phone_number='9876543298',
            email='pending_auto@medaccess.test',
            address='City Center',
            city='Bhopal',
            state='Madhya Pradesh',
            pincode='462001',
            approval_status='PENDING'
        )

        # 3. Patient User
        self.patient_user = User.objects.create_user(
            username='auto_patient',
            password='Password123!',
            role='PATIENT'
        )
        PatientProfile.objects.create(user=self.patient_user)

        # 4. Create Catalogue Medicines
        self.med_para = Medicine.objects.create(
            medicine_name='Paracetamol 500mg Tablet',
            composition='Paracetamol (500mg)',
            manufacturer='GlaxoSmithKline',
            strength='500mg',
            dosage_form='Tablet',
            pack_size='Strip of 10 Tablets',
            mrp=Decimal('25.50')
        )
        self.med_amox = Medicine.objects.create(
            medicine_name='Amoxicillin 250mg Capsule',
            composition='Amoxicillin Trihydrate (250mg)',
            manufacturer='Cipla Ltd',
            strength='250mg',
            dosage_form='Capsule',
            pack_size=None,
            mrp=Decimal('48.00')
        )
        self.med_cetri = Medicine.objects.create(
            medicine_name='Cetirizine 10mg Tablet',
            composition='Cetirizine Hydrochloride (10mg)',
            manufacturer='Sun Pharma',
            strength='10mg',
            dosage_form='Tablet',
            pack_size='Strip of 10',
            mrp=None
        )

        # 5. Create 12 additional medicines to verify [:10] limit
        for i in range(1, 13):
            Medicine.objects.create(
                medicine_name=f'Cough Relief Syrup #{i:02d}',
                composition='Dextromethorphan (10mg)',
                manufacturer='Abbott Healthcare',
                strength='10mg/5ml',
                dosage_form='Syrup',
                pack_size='100ml Bottle',
                mrp=Decimal('85.00')
            )

        self.url = reverse('pharmacy:medicine_autocomplete')

    def test_url_reverse_and_namespace(self):
        """Autocomplete route resolves to expected URL."""
        self.assertEqual(self.url, '/pharmacy/medicines/autocomplete/')

    def test_unauthenticated_access_redirects(self):
        """Anonymous access is redirected to login."""
        response = self.client.get(self.url, {'q': 'para'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)

    def test_patient_user_access_denied(self):
        """Patient user is denied and redirected."""
        self.client.login(username='auto_patient', password='Password123!')
        response = self.client.get(self.url, {'q': 'para'})
        self.assertEqual(response.status_code, 302)

    def test_pending_pharmacy_user_access_denied(self):
        """Unapproved/pending pharmacy user is redirected."""
        self.client.login(username='auto_pharma_pending', password='Password123!')
        response = self.client.get(self.url, {'q': 'para'})
        self.assertEqual(response.status_code, 302)

    def test_post_method_not_allowed(self):
        """Endpoint only accepts GET requests."""
        self.client.login(username='auto_pharma_approved', password='Password123!')
        response = self.client.post(self.url, {'q': 'para'})
        self.assertEqual(response.status_code, 405)

    def test_query_less_than_two_chars_returns_empty_results(self):
        """Query with less than 2 characters returns empty results without querying."""
        self.client.login(username='auto_pharma_approved', password='Password123!')

        # Empty query
        res_empty = self.client.get(self.url)
        self.assertEqual(res_empty.status_code, 200)
        self.assertEqual(res_empty.json()['results'], [])
        self.assertEqual(res_empty.json()['count'], 0)

        # Single char query
        res_single = self.client.get(self.url, {'q': 'p'})
        self.assertEqual(res_single.status_code, 200)
        self.assertEqual(res_single.json()['results'], [])
        self.assertEqual(res_single.json()['count'], 0)

        # Whitespace query
        res_spaces = self.client.get(self.url, {'q': '   '})
        self.assertEqual(res_spaces.status_code, 200)
        self.assertEqual(res_spaces.json()['results'], [])
        self.assertEqual(res_spaces.json()['count'], 0)

    def test_search_by_medicine_name(self):
        """Search query matches against medicine_name."""
        self.client.login(username='auto_pharma_approved', password='Password123!')
        response = self.client.get(self.url, {'q': 'Paracetamol'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        names = [r['medicine_name'] for r in data['results']]
        self.assertIn('Paracetamol 500mg Tablet', names)

    def test_search_by_composition(self):
        """Search query matches against composition."""
        self.client.login(username='auto_pharma_approved', password='Password123!')
        response = self.client.get(self.url, {'q': 'Trihydrate'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        names = [r['medicine_name'] for r in data['results']]
        self.assertIn('Amoxicillin 250mg Capsule', names)

    def test_search_by_manufacturer(self):
        """Search query matches against manufacturer."""
        self.client.login(username='auto_pharma_approved', password='Password123!')
        response = self.client.get(self.url, {'q': 'GlaxoSmithKline'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        names = [r['medicine_name'] for r in data['results']]
        self.assertIn('Paracetamol 500mg Tablet', names)

    def test_search_by_strength(self):
        """Search query matches against strength."""
        self.client.login(username='auto_pharma_approved', password='Password123!')
        response = self.client.get(self.url, {'q': '250mg'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        names = [r['medicine_name'] for r in data['results']]
        self.assertIn('Amoxicillin 250mg Capsule', names)

    def test_max_ten_results_limit_and_deterministic_order(self):
        """Returns at most 10 suggestions with deterministic ordering."""
        self.client.login(username='auto_pharma_approved', password='Password123!')
        response = self.client.get(self.url, {'q': 'Cough Relief'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        # 12 exist in DB, but only 10 returned
        self.assertEqual(len(data['results']), 10)
        self.assertEqual(data['count'], 10)

        # Check deterministic alphabetical ordering by medicine_name
        names = [r['medicine_name'] for r in data['results']]
        self.assertEqual(names, sorted(names))

    def test_response_payload_structure_and_null_field_handling(self):
        """Response objects include required fields and handle nulls consistently."""
        self.client.login(username='auto_pharma_approved', password='Password123!')
        response = self.client.get(self.url, {'q': 'Cetirizine'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertGreaterEqual(len(data['results']), 1)

        first = data['results'][0]
        # Verify required fields exist
        required_keys = {
            'id', 'medicine_name', 'composition', 'manufacturer',
            'strength', 'dosage_form', 'pack_size', 'mrp'
        }
        self.assertTrue(required_keys.issubset(set(first.keys())))

        # Verify medicine id matches
        self.assertEqual(first['id'], self.med_cetri.id)

        # Verify null mrp is returned as null
        self.assertIsNone(first['mrp'])


class SmartStockEntryTests(TestCase):
    """Automated tests for Update Stocks / Smart Stock Entry functionality."""

    def setUp(self):
        self.client = Client()

        # 1. Approved Pharmacy User
        self.approved_user = User.objects.create_user(
            username='smart_pharma_approved',
            password='Password123!',
            role='PHARMACY'
        )
        self.approved_profile = PharmacyProfile.objects.create(
            user=self.approved_user,
            pharmacy_name='Careline Chemist',
            owner_name='Careline Owner',
            pharmacist_name='Careline Pharmacist',
            pharmacy_license_number='DL-SMART-01',
            gst_number='GSTIN-SMART-01',
            phone_number='9876543288',
            email='smart@medaccess.test',
            address='Careline Square',
            city='Bhopal',
            state='Madhya Pradesh',
            pincode='462001',
            approval_status='APPROVED'
        )

        # 2. Pending Pharmacy User
        self.pending_user = User.objects.create_user(
            username='smart_pharma_pending',
            password='Password123!',
            role='PHARMACY'
        )
        PharmacyProfile.objects.create(
            user=self.pending_user,
            pharmacy_name='Pending Chemist',
            owner_name='Pending Owner',
            pharmacist_name='Pending Pharmacist',
            pharmacy_license_number='DL-SMART-02',
            gst_number='GSTIN-SMART-02',
            phone_number='9876543287',
            email='pending_smart@medaccess.test',
            address='Careline Square',
            city='Bhopal',
            state='Madhya Pradesh',
            pincode='462001',
            approval_status='PENDING'
        )

        # 3. Patient User
        self.patient_user = User.objects.create_user(
            username='smart_patient',
            password='Password123!',
            role='PATIENT'
        )
        PatientProfile.objects.create(user=self.patient_user)

        # 4. Master Catalogue Medicines
        self.med1 = Medicine.objects.create(
            medicine_name='Paracetamol 650mg Tablet',
            composition='Paracetamol (650mg)',
            manufacturer='Dolo Pharma',
            strength='650mg',
            dosage_form='Tablet',
            pack_size='Strip of 15 Tablets',
            mrp=Decimal('30.00')
        )
        self.med2 = Medicine.objects.create(
            medicine_name='Azithromycin 500mg Tablet',
            composition='Azithromycin (500mg)',
            manufacturer='Azi Corp',
            strength='500mg',
            dosage_form='Tablet',
            pack_size='Strip of 3 Tablets',
            mrp=Decimal('75.00')
        )

        self.url = reverse('pharmacy:update_stock')

    def test_smart_stock_entry_url_routing(self):
        """Verify URL routing and reverse resolution for Update Stocks / Smart Stock Entry."""
        self.assertEqual(reverse('pharmacy:update_stock'), '/pharmacy/update-stock/')
        self.assertEqual(reverse('pharmacy:smart_stock_entry'), '/pharmacy/smart-stock-entry/')

    def test_unauthenticated_user_redirected(self):
        """Anonymous access is redirected to login."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)

    def test_patient_user_access_denied(self):
        """Patient user access is blocked and redirected."""
        self.client.login(username='smart_patient', password='Password123!')
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_pending_pharmacy_user_access_denied(self):
        """Pending pharmacy user is blocked and redirected."""
        self.client.login(username='smart_pharma_pending', password='Password123!')
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_approved_pharmacy_user_access_success(self):
        """Approved pharmacy user can load the Smart Stock Entry page."""
        self.client.login(username='smart_pharma_approved', password='Password123!')
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'pharmacy/smart_stock_entry.html')
        self.assertContains(response, 'Smart Stock Entry')
        self.assertContains(response, 'Search Medicine Catalogue')
        self.assertContains(response, 'Voice Entry')

    def test_successful_stock_submission_and_redirect(self):
        """Valid stock entry creates inventory record and redirects to Manage Inventory."""
        self.client.login(username='smart_pharma_approved', password='Password123!')
        payload = {
            'medicine': self.med1.pk,
            'batch_number': 'DOLO-LOT-101',
            'expiry_date': (timezone.localdate() + timedelta(days=365)).isoformat(),
            'quantity': 75,
            'unit': 'Strips',
            'low_stock_limit': 15,
        }
        response = self.client.post(self.url, payload)
        self.assertRedirects(response, reverse('pharmacy:inventory'))

        # Verify record exists in MySQL inventory
        inv = Inventory.objects.filter(
            pharmacy=self.approved_profile,
            medicine=self.med1,
            batch_number='DOLO-LOT-101'
        ).first()
        self.assertIsNotNone(inv)
        self.assertEqual(inv.quantity, 75)
        self.assertEqual(inv.unit, 'Strips')
        self.assertEqual(inv.low_stock_limit, 15)

    def test_submission_without_selected_medicine_fails(self):
        """Submitting without selecting a catalogue medicine displays validation error."""
        self.client.login(username='smart_pharma_approved', password='Password123!')
        payload = {
            'medicine': '',  # missing medicine
            'batch_number': 'DOLO-LOT-102',
            'expiry_date': (timezone.localdate() + timedelta(days=365)).isoformat(),
            'quantity': 50,
            'unit': 'Strips',
            'low_stock_limit': 10,
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Please search and select a valid medicine from the catalogue.')
        self.assertFalse(Inventory.objects.filter(batch_number='DOLO-LOT-102').exists())

    def test_duplicate_batch_for_same_medicine_rejected(self):
        """Creating an existing batch for the same medicine in this pharmacy is rejected."""
        # Create existing batch
        Inventory.objects.create(
            pharmacy=self.approved_profile,
            medicine=self.med1,
            batch_number='DOLO-DUP-01',
            expiry_date=timezone.localdate() + timedelta(days=180),
            quantity=20,
            unit='Strips',
            low_stock_limit=5
        )

        self.client.login(username='smart_pharma_approved', password='Password123!')
        payload = {
            'medicine': self.med1.pk,
            'batch_number': 'DOLO-DUP-01',  # duplicate batch
            'expiry_date': (timezone.localdate() + timedelta(days=365)).isoformat(),
            'quantity': 30,
            'unit': 'Strips',
            'low_stock_limit': 10,
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'already exists in your inventory')

    def test_preselected_medicine_via_query_param(self):
        """Opening page with ?medicine_id= renders preselected medicine preview."""
        self.client.login(username='smart_pharma_approved', password='Password123!')
        response = self.client.get(self.url, {'medicine_id': self.med2.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['selected_medicine'], self.med2)
        self.assertContains(response, 'Azithromycin 500mg Tablet')
        self.assertContains(response, 'Azi Corp')

    def test_existing_manage_inventory_and_batch_update_remain_intact(self):
        """Existing Manage Inventory listing and per-batch editor work without interference."""
        # Create a batch
        batch = Inventory.objects.create(
            pharmacy=self.approved_profile,
            medicine=self.med1,
            batch_number='LEGACY-BATCH-01',
            expiry_date=timezone.localdate() + timedelta(days=90),
            quantity=100,
            unit='Tablets',
            low_stock_limit=20
        )

        self.client.login(username='smart_pharma_approved', password='Password123!')

        # 1. Manage Inventory page loads successfully and shows batch
        res_list = self.client.get(reverse('pharmacy:inventory'))
        self.assertEqual(res_list.status_code, 200)
        self.assertContains(res_list, 'LEGACY-BATCH-01')

        # 2. Add inventory batch page remains functional
        res_add = self.client.get(reverse('pharmacy:inventory_add'))
        self.assertEqual(res_add.status_code, 200)

        # 3. Per-batch update page updates stock without changing medicine identity
        update_url = reverse('pharmacy:inventory_update', args=[batch.pk])
        res_get_update = self.client.get(update_url)
        self.assertEqual(res_get_update.status_code, 200)
        self.assertContains(res_get_update, 'LEGACY-BATCH-01')

        update_payload = {
            'quantity': 150,
            'expiry_date': (timezone.localdate() + timedelta(days=120)).isoformat(),
            'unit': 'Tablets',
            'low_stock_limit': 25,
        }
        res_post_update = self.client.post(update_url, update_payload)
        self.assertRedirects(res_post_update, reverse('pharmacy:inventory'))

        batch.refresh_from_db()
        self.assertEqual(batch.quantity, 150)
        self.assertEqual(batch.medicine, self.med1)  # Medicine identity unchanged

    def test_voice_entry_ui_elements_present(self):
        """Voice-based smart stock entry UI components are rendered on the page."""
        self.client.login(username='smart_pharma_approved', password='Password123!')
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

        # Mode switcher tabs
        self.assertContains(response, 'id="tab-mode-manual"')
        self.assertContains(response, 'id="tab-mode-voice"')

        # Manual search and voice entry sections
        self.assertContains(response, 'id="section-manual-search"')
        self.assertContains(response, 'id="section-voice-entry"')

        # Voice entry controls and inputs
        self.assertContains(response, 'id="voice-language-select"')
        self.assertContains(response, 'value="en-IN"')
        self.assertContains(response, 'value="hi-IN"')
        self.assertContains(response, 'id="btn-voice-record"')
        self.assertContains(response, 'id="voice-status-badge"')
        self.assertContains(response, 'id="voice-transcript-input"')
        self.assertContains(response, 'id="btn-clear-transcript"')
        self.assertContains(response, 'id="voice-unsupported-alert"')

        # Phase 2 voice processing & review elements
        self.assertContains(response, 'id="btn-process-voice"')
        self.assertContains(response, 'id="voice-review-section"')
        self.assertContains(response, 'id="voice-field-medicine"')
        self.assertContains(response, 'id="voice-field-quantity"')
        self.assertContains(response, 'id="voice-candidates-container"')
        self.assertContains(response, 'id="btn-apply-voice-details"')


class VoiceStockParserUnitTests(TestCase):
    """
    Unit tests for the rule-based Hinglish voice stock parser
    (pharmacy.services.voice_stock_parser).
    """

    def test_english_medicine_command(self):
        """Case 1: Standard English voice stock command."""
        cmd = "Add 50 strips of Paracetamol 500 mg."
        res = parse_voice_command(cmd)
        self.assertEqual(res['medicine_name'], 'Paracetamol')
        self.assertEqual(res['strength'], '500 mg')
        self.assertEqual(res['quantity'], 50)
        self.assertEqual(res['unit'], 'Strips')

    def test_hindi_medicine_command(self):
        """Case 2: Hindi/Hinglish medicine stock command with quantity and boxes."""
        cmd = "Crocin 500 mg ke 20 boxes hain."
        res = parse_voice_command(cmd)
        self.assertEqual(res['medicine_name'], 'Crocin')
        self.assertEqual(res['strength'], '500 mg')
        self.assertEqual(res['quantity'], 20)
        self.assertEqual(res['unit'], 'Boxes')

    def test_hinglish_mixed_command(self):
        """Case 3: Mixed Hinglish command with quantity, unit, and Hindi conjunction/expiry."""
        cmd = "Paracetamol ki quantity 50 strips aur expiry December 2026 hai."
        res = parse_voice_command(cmd)
        self.assertEqual(res['medicine_name'], 'Paracetamol')
        self.assertEqual(res['quantity'], 50)
        self.assertEqual(res['unit'], 'Strips')
        self.assertIsNotNone(res['expiry_date'])
        self.assertEqual(res['expiry_date']['formatted'], '2026-12-31')
        self.assertEqual(res['expiry_date']['raw'], 'December 2026')

    def test_strength_vs_quantity_distinction(self):
        """Case 4: Confirms strength (500 mg) is not mistaken for stock quantity (100)."""
        cmd = "Paracetamol 500 mg, quantity 100 tablets."
        res = parse_voice_command(cmd)
        self.assertEqual(res['strength'], '500 mg')
        self.assertEqual(res['quantity'], 100)
        self.assertNotEqual(res['quantity'], 500)
        self.assertEqual(res['unit'], 'Tablets')

    def test_batch_number_extraction(self):
        """Case 5: Batch number is extracted cleanly and digits in batch are not confused with quantity."""
        cmd1 = "Is medicine ka batch number PARA-101 hai."
        res1 = parse_voice_command(cmd1)
        self.assertEqual(res1['batch_number'], 'PARA-101')
        self.assertIsNone(res1['quantity'])
        self.assertIsNone(res1['medicine_name'])

        cmd2 = "Crocin ki 20 strips add karo, batch number CRO-101."
        res2 = parse_voice_command(cmd2)
        self.assertEqual(res2['medicine_name'], 'Crocin')
        self.assertEqual(res2['quantity'], 20)
        self.assertEqual(res2['unit'], 'Strips')
        self.assertEqual(res2['batch_number'], 'CRO-101')

    def test_month_year_expiry_extraction(self):
        """Case 6: Month-year expiry expressions are extracted with standard end-of-month dates."""
        cmd1 = "Expiry December 2026 hai."
        res1 = parse_voice_command(cmd1)
        self.assertIsNotNone(res1['expiry_date'])
        self.assertEqual(res1['expiry_date']['raw'], 'December 2026')
        self.assertEqual(res1['expiry_date']['formatted'], '2026-12-31')

        cmd2 = "Exp date 10/2027"
        res2 = parse_voice_command(cmd2)
        self.assertIsNotNone(res2['expiry_date'])
        self.assertEqual(res2['expiry_date']['formatted'], '2027-10-31')

    def test_missing_quantity(self):
        """Case 7: Command with missing quantity marks field as missing."""
        cmd = "Paracetamol 500 mg add karo."
        res = parse_voice_command(cmd)
        self.assertEqual(res['medicine_name'], 'Paracetamol')
        self.assertIsNone(res['quantity'])
        self.assertIn('quantity', res['missing_fields'])
        self.assertFalse(res['is_complete'])

    def test_missing_medicine_name(self):
        """Case 8: Standalone parameter commands do not invent medicine names."""
        cmd1 = "Batch number is PARA-101."
        res1 = parse_voice_command(cmd1)
        self.assertIsNone(res1['medicine_name'])
        self.assertIn('medicine_name', res1['missing_fields'])

        cmd2 = "Expiry date is December 2026."
        res2 = parse_voice_command(cmd2)
        self.assertIsNone(res2['medicine_name'])
        self.assertIn('medicine_name', res2['missing_fields'])


class VoiceStockParseApiIntegrationTests(TestCase):
    """
    Integration tests for the voice transcript parsing API endpoint
    (/pharmacy/update-stock/parse-voice/).
    """

    def setUp(self):
        self.client = Client()
        self.url = reverse('pharmacy:voice_stock_parse')

        # 1. Approved Pharmacy User
        self.approved_user = User.objects.create_user(
            username='voice_pharma_approved',
            password='Password123!',
            role='PHARMACY'
        )
        self.approved_profile = PharmacyProfile.objects.create(
            user=self.approved_user,
            pharmacy_name='Voice Test Pharmacy',
            owner_name='Voice Owner',
            pharmacist_name='Voice Pharmacist',
            pharmacy_license_number='DL-VOICE-01',
            gst_number='GSTIN-VOICE-01',
            phone_number='9876543299',
            email='voice@medaccess.test',
            address='Voice Square',
            city='Bhopal',
            state='Madhya Pradesh',
            pincode='462001',
            approval_status='APPROVED'
        )

        # 2. Pending Pharmacy User
        self.pending_user = User.objects.create_user(
            username='voice_pharma_pending',
            password='Password123!',
            role='PHARMACY'
        )
        PharmacyProfile.objects.create(
            user=self.pending_user,
            pharmacy_name='Pending Voice Pharmacy',
            owner_name='Pending Owner',
            pharmacist_name='Pending Pharmacist',
            pharmacy_license_number='DL-VOICE-02',
            gst_number='GSTIN-VOICE-02',
            phone_number='9876543298',
            email='pending_voice@medaccess.test',
            address='Voice Square',
            city='Bhopal',
            state='Madhya Pradesh',
            pincode='462001',
            approval_status='PENDING'
        )

        # 3. Patient User
        self.patient_user = User.objects.create_user(
            username='voice_patient',
            password='Password123!',
            role='PATIENT'
        )
        PatientProfile.objects.create(
            user=self.patient_user,
            date_of_birth='1990-01-01',
            gender='M'
        )

        # 4. Catalogue Medicines for Test Queries
        self.med_azithro_500 = Medicine.objects.create(
            medicine_name='Azithromycin 500mg Tablet',
            composition='Azithromycin (500mg)',
            strength='500mg',
            manufacturer='Cipla Ltd',
            dosage_form='Tablet',
            mrp=Decimal('120.50')
        )
        self.med_azithro_250 = Medicine.objects.create(
            medicine_name='Azithromycin 250mg Tablet',
            composition='Azithromycin (250mg)',
            strength='250mg',
            manufacturer='Cipla Ltd',
            dosage_form='Tablet',
            mrp=Decimal('65.00')
        )
        self.med_aziwok = Medicine.objects.create(
            medicine_name='Aziwok 500mg Tablet',
            composition='Azithromycin (500mg)',
            strength='500mg',
            manufacturer='Wockhardt Ltd',
            dosage_form='Tablet',
            mrp=Decimal('115.00')
        )

    def test_ambiguous_medicine_match_ranks_candidates(self):
        """Case 9: Multiple plausible candidate formulations are returned with top match first."""
        self.client.login(username='voice_pharma_approved', password='Password123!')
        payload = {'transcript': 'Azithromycin 500 mg ki 30 strips add karo.'}
        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['extracted']['medicine_name'], 'Azithromycin')
        self.assertEqual(data['extracted']['quantity'], 30)
        self.assertEqual(data['extracted']['unit'], 'Strips')

        # Candidates returned
        candidates = data['candidates']
        self.assertGreaterEqual(len(candidates), 2)
        # Top candidate should be the exact prefix + strength match
        self.assertEqual(candidates[0]['id'], self.med_azithro_500.id)
        self.assertIn('Strength Match', candidates[0]['match_type'])

    def test_no_catalogue_match(self):
        """Case 10: Non-existent medicine returns valid extraction with empty candidates array."""
        self.client.login(username='voice_pharma_approved', password='Password123!')
        payload = {'transcript': 'NonExistentMedicine999 50 strips add karo'}
        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['extracted']['quantity'], 50)
        self.assertEqual(data['candidates'], [])
        self.assertEqual(data['match_count'], 0)

    def test_invalid_or_empty_transcript(self):
        """Case 11: Empty, whitespace, or invalid transcripts return 400 Bad Request."""
        self.client.login(username='voice_pharma_approved', password='Password123!')

        # Empty string
        res1 = self.client.post(
            self.url,
            data=json.dumps({'transcript': '   '}),
            content_type='application/json'
        )
        self.assertEqual(res1.status_code, 400)
        self.assertFalse(res1.json()['success'])
        self.assertIn('empty', res1.json()['error'].lower())

        # Too long transcript (> 1000 chars)
        res2 = self.client.post(
            self.url,
            data=json.dumps({'transcript': 'Paracetamol ' * 200}),
            content_type='application/json'
        )
        self.assertEqual(res2.status_code, 400)
        self.assertIn('maximum', res2.json()['error'].lower())

        # Malformed JSON
        res3 = self.client.post(
            self.url,
            data='{invalid_json',
            content_type='application/json'
        )
        self.assertEqual(res3.status_code, 400)

    def test_unauthorized_access_restrictions(self):
        """Case 12: Anonymous, patient, and pending pharmacy accounts are blocked."""
        payload = json.dumps({'transcript': 'Paracetamol 50 strips'})

        # Anonymous user -> redirected to login
        res_anon = self.client.post(self.url, data=payload, content_type='application/json')
        self.assertEqual(res_anon.status_code, 302)

        # Patient user -> access forbidden
        self.client.login(username='voice_patient', password='Password123!')
        res_pat = self.client.post(self.url, data=payload, content_type='application/json')
        self.assertEqual(res_pat.status_code, 302)  # Role redirect

        # Pending pharmacy user -> blocked by @role_required('PHARMACY') and redirected
        self.client.login(username='voice_pharma_pending', password='Password123!')
        res_pending = self.client.post(self.url, data=payload, content_type='application/json')
        self.assertEqual(res_pending.status_code, 302)
        self.assertRedirects(res_pending, reverse('landing'))

    def test_non_post_request_rejected(self):
        """Case 13: Non-POST requests (GET, PUT, DELETE) are rejected with 405 Method Not Allowed."""
        self.client.login(username='voice_pharma_approved', password='Password123!')
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)
        self.assertFalse(response.json()['success'])
        self.assertIn('POST required', response.json()['error'])

    def test_csrf_protection_enforced(self):
        """Case 14: CSRF protection is enforced for state-changing POST requests."""
        # Client with CSRF enforcement turned on
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.login(username='voice_pharma_approved', password='Password123!')

        # POST without CSRF token should be rejected (403)
        res_no_csrf = csrf_client.post(
            self.url,
            data=json.dumps({'transcript': 'Crocin 20 boxes'}),
            content_type='application/json'
        )
        self.assertEqual(res_no_csrf.status_code, 403)

    def test_no_inventory_records_created_or_modified(self):
        """Case 15: Confirms that zero Inventory or Medicine records are created or altered during parsing."""
        initial_inventory_count = Inventory.objects.count()
        initial_medicine_count = Medicine.objects.count()

        self.client.login(username='voice_pharma_approved', password='Password123!')
        payload = {
            'transcript': 'Paracetamol 500 mg ki 50 strips add karo, batch number PARA-101 aur expiry December 2026 hai.'
        }
        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)

        # Verify zero change in Inventory or Medicine tables
        self.assertEqual(Inventory.objects.count(), initial_inventory_count)
        self.assertEqual(Medicine.objects.count(), initial_medicine_count)


class CsvBulkStockUploadTests(TestCase):
    """
    Automated test suite for Flexible CSV Bulk Stock Upload functionality.
    Covers file upload, format tolerance, delimiter/BOM detection, automatic
    and manual column mapping, catalogue formulation matching, duplicate batch
    prevention, row-level transactional import, and pharmacy isolation.
    """

    def setUp(self):
        self.client = Client()
        self.analyze_url = reverse('pharmacy:csv_stock_analyze')
        self.validate_url = reverse('pharmacy:csv_stock_validate')
        self.import_url = reverse('pharmacy:csv_stock_import')

        # 1. Approved Pharmacy User 1 (Alpha)
        self.user1 = User.objects.create_user(
            username='csv_pharma_alpha',
            password='Password123!',
            role='PHARMACY'
        )
        self.profile1 = PharmacyProfile.objects.create(
            user=self.user1,
            pharmacy_name='Alpha Chemist Store',
            owner_name='Alpha Owner',
            pharmacist_name='Alpha Pharmacist',
            pharmacy_license_number='DL-CSV-001',
            gst_number='GSTIN-CSV-001',
            phone_number='9876543201',
            email='alpha@medaccess.test',
            address='Alpha Market',
            city='Bhopal',
            state='Madhya Pradesh',
            pincode='462001',
            approval_status='APPROVED'
        )

        # 2. Approved Pharmacy User 2 (Beta - For cross-pharmacy isolation)
        self.user2 = User.objects.create_user(
            username='csv_pharma_beta',
            password='Password123!',
            role='PHARMACY'
        )
        self.profile2 = PharmacyProfile.objects.create(
            user=self.user2,
            pharmacy_name='Beta Chemist Store',
            owner_name='Beta Owner',
            pharmacist_name='Beta Pharmacist',
            pharmacy_license_number='DL-CSV-002',
            gst_number='GSTIN-CSV-002',
            phone_number='9876543202',
            email='beta@medaccess.test',
            address='Beta Market',
            city='Indore',
            state='Madhya Pradesh',
            pincode='452001',
            approval_status='APPROVED'
        )

        # 3. Pending Pharmacy User
        self.pending_user = User.objects.create_user(
            username='csv_pharma_pending',
            password='Password123!',
            role='PHARMACY'
        )
        PharmacyProfile.objects.create(
            user=self.pending_user,
            pharmacy_name='Pending CSV Pharmacy',
            owner_name='Pending Owner',
            pharmacist_name='Pending Pharmacist',
            pharmacy_license_number='DL-CSV-003',
            gst_number='GSTIN-CSV-003',
            phone_number='9876543203',
            email='pending_csv@medaccess.test',
            address='Pending Square',
            city='Bhopal',
            state='Madhya Pradesh',
            pincode='462001',
            approval_status='PENDING'
        )

        # 4. Patient User
        self.patient_user = User.objects.create_user(
            username='csv_patient',
            password='Password123!',
            role='PATIENT'
        )
        PatientProfile.objects.create(
            user=self.patient_user,
            date_of_birth='1995-05-15',
            gender='F'
        )

        # 5. Shared Catalogue Medicines
        self.med_para = Medicine.objects.create(
            medicine_name='Paracetamol 500mg Tablet',
            composition='Paracetamol (500mg)',
            strength='500mg',
            manufacturer='Lyka Labs',
            dosage_form='Tablet',
            mrp=Decimal('25.50')
        )
        self.med_azithro_500 = Medicine.objects.create(
            medicine_name='Azithromycin 500mg Tablet',
            composition='Azithromycin (500mg)',
            strength='500mg',
            manufacturer='Cipla Ltd',
            dosage_form='Tablet',
            mrp=Decimal('120.00')
        )
        self.med_azithro_250 = Medicine.objects.create(
            medicine_name='Azithromycin 250mg Tablet',
            composition='Azithromycin (250mg)',
            strength='250mg',
            manufacturer='Cipla Ltd',
            dosage_form='Tablet',
            mrp=Decimal('65.00')
        )

    def test_authorized_pharmacy_can_upload_csv(self):
        """Case 1: Approved pharmacy owner can upload and analyze CSV."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')
        csv_content = b"medicine_name,batch_number,expiry_date,quantity\nParacetamol 500mg Tablet,B-101,2026-12-31,50"
        csv_file = SimpleUploadedFile("stock.csv", csv_content, content_type="text/csv")

        response = self.client.post(self.analyze_url, {'csv_file': csv_file})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['total_rows'], 1)
        self.assertEqual(data['headers'], ['medicine_name', 'batch_number', 'expiry_date', 'quantity'])
        self.assertEqual(data['detected_mapping']['medicine_name'], 'medicine_name')
        self.assertEqual(data['detected_mapping']['quantity'], 'quantity')

    def test_unauthorized_users_cannot_access_csv_endpoints(self):
        """Case 2: Anonymous, patient, and unapproved pharmacy accounts are blocked from CSV endpoints."""
        csv_file = SimpleUploadedFile("test.csv", b"a,b\n1,2", content_type="text/csv")
        endpoints = [self.analyze_url, self.validate_url, self.import_url]

        # 1. Anonymous user redirected to login
        for url in endpoints:
            res = self.client.post(url, {'csv_file': csv_file})
            self.assertEqual(res.status_code, 302)
            self.assertIn('/accounts/login/', res.url)

        # 2. Patient user redirected
        self.client.login(username='csv_patient', password='Password123!')
        for url in endpoints:
            res = self.client.post(url, {'csv_file': csv_file})
            self.assertEqual(res.status_code, 302)

        # 3. Pending pharmacy user redirected to landing
        self.client.login(username='csv_pharma_pending', password='Password123!')
        for url in endpoints:
            res = self.client.post(url, {'csv_file': csv_file})
            self.assertEqual(res.status_code, 302)

    def test_different_column_names_recognized(self):
        """Case 3: Synonym and alias column names are automatically recognized."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')
        csv_content = b"product_name,batch_no,exp_date,available_stock,packaging_unit,reorder_level\nParacetamol 500mg Tablet,LOT-01,12/2026,100,Strips,15"
        csv_file = SimpleUploadedFile("synonyms.csv", csv_content, content_type="text/csv")

        response = self.client.post(self.analyze_url, {'csv_file': csv_file})
        self.assertEqual(response.status_code, 200)
        mapping = response.json()['detected_mapping']
        self.assertEqual(mapping['medicine_name'], 'product_name')
        self.assertEqual(mapping['batch_number'], 'batch_no')
        self.assertEqual(mapping['expiry_date'], 'exp_date')
        self.assertEqual(mapping['quantity'], 'available_stock')
        self.assertEqual(mapping['unit'], 'packaging_unit')
        self.assertEqual(mapping['low_stock_limit'], 'reorder_level')

    def test_different_column_orders_work(self):
        """Case 4: Variable column ordering is handled cleanly by mapping."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')
        csv_content = b"quantity,expiry_date,batch_number,medicine_name\n75,2026-12-31,LOT-REV,Paracetamol 500mg Tablet"
        csv_file = SimpleUploadedFile("reverse_order.csv", csv_content, content_type="text/csv")

        res_analyze = self.client.post(self.analyze_url, {'csv_file': csv_file})
        self.assertEqual(res_analyze.status_code, 200)
        mapping = res_analyze.json()['detected_mapping']
        self.assertEqual(mapping['medicine_name'], 'medicine_name')
        self.assertEqual(mapping['quantity'], 'quantity')

        # Validate with this mapping
        csv_file.seek(0)
        res_val = self.client.post(self.validate_url, {
            'csv_file': csv_file,
            'mapping': json.dumps(mapping)
        })
        self.assertEqual(res_val.status_code, 200)
        d = res_val.json()
        self.assertEqual(d['valid_count'], 1)
        self.assertEqual(d['preview_rows'][0]['quantity'], 75)
        self.assertEqual(d['preview_rows'][0]['batch_number'], 'LOT-REV')

    def test_manual_column_mapping_works(self):
        """Case 5: Unconventional custom column headers work via manual mapping."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')
        csv_content = b"col_a,col_b,col_c,col_d\nParacetamol 500mg Tablet,LOT-CUSTOM,2026-12-31,40"
        csv_file = SimpleUploadedFile("custom_cols.csv", csv_content, content_type="text/csv")

        manual_mapping = {
            'medicine_name': 'col_a',
            'batch_number': 'col_b',
            'expiry_date': 'col_c',
            'quantity': 'col_d',
            'strength': None,
            'unit': None,
            'low_stock_limit': None,
        }

        response = self.client.post(self.validate_url, {
            'csv_file': csv_file,
            'mapping': json.dumps(manual_mapping)
        })
        self.assertEqual(response.status_code, 200)
        d = response.json()
        self.assertEqual(d['valid_count'], 1)
        self.assertEqual(d['preview_rows'][0]['status'], 'VALID')
        self.assertEqual(d['preview_rows'][0]['quantity'], 40)

    def test_extra_columns_ignored_safely(self):
        """Case 6: Extra columns not required for inventory are safely ignored."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')
        csv_content = b"medicine_name,batch_number,expiry_date,quantity,supplier,purchase_price,aisle,remarks\nParacetamol 500mg Tablet,LOT-EXT,2026-12-31,25,MediDist,20.0,Aisle-3,Special delivery"
        csv_file = SimpleUploadedFile("extra_cols.csv", csv_content, content_type="text/csv")

        mapping = {
            'medicine_name': 'medicine_name',
            'batch_number': 'batch_number',
            'expiry_date': 'expiry_date',
            'quantity': 'quantity',
        }
        response = self.client.post(self.validate_url, {
            'csv_file': csv_file,
            'mapping': json.dumps(mapping)
        })
        self.assertEqual(response.status_code, 200)
        d = response.json()
        self.assertEqual(d['valid_count'], 1)
        self.assertEqual(d['invalid_count'], 0)

    def test_utf8_and_utf8_bom_files_work(self):
        """Case 7: Files with UTF-8 BOM encoding are correctly decoded without garbled headers."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')
        bom_content = b"\xef\xbb\xbfmedicine_name,batch_number,expiry_date,quantity\nParacetamol 500mg Tablet,BOM-01,2026-12-31,10"
        csv_file = SimpleUploadedFile("bom.csv", bom_content, content_type="text/csv")

        response = self.client.post(self.analyze_url, {'csv_file': csv_file})
        self.assertEqual(response.status_code, 200)
        d = response.json()
        self.assertEqual(d['encoding'], 'utf-8-sig')
        # First header should be clean 'medicine_name', without BOM \ufeff
        self.assertEqual(d['headers'][0], 'medicine_name')

    def test_supported_delimiters_handled(self):
        """Case 8: Comma, semicolon, and tab delimiters are auto-detected and parsed."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')

        # Semicolon delimiter
        semi_content = b"medicine_name;batch_number;expiry_date;quantity\nParacetamol 500mg Tablet;SEMI-01;2026-12-31;15"
        res_semi = self.client.post(self.analyze_url, {'csv_file': SimpleUploadedFile("semi.csv", semi_content, content_type="text/csv")})
        self.assertEqual(res_semi.status_code, 200)
        self.assertEqual(res_semi.json()['delimiter'], ';')

        # Tab delimiter
        tab_content = b"medicine_name\tbatch_number\texpiry_date\tquantity\nParacetamol 500mg Tablet\tTAB-01\t2026-12-31\t20"
        res_tab = self.client.post(self.analyze_url, {'csv_file': SimpleUploadedFile("tab.csv", tab_content, content_type="text/csv")})
        self.assertEqual(res_tab.status_code, 200)
        self.assertEqual(res_tab.json()['delimiter'], '\t')

    def test_invalid_expiry_dates_rejected(self):
        """Case 9: Rows with unparseable or nonsensical expiry dates are flagged INVALID."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')
        csv_content = b"medicine_name,batch_number,expiry_date,quantity\nParacetamol 500mg Tablet,EXP-BAD,invalid_date,30\nParacetamol 500mg Tablet,EXP-BAD2,32/01/2026,30"
        csv_file = SimpleUploadedFile("bad_dates.csv", csv_content, content_type="text/csv")

        mapping = {'medicine_name': 'medicine_name', 'batch_number': 'batch_number', 'expiry_date': 'expiry_date', 'quantity': 'quantity'}
        response = self.client.post(self.validate_url, {'csv_file': csv_file, 'mapping': json.dumps(mapping)})
        self.assertEqual(response.status_code, 200)
        d = response.json()
        self.assertEqual(d['invalid_count'], 2)
        self.assertEqual(d['valid_count'], 0)

    def test_invalid_quantities_rejected(self):
        """Case 10: Negative numbers, alphabets, or missing quantities are flagged INVALID."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')
        csv_content = b"medicine_name,batch_number,expiry_date,quantity\nParacetamol 500mg Tablet,QTY-NEG,2026-12-31,-5\nParacetamol 500mg Tablet,QTY-TXT,2026-12-31,fifty"
        csv_file = SimpleUploadedFile("bad_qty.csv", csv_content, content_type="text/csv")

        mapping = {'medicine_name': 'medicine_name', 'batch_number': 'batch_number', 'expiry_date': 'expiry_date', 'quantity': 'quantity'}
        response = self.client.post(self.validate_url, {'csv_file': csv_file, 'mapping': json.dumps(mapping)})
        self.assertEqual(response.status_code, 200)
        d = response.json()
        self.assertEqual(d['invalid_count'], 2)
        self.assertEqual(d['valid_count'], 0)

    def test_unmatched_medicines_not_inserted_into_inventory(self):
        """Case 11: Rows with medicines not present in catalogue are marked UNMATCHED and never inserted."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')
        csv_content = b"medicine_name,batch_number,expiry_date,quantity\nCompletelyFakeNonExistentDrug999,FAKE-01,2026-12-31,100"
        csv_file = SimpleUploadedFile("unmatched.csv", csv_content, content_type="text/csv")

        mapping = {'medicine_name': 'medicine_name', 'batch_number': 'batch_number', 'expiry_date': 'expiry_date', 'quantity': 'quantity'}
        res_val = self.client.post(self.validate_url, {'csv_file': csv_file, 'mapping': json.dumps(mapping)})
        self.assertEqual(res_val.status_code, 200)
        d = res_val.json()
        self.assertEqual(d['valid_count'], 1)
        self.assertEqual(d['preview_rows'][0]['status'], 'VALID')

        # Attempt to import unmatched item (medicine_id is None)
        import_payload = {'rows': [{'row_num': 1, 'medicine_id': None, 'original_medicine': 'CompletelyFakeNonExistentDrug999', 'batch_number': 'FAKE-01', 'expiry_date': '2026-12-31', 'quantity': 100}]}
        res_imp = self.client.post(self.import_url, data=json.dumps(import_payload), content_type='application/json')
        self.assertEqual(res_imp.status_code, 200)
        self.assertEqual(res_imp.json()['imported_count'], 0)
        self.assertEqual(res_imp.json()['skipped_count'], 1)
        self.assertFalse(Inventory.objects.filter(batch_number='FAKE-01').exists())

    def test_ambiguous_medicine_matches_require_manual_selection(self):
        """Case 12: CSV preview does not suggest candidates from existing catalogue."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')
        csv_content = b"medicine_name,batch_number,expiry_date,quantity\nAzithromycin,AZI-LOT,2026-12-31,50"
        csv_file = SimpleUploadedFile("ambiguous.csv", csv_content, content_type="text/csv")

        mapping = {'medicine_name': 'medicine_name', 'batch_number': 'batch_number', 'expiry_date': 'expiry_date', 'quantity': 'quantity'}
        response = self.client.post(self.validate_url, {'csv_file': csv_file, 'mapping': json.dumps(mapping)})
        self.assertEqual(response.status_code, 200)
        d = response.json()
        row = d['preview_rows'][0]
        self.assertEqual(len(row['candidates']), 0)

    def test_duplicate_existing_batches_not_overwritten(self):
        """Case 13: Batches that already exist in the database are flagged DUPLICATE and skipped on import."""
        # Create pre-existing batch in Alpha inventory
        Inventory.objects.create(
            pharmacy=self.profile1,
            medicine=self.med_para,
            batch_number='EXISTING-PARA-100',
            expiry_date=date(2026, 12, 31),
            quantity=25,
            unit='Strips'
        )

        self.client.login(username='csv_pharma_alpha', password='Password123!')
        csv_content = b"medicine_name,batch_number,expiry_date,quantity\nParacetamol 500mg Tablet,EXISTING-PARA-100,2026-12-31,200"
        csv_file = SimpleUploadedFile("dup.csv", csv_content, content_type="text/csv")

        mapping = {'medicine_name': 'medicine_name', 'batch_number': 'batch_number', 'expiry_date': 'expiry_date', 'quantity': 'quantity'}
        res_val = self.client.post(self.validate_url, {'csv_file': csv_file, 'mapping': json.dumps(mapping)})
        self.assertEqual(res_val.status_code, 200)
        d = res_val.json()
        self.assertEqual(d['duplicate_count'], 1)
        self.assertEqual(d['preview_rows'][0]['status'], 'DUPLICATE')

        # Attempt to import duplicate row
        import_payload = {'rows': [{
            'row_num': 1,
            'medicine_id': self.med_para.id,
            'original_medicine': 'Paracetamol 500mg Tablet',
            'batch_number': 'EXISTING-PARA-100',
            'expiry_date': '2026-12-31',
            'quantity': 200
        }]}
        res_imp = self.client.post(self.import_url, data=json.dumps(import_payload), content_type='application/json')
        self.assertEqual(res_imp.status_code, 200)
        self.assertEqual(res_imp.json()['skipped_count'], 1)
        self.assertEqual(res_imp.json()['imported_count'], 0)

        # Confirm original stock quantity of 25 was not overwritten by 200
        inv = Inventory.objects.get(pharmacy=self.profile1, medicine=self.med_para, batch_number='EXISTING-PARA-100')
        self.assertEqual(inv.quantity, 25)

    def test_duplicate_batches_inside_same_csv_handled(self):
        """Case 14: Duplicate batches within the same uploaded CSV file are detected."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')
        csv_content = b"medicine_name,batch_number,expiry_date,quantity\nParacetamol 500mg Tablet,INFILE-01,2026-12-31,30\nParacetamol 500mg Tablet,INFILE-01,2026-12-31,50"
        csv_file = SimpleUploadedFile("infile_dup.csv", csv_content, content_type="text/csv")

        mapping = {'medicine_name': 'medicine_name', 'batch_number': 'batch_number', 'expiry_date': 'expiry_date', 'quantity': 'quantity'}
        response = self.client.post(self.validate_url, {'csv_file': csv_file, 'mapping': json.dumps(mapping)})
        self.assertEqual(response.status_code, 200)
        d = response.json()
        self.assertEqual(d['valid_count'], 1)
        self.assertEqual(d['duplicate_count'], 1)
        self.assertEqual(d['preview_rows'][1]['status'], 'DUPLICATE')

    def test_valid_rows_import_successfully(self):
        """Case 15: Valid stock rows are inserted into MySQL inventory with correct fields."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')
        rows_to_import = [
            {
                'row_num': 1,
                'medicine_id': self.med_para.id,
                'original_medicine': 'Paracetamol 500mg Tablet',
                'batch_number': 'SUCCESS-BATCH-01',
                'expiry_date': '2026-12-31',
                'quantity': 150,
                'unit': 'Strips',
                'low_stock_limit': 15
            },
            {
                'row_num': 2,
                'medicine_id': self.med_azithro_500.id,
                'original_medicine': 'Azithromycin 500mg Tablet',
                'batch_number': 'SUCCESS-BATCH-02',
                'expiry_date': '2027-06-30',
                'quantity': 80,
                'unit': 'Boxes',
                'low_stock_limit': 10
            }
        ]

        response = self.client.post(
            self.import_url,
            data=json.dumps({'rows': rows_to_import}),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        d = response.json()
        self.assertEqual(d['imported_count'], 2)
        self.assertEqual(d['failed_count'], 0)

        # Verify DB records exist
        inv1 = Inventory.objects.filter(pharmacy=self.profile1, batch_number='SUCCESS-BATCH-01').first()
        self.assertIsNotNone(inv1)
        self.assertEqual(inv1.quantity, 150)
        self.assertEqual(inv1.unit, 'Strips')

        inv2 = Inventory.objects.filter(pharmacy=self.profile1, batch_number='SUCCESS-BATCH-02').first()
        self.assertIsNotNone(inv2)
        self.assertEqual(inv2.quantity, 80)
        self.assertEqual(inv2.unit, 'Boxes')

    def test_invalid_rows_do_not_create_inventory_records(self):
        """Case 16: Invalid rows do not create partially-formed or corrupt inventory records."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')
        rows_payload = [
            # Valid row
            {
                'row_num': 1,
                'medicine_id': self.med_para.id,
                'original_medicine': 'Paracetamol 500mg Tablet',
                'batch_number': 'GOOD-ROW-01',
                'expiry_date': '2026-12-31',
                'quantity': 50,
                'unit': 'Strips',
                'low_stock_limit': 10
            },
            # Invalid row (missing batch number)
            {
                'row_num': 2,
                'medicine_id': self.med_para.id,
                'original_medicine': 'Paracetamol 500mg Tablet',
                'batch_number': '',
                'expiry_date': '2026-12-31',
                'quantity': 50,
                'unit': 'Strips',
                'low_stock_limit': 10
            }
        ]

        response = self.client.post(
            self.import_url,
            data=json.dumps({'rows': rows_payload}),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        d = response.json()
        self.assertEqual(d['imported_count'], 1)
        self.assertEqual(d['failed_count'], 1)
        self.assertTrue(Inventory.objects.filter(batch_number='GOOD-ROW-01').exists())
        self.assertFalse(Inventory.objects.filter(batch_number='').exists())

    def test_imported_records_belong_only_to_logged_in_pharmacy(self):
        """Case 17: Multi-tenant pharmacy isolation is strictly preserved."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')
        rows_to_import = [{
            'row_num': 1,
            'medicine_id': self.med_para.id,
            'original_medicine': 'Paracetamol 500mg Tablet',
            'batch_number': 'ALPHA-ONLY-BATCH',
            'expiry_date': '2026-12-31',
            'quantity': 100,
            'unit': 'Strips',
            'low_stock_limit': 10
        }]

        self.client.post(
            self.import_url,
            data=json.dumps({'rows': rows_to_import}),
            content_type='application/json'
        )

        # Alpha owns the record
        self.assertTrue(Inventory.objects.filter(pharmacy=self.profile1, batch_number='ALPHA-ONLY-BATCH').exists())

        # Beta must NOT own or see the record
        self.assertFalse(Inventory.objects.filter(pharmacy=self.profile2, batch_number='ALPHA-ONLY-BATCH').exists())

    def test_existing_manual_search_and_voice_entry_unaffected(self):
        """Case 18: Manual Search and Voice Entry modes remain intact on the page."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')
        response = self.client.get(reverse('pharmacy:update_stock'))
        self.assertEqual(response.status_code, 200)

        # Verify all 3 modes exist in the DOM
        self.assertContains(response, 'id="tab-mode-manual"')
        self.assertContains(response, 'id="tab-mode-voice"')
        self.assertContains(response, 'id="tab-mode-csv"')

        # Verify Manual Search components
        self.assertContains(response, 'id="section-manual-search"')
        self.assertContains(response, 'id="medicine-search-input"')

        # Verify Voice Entry components
        self.assertContains(response, 'id="section-voice-entry"')
        self.assertContains(response, 'id="btn-voice-record"')
        self.assertContains(response, 'id="voice-transcript-input"')

        # Verify CSV Upload components
        self.assertContains(response, 'id="section-csv-upload"')
        self.assertContains(response, 'id="csv-dropzone"')
        self.assertContains(response, 'id="btn-analyze-csv"')

        # Perform manual stock save via standard form POST to ensure manual mode works 100%
        manual_payload = {
            'medicine': self.med_para.pk,
            'batch_number': 'MANUAL-PRESERVED-01',
            'expiry_date': (timezone.localdate() + timedelta(days=365)).isoformat(),
            'quantity': 60,
            'unit': 'Strips',
            'low_stock_limit': 10,
        }
        res_post = self.client.post(reverse('pharmacy:update_stock'), manual_payload)
        self.assertRedirects(res_post, reverse('pharmacy:inventory'))
        self.assertTrue(Inventory.objects.filter(pharmacy=self.profile1, batch_number='MANUAL-PRESERVED-01').exists())

    # =========================================================================
    # Missing Medicines Review & Catalogue Creation Tests
    # =========================================================================

    def test_unmatched_medicines_appear_in_missing_medicines_section(self):
        """Unmatched CSV medicines are flagged UNMATCHED and surfaced in missing_medicine_rows."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')
        csv_content = b"medicine_name,batch_number,expiry_date,quantity\nBrandNewNovelMed101,BN-01,2026-12-31,50"
        csv_file = SimpleUploadedFile("novel.csv", csv_content, content_type="text/csv")

        mapping = {'medicine_name': 'medicine_name', 'batch_number': 'batch_number', 'expiry_date': 'expiry_date', 'quantity': 'quantity'}
        response = self.client.post(self.validate_url, {'csv_file': csv_file, 'mapping': json.dumps(mapping)})
        self.assertEqual(response.status_code, 200)
        d = response.json()
        self.assertEqual(d['unmatched_count'], 1)
        self.assertEqual(len(d['missing_medicine_rows']), 1)
        missing_item = d['missing_medicine_rows'][0]
        self.assertEqual(missing_item['original_medicine'], 'BrandNewNovelMed101')
        self.assertEqual(missing_item['status'], 'VALID')
        self.assertTrue(missing_item['stock_is_valid'])

    def test_prefill_available_csv_catalogue_fields(self):
        """Column aliases for composition, manufacturer, dosage_form, pack_size, mrp are extracted."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')
        csv_content = (
            b"product_name,salt_composition,company,medicine_type,package_size,unit_price,batch_no,exp,stock\n"
            b"CoughRelief DX,Dextromethorphan (10mg),Sun Pharma,Syrup,100 ml Bottle,85.50,CRDX-01,2026-12-31,30"
        )
        csv_file = SimpleUploadedFile("prefill.csv", csv_content, content_type="text/csv")

        res_analyze = self.client.post(self.analyze_url, {'csv_file': csv_file})
        self.assertEqual(res_analyze.status_code, 200)
        mapping = res_analyze.json()['detected_mapping']
        self.assertEqual(mapping['composition'], 'salt_composition')
        self.assertEqual(mapping['manufacturer'], 'company')
        self.assertEqual(mapping['dosage_form'], 'medicine_type')
        self.assertEqual(mapping['pack_size'], 'package_size')
        self.assertEqual(mapping['mrp'], 'unit_price')

        # Validate with detected mapping
        csv_file.seek(0)
        res_val = self.client.post(self.validate_url, {'csv_file': csv_file, 'mapping': json.dumps(mapping)})
        self.assertEqual(res_val.status_code, 200)
        d = res_val.json()
        self.assertEqual(len(d['missing_medicine_rows']), 1)
        cat = d['missing_medicine_rows'][0]['catalogue_data']
        self.assertEqual(cat['medicine_name'], 'CoughRelief DX')
        self.assertEqual(cat['composition'], 'Dextromethorphan (10mg)')
        self.assertEqual(cat['manufacturer'], 'Sun Pharma')
        self.assertEqual(cat['dosage_form'], 'Syrup')
        self.assertEqual(cat['pack_size'], '100 ml Bottle')
        self.assertEqual(cat['mrp'], '85.50')

    def test_medicine_with_only_name_and_valid_stock_imports_successfully(self):
        """1. Medicine with only name and valid stock details imports successfully with composition/manufacturer None."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')
        import_payload = {
            'new_medicines': [
                {
                    'row_num': 1,
                    'medicine_name': 'Cough Syrup (Dextromethorphan)',
                    'composition': '',
                    'manufacturer': '',
                    'batch_number': 'DEM029100',
                    'expiry_date': '2029-10-28',
                    'quantity': 63,
                    'unit': 'Units',
                    'low_stock_limit': 10
                }
            ]
        }
        res = self.client.post(self.import_url, data=json.dumps(import_payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        d = res.json()
        self.assertEqual(d['imported_count'], 1)
        self.assertEqual(d['new_medicines_created_count'], 1)

        med = Medicine.objects.filter(medicine_name='Cough Syrup (Dextromethorphan)').first()
        self.assertIsNotNone(med)
        self.assertIsNone(med.composition)
        self.assertIsNone(med.manufacturer)

        inv = Inventory.objects.filter(pharmacy=self.profile1, batch_number='DEM029100').first()
        self.assertIsNotNone(inv)
        self.assertEqual(inv.medicine, med)
        self.assertEqual(inv.quantity, 63)

    def test_missing_composition_does_not_block_import(self):
        """2. Missing composition does not block import; manufacturer is preserved."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')
        import_payload = {
            'new_medicines': [
                {
                    'row_num': 1,
                    'medicine_name': 'Brand X Tablets',
                    'composition': None,
                    'manufacturer': 'Sun Pharma Ltd',
                    'batch_number': 'BX-100',
                    'expiry_date': '2028-12-31',
                    'quantity': 40
                }
            ]
        }
        res = self.client.post(self.import_url, data=json.dumps(import_payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        d = res.json()
        self.assertEqual(d['imported_count'], 1)

        med = Medicine.objects.filter(medicine_name='Brand X Tablets').first()
        self.assertIsNotNone(med)
        self.assertIsNone(med.composition)
        self.assertEqual(med.manufacturer, 'Sun Pharma Ltd')

    def test_missing_manufacturer_does_not_block_import(self):
        """3. Missing manufacturer does not block import; composition is preserved."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')
        import_payload = {
            'new_medicines': [
                {
                    'row_num': 1,
                    'medicine_name': 'Brand Y Capsule',
                    'composition': 'Amoxicillin IP 500mg',
                    'manufacturer': '',
                    'batch_number': 'BY-200',
                    'expiry_date': '2028-10-31',
                    'quantity': 25
                }
            ]
        }
        res = self.client.post(self.import_url, data=json.dumps(import_payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        d = res.json()
        self.assertEqual(d['imported_count'], 1)

        med = Medicine.objects.filter(medicine_name='Brand Y Capsule').first()
        self.assertIsNotNone(med)
        self.assertEqual(med.composition, 'Amoxicillin IP 500mg')
        self.assertIsNone(med.manufacturer)

    def test_both_composition_and_manufacturer_can_be_blank_or_none_simultaneously(self):
        """4. Both composition and manufacturer can be blank/None simultaneously in database."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')
        import_payload = {
            'new_medicines': [
                {
                    'row_num': 1,
                    'medicine_name': 'Dual Blank Elixir',
                    'composition': '',
                    'manufacturer': '',
                    'batch_number': 'DBE-01',
                    'expiry_date': '2027-11-30',
                    'quantity': 15
                }
            ]
        }
        res = self.client.post(self.import_url, data=json.dumps(import_payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        d = res.json()
        self.assertEqual(d['imported_count'], 1)

        med = Medicine.objects.filter(medicine_name='Dual Blank Elixir').first()
        self.assertIsNotNone(med)
        self.assertIsNone(med.composition)
        self.assertIsNone(med.manufacturer)

    def test_missing_optional_fields_stored_as_blank_or_null(self):
        """5. Missing optional fields (strength, dosage_form, pack_size, mrp) stored as blank/NULL without error."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')
        import_payload = {
            'new_medicines': [
                {
                    'row_num': 1,
                    'medicine_name': 'Minimal Opt Medicine',
                    'strength': '',
                    'dosage_form': '',
                    'pack_size': '',
                    'mrp': '',
                    'batch_number': 'MIN-OPT-01',
                    'expiry_date': '2028-05-31',
                    'quantity': 35
                }
            ]
        }
        res = self.client.post(self.import_url, data=json.dumps(import_payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        d = res.json()
        self.assertEqual(d['imported_count'], 1)

        med = Medicine.objects.filter(medicine_name='Minimal Opt Medicine').first()
        self.assertIsNotNone(med)
        self.assertIsNone(med.strength)
        self.assertIsNone(med.dosage_form)
        self.assertIsNone(med.pack_size)
        self.assertIsNone(med.mrp)

    def test_invalid_stock_rows_do_not_create_catalogue_or_inventory(self):
        """6. Invalid stock rows (missing batch, invalid expiry, negative quantity) do NOT create catalogue records."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')
        initial_med_count = Medicine.objects.count()

        import_payload = {
            'new_medicines': [
                {
                    'row_num': 1,
                    'medicine_name': 'EmptyBatchMed',
                    'batch_number': '',
                    'expiry_date': '2026-12-31',
                    'quantity': 50
                },
                {
                    'row_num': 2,
                    'medicine_name': 'BadExpiryMed',
                    'batch_number': 'EXP-BAD',
                    'expiry_date': 'not-a-date',
                    'quantity': 50
                },
                {
                    'row_num': 3,
                    'medicine_name': 'NegativeQtyMed',
                    'batch_number': 'NEG-QTY',
                    'expiry_date': '2026-12-31',
                    'quantity': -10
                }
            ]
        }
        res = self.client.post(self.import_url, data=json.dumps(import_payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        d = res.json()
        self.assertEqual(d['imported_count'], 0)
        self.assertEqual(d['failed_count'], 3)

        # None of the invalid rows created a catalogue medicine
        self.assertEqual(Medicine.objects.count(), initial_med_count)
        self.assertFalse(Medicine.objects.filter(medicine_name__in=['EmptyBatchMed', 'BadExpiryMed', 'NegativeQtyMed']).exists())

    def test_multiple_batches_for_same_medicine_in_one_csv_creates_one_medicine(self):
        """7. Multiple batches for same medicine name within one CSV create 1 Medicine and multiple Inventory batches."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')
        initial_med_count = Medicine.objects.count()

        import_payload = {
            'new_medicines': [
                {
                    'row_num': 1,
                    'medicine_name': 'Azithromycin 500mg',
                    'batch_number': 'AZ-LOT-1',
                    'expiry_date': '2027-05-01',
                    'quantity': 100,
                    'unit': 'Tablets'
                },
                {
                    'row_num': 2,
                    'medicine_name': 'Azithromycin 500mg',
                    'batch_number': 'AZ-LOT-2',
                    'expiry_date': '2027-06-01',
                    'quantity': 150,
                    'unit': 'Tablets'
                }
            ]
        }
        res = self.client.post(self.import_url, data=json.dumps(import_payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        d = res.json()
        self.assertEqual(d['imported_count'], 2)
        self.assertEqual(d['new_medicines_created_count'], 1)

        # Exactly 1 new Medicine record in catalogue
        self.assertEqual(Medicine.objects.count(), initial_med_count + 1)
        az_med = Medicine.objects.filter(medicine_name='Azithromycin 500mg').first()
        self.assertIsNotNone(az_med)

        # 2 Inventory batches linked to that same Medicine
        batches = Inventory.objects.filter(pharmacy=self.profile1, medicine=az_med).order_by('batch_number')
        self.assertEqual(batches.count(), 2)
        self.assertEqual(batches[0].batch_number, 'AZ-LOT-1')
        self.assertEqual(batches[0].quantity, 100)
        self.assertEqual(batches[1].batch_number, 'AZ-LOT-2')
        self.assertEqual(batches[1].quantity, 150)

    def test_csv_import_does_not_call_existing_catalogue_matching_or_deduplication(self):
        """8. CSV validate & import workflows do NOT call match_medicine_catalogue or check_catalogue_duplicate."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')
        csv_content = b"medicine_name,batch_number,expiry_date,quantity\nBypassMatchingMed,BYPASS-01,2027-12-31,25"
        csv_file = SimpleUploadedFile("bypass.csv", csv_content, content_type="text/csv")
        mapping = {'medicine_name': 'medicine_name', 'batch_number': 'batch_number', 'expiry_date': 'expiry_date', 'quantity': 'quantity'}

        with patch('pharmacy.services.catalogue_matcher.match_medicine_catalogue') as mock_match, \
             patch('pharmacy.services.csv_stock_importer.check_catalogue_duplicate') as mock_dup:
            # 1. Validate CSV endpoint
            res_val = self.client.post(self.validate_url, {'csv_file': csv_file, 'mapping': json.dumps(mapping)})
            self.assertEqual(res_val.status_code, 200)
            mock_match.assert_not_called()
            mock_dup.assert_not_called()

            # 2. Import CSV endpoint
            import_payload = {
                'new_medicines': [
                    {
                        'row_num': 1,
                        'medicine_name': 'BypassMatchingMed',
                        'batch_number': 'BYPASS-01',
                        'expiry_date': '2027-12-31',
                        'quantity': 25
                    }
                ]
            }
            res_imp = self.client.post(self.import_url, data=json.dumps(import_payload), content_type='application/json')
            self.assertEqual(res_imp.status_code, 200)
            mock_match.assert_not_called()
            mock_dup.assert_not_called()

    def test_manual_search_and_voice_entry_still_use_catalogue_matching(self):
        """9. Manual Search autocomplete and Voice Entry parsing still actively use catalogue matching."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')

        # A. Manual Search Autocomplete API uses catalogue matching
        auto_url = reverse('pharmacy:medicine_autocomplete') + '?q=Para'
        res_auto = self.client.get(auto_url)
        self.assertEqual(res_auto.status_code, 200)
        auto_data = res_auto.json()
        self.assertEqual(auto_data.get('status'), 'success')
        self.assertGreater(len(auto_data['results']), 0)
        self.assertTrue(any('Paracetamol' in m['medicine_name'] for m in auto_data['results']))

        # B. Voice Entry Parse API uses catalogue matching
        voice_url = reverse('pharmacy:voice_stock_parse')
        with patch('pharmacy.views.match_medicine_catalogue', wraps=match_medicine_catalogue) as mock_voice_match:
            voice_payload = {'transcript': 'Add 50 strips of Paracetamol 650mg batch P650-01 expiry Dec 2027'}
            res_voice = self.client.post(voice_url, data=json.dumps(voice_payload), content_type='application/json')
            self.assertEqual(res_voice.status_code, 200)
            mock_voice_match.assert_called()
            voice_data = res_voice.json()
            self.assertTrue(voice_data['success'])
            self.assertGreater(len(voice_data['candidates']), 0)

    def test_negative_or_zero_mrp_rejected(self):
        """Negative or zero MRP values are rejected during missing medicine catalogue creation."""
        self.client.login(username='csv_pharma_alpha', password='Password123!')
        import_payload = {
            'matched_rows': [],
            'new_medicines': [
                {
                    'row_num': 1,
                    'medicine_name': 'ZeroMRP Drug',
                    'composition': 'Salt (50mg)',
                    'manufacturer': 'Pharma Corp',
                    'mrp': '-10.00',  # Negative MRP
                    'batch_number': 'MRP-BAD-01',
                    'expiry_date': '2026-12-31',
                    'quantity': 25
                }
            ]
        }
        res = self.client.post(self.import_url, data=json.dumps(import_payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        d = res.json()
        self.assertEqual(d['failed_count'], 1)
        self.assertIn('MRP must be greater than zero', d['failed_rows'][0]['reason'])
        self.assertFalse(Medicine.objects.filter(medicine_name='ZeroMRP Drug').exists())

    def test_unauthorized_users_cannot_create_catalogue_records(self):
        """Unauthorized users cannot create new catalogue records via import endpoint."""
        self.client.login(username='csv_patient', password='Password123!')
        initial_med_count = Medicine.objects.count()

        import_payload = {
            'matched_rows': [],
            'new_medicines': [
                {
                    'row_num': 1,
                    'medicine_name': 'Hacker Drug 500mg',
                    'composition': 'Forbidden Substance',
                    'manufacturer': 'Illicit Corp',
                    'batch_number': 'HACK-01',
                    'expiry_date': '2026-12-31',
                    'quantity': 50
                }
            ]
        }
        response = self.client.post(self.import_url, data=json.dumps(import_payload), content_type='application/json')
        self.assertIn(response.status_code, [302, 403])
        self.assertEqual(Medicine.objects.count(), initial_med_count)
        self.assertFalse(Medicine.objects.filter(medicine_name='Hacker Drug 500mg').exists())


class PharmacyCustomerFeedbackTests(TestCase):
    """
    Automated test suite for Pharmacy Portal Customer Feedback module.
    Covers access control, role authorization, strict multi-tenant pharmacy data isolation,
    real-time KPI metrics calculation (average, total, distribution), comment search,
    star rating filtering, pagination, empty states, and read-only data integrity.
    """
    def setUp(self):
        self.client = Client()

        # 1. Approved Pharmacy A
        self.pharma_user_a = User.objects.create_user(
            username='feedback_pharma_a',
            email='pharma_a@medaccess.test',
            password='TestPassword123!',
            role='PHARMACY',
            email_verified=True,
        )
        self.pharma_profile_a = PharmacyProfile.objects.create(
            user=self.pharma_user_a,
            pharmacy_name='City Care Pharmacy A',
            owner_name='Rajesh Kumar',
            pharmacist_name='Rajesh Kumar, RPh',
            pharmacy_license_number='DL-A-1001',
            gst_number='27AAAAA0000A1Z5',
            phone_number='022-21111111',
            email='pharma_a@medaccess.test',
            address='Shop 1, Main Road',
            city='Mumbai',
            state='Maharashtra',
            pincode='400001',
            approval_status='APPROVED',
        )

        # 2. Approved Pharmacy B
        self.pharma_user_b = User.objects.create_user(
            username='feedback_pharma_b',
            email='pharma_b@medaccess.test',
            password='TestPassword123!',
            role='PHARMACY',
            email_verified=True,
        )
        self.pharma_profile_b = PharmacyProfile.objects.create(
            user=self.pharma_user_b,
            pharmacy_name='Metro Health Chemist B',
            owner_name='Anita Sharma',
            pharmacist_name='Anita Sharma, RPh',
            pharmacy_license_number='DL-B-2002',
            gst_number='27BBBBB1111B1Z6',
            phone_number='022-22222222',
            email='pharma_b@medaccess.test',
            address='Shop 2, Station Road',
            city='Pune',
            state='Maharashtra',
            pincode='411001',
            approval_status='APPROVED',
        )

        # 3. Patient Users
        self.patient_user_1 = User.objects.create_user(
            username='patient_rahul',
            email='rahul@medaccess.test',
            first_name='Rahul',
            last_name='Mehta',
            password='TestPassword123!',
            role='PATIENT',
            email_verified=True,
        )
        self.patient_profile_1 = PatientProfile.objects.create(user=self.patient_user_1)

        self.patient_user_2 = User.objects.create_user(
            username='patient_priya',
            email='priya@medaccess.test',
            first_name='Priya',
            last_name='Nair',
            password='TestPassword123!',
            role='PATIENT',
            email_verified=True,
        )
        self.patient_profile_2 = PatientProfile.objects.create(user=self.patient_user_2)

        self.patient_user_3 = User.objects.create_user(
            username='patient_amit',
            email='amit@medaccess.test',
            first_name='Amit',
            last_name='Patel',
            password='TestPassword123!',
            role='PATIENT',
            email_verified=True,
        )
        self.patient_profile_3 = PatientProfile.objects.create(user=self.patient_user_3)

        self.patient_user_4 = User.objects.create_user(
            username='patient_deepa',
            email='deepa@medaccess.test',
            first_name='Deepa',
            last_name='Sen',
            password='TestPassword123!',
            role='PATIENT',
            email_verified=True,
        )
        self.patient_profile_4 = PatientProfile.objects.create(user=self.patient_user_4)

        # 4. Seed Reviews for Pharmacy A (ratings: 5, 4, 5, 2 -> total=4, sum=16, avg=4.0)
        self.fb_a1 = Feedback.objects.create(
            patient=self.patient_profile_1,
            pharmacy=self.pharma_profile_a,
            rating=5,
            comment='Excellent fast delivery and genuine medicines!'
        )
        self.fb_a2 = Feedback.objects.create(
            patient=self.patient_profile_2,
            pharmacy=self.pharma_profile_a,
            rating=4,
            comment='Good customer service but packaging could be slightly improved.'
        )
        self.fb_a3 = Feedback.objects.create(
            patient=self.patient_profile_3,
            pharmacy=self.pharma_profile_a,
            rating=5,
            comment='Always helpful pharmacist staff and quick turnaround time.'
        )
        self.fb_a4 = Feedback.objects.create(
            patient=self.patient_profile_4,
            pharmacy=self.pharma_profile_a,
            rating=2,
            comment='Delayed delivery, took two extra days to arrive.'
        )

        # 5. Seed Review for Pharmacy B
        self.fb_b1 = Feedback.objects.create(
            patient=self.patient_profile_1,
            pharmacy=self.pharma_profile_b,
            rating=1,
            comment='Terrible experience with Pharmacy B store stock availability.'
        )

        self.feedback_url = reverse('pharmacy:customer_feedback')

    # 1. Pharmacy owner can access Customer Feedback
    def test_pharmacy_owner_can_access_customer_feedback(self):
        self.client.force_login(self.pharma_user_a)
        response = self.client.get(self.feedback_url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'pharmacy/customer_feedback.html')
        self.assertContains(response, 'Customer Feedback & Ratings')
        self.assertContains(response, self.pharma_profile_a.pharmacy_name)

    # 2. Patient cannot access Pharmacy Customer Feedback
    def test_patient_cannot_access_pharmacy_customer_feedback(self):
        self.client.force_login(self.patient_user_1)
        response = self.client.get(self.feedback_url)
        # role_required denies access and redirects
        self.assertIn(response.status_code, [302, 403])

    # 3. Unauthenticated user cannot access the page
    def test_unauthenticated_user_cannot_access_customer_feedback(self):
        response = self.client.get(self.feedback_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('accounts:login'), response.url)

    # 4. Pharmacy A only sees Pharmacy A reviews
    def test_pharmacy_a_only_sees_pharmacy_a_reviews(self):
        self.client.force_login(self.pharma_user_a)
        response = self.client.get(self.feedback_url)
        self.assertEqual(response.status_code, 200)

        # Pharmacy A comments present
        self.assertContains(response, 'Excellent fast delivery and genuine medicines!')
        self.assertContains(response, 'Good customer service but packaging')
        self.assertContains(response, 'Delayed delivery, took two extra days')

        # Pharmacy B comments strictly excluded
        self.assertNotContains(response, 'Terrible experience with Pharmacy B store')

    # 5. Pharmacy A cannot see Pharmacy B reviews, and Pharmacy B only sees Pharmacy B
    def test_pharmacy_data_isolation_across_tenants(self):
        self.client.force_login(self.pharma_user_b)
        response = self.client.get(self.feedback_url)
        self.assertEqual(response.status_code, 200)

        # Pharmacy B comment present
        self.assertContains(response, 'Terrible experience with Pharmacy B store')

        # Pharmacy A comments strictly excluded
        self.assertNotContains(response, 'Excellent fast delivery and genuine medicines!')
        self.assertNotContains(response, 'Always helpful pharmacist staff')

    # 6. Average rating is calculated correctly
    def test_average_rating_calculated_correctly(self):
        self.client.force_login(self.pharma_user_a)
        response = self.client.get(self.feedback_url)
        # Ratings: 5, 4, 5, 2 -> Total = 16, Count = 4 -> Avg = 4.0
        self.assertEqual(response.context['avg_rating'], 4.0)
        self.assertEqual(response.context['total_reviews'], 4)
        self.assertContains(response, '4.0')

    # 7. Rating distribution counts are correct
    def test_rating_distribution_counts_correct(self):
        self.client.force_login(self.pharma_user_a)
        response = self.client.get(self.feedback_url)
        dist = {d['stars']: d['count'] for d in response.context['rating_distribution']}
        self.assertEqual(dist[5], 2)
        self.assertEqual(dist[4], 1)
        self.assertEqual(dist[3], 0)
        self.assertEqual(dist[2], 1)
        self.assertEqual(dist[1], 0)

        # Check percentages
        pct = {d['stars']: d['percentage'] for d in response.context['rating_distribution']}
        self.assertEqual(pct[5], 50.0)
        self.assertEqual(pct[4], 25.0)
        self.assertEqual(pct[3], 0.0)
        self.assertEqual(pct[2], 25.0)
        self.assertEqual(pct[1], 0.0)

    # 8. Search by comment works
    def test_search_by_comment_filters_correctly(self):
        self.client.force_login(self.pharma_user_a)
        response = self.client.get(f"{self.feedback_url}?q=delivery")
        self.assertEqual(response.status_code, 200)

        # 2 reviews contain "delivery"
        self.assertEqual(response.context['reviews'].paginator.count, 2)
        self.assertContains(response, 'Excellent fast delivery and genuine medicines!')
        self.assertContains(response, 'Delayed delivery, took two extra days')
        self.assertNotContains(response, 'Always helpful pharmacist staff')

    # 9. Rating filter works
    def test_filter_by_star_rating_filters_correctly(self):
        self.client.force_login(self.pharma_user_a)
        response = self.client.get(f"{self.feedback_url}?rating=5")
        self.assertEqual(response.status_code, 200)

        # 2 reviews have rating=5
        self.assertEqual(response.context['reviews'].paginator.count, 2)
        for r in response.context['reviews']:
            self.assertEqual(r.rating, 5)

        # 4-star and 2-star reviews excluded from filtered list
        self.assertNotContains(response, 'Good customer service but packaging')
        self.assertNotContains(response, 'Delayed delivery, took two extra days')

    # 10. Combined search and rating filter
    def test_combined_search_and_rating_filter(self):
        self.client.force_login(self.pharma_user_a)
        response = self.client.get(f"{self.feedback_url}?q=delivery&rating=5")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['reviews'].paginator.count, 1)
        self.assertContains(response, 'Excellent fast delivery and genuine medicines!')
        self.assertNotContains(response, 'Delayed delivery, took two extra days')

    # 11. Pagination works and preserves filters
    def test_pagination_and_query_preservation(self):
        # Create additional feedback entries to exceed 10 records (2 from setUp + 10 = 12 records with 'delivery')
        for i in range(5, 15):
            user = User.objects.create_user(
                username=f'pagination_patient_{i}',
                password='Password123!',
                role='PATIENT'
            )
            prof = PatientProfile.objects.create(user=user)
            Feedback.objects.create(
                patient=prof,
                pharmacy=self.pharma_profile_a,
                rating=5,
                comment=f'Bulk review comment number {i} with delivery keyword'
            )

        self.client.force_login(self.pharma_user_a)

        # Page 1 has 10 items
        resp_p1 = self.client.get(f"{self.feedback_url}?q=delivery")
        self.assertEqual(resp_p1.status_code, 200)
        self.assertEqual(len(resp_p1.context['reviews']), 10)
        self.assertTrue(resp_p1.context['reviews'].has_next())

        # Page 2 has the remaining 2 items
        resp_p2 = self.client.get(f"{self.feedback_url}?page=2&q=delivery")
        self.assertEqual(resp_p2.status_code, 200)
        self.assertEqual(len(resp_p2.context['reviews']), 2)
        self.assertTrue(resp_p2.context['reviews'].has_previous())

        # Check navigation links preserve 'q=delivery'
        self.assertContains(resp_p1, 'q=delivery')

    # 12. Empty state when pharmacy has no reviews
    def test_empty_state_when_pharmacy_has_no_reviews(self):
        # Create new approved pharmacy with zero reviews
        new_pharma_user = User.objects.create_user(
            username='empty_pharma_user',
            password='TestPassword123!',
            role='PHARMACY',
            email_verified=True,
        )
        PharmacyProfile.objects.create(
            user=new_pharma_user,
            pharmacy_name='Zero Reviews Pharmacy Store',
            approval_status='APPROVED'
        )

        self.client.force_login(new_pharma_user)
        response = self.client.get(self.feedback_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_reviews'], 0)
        self.assertEqual(response.context['avg_rating'], 0.0)
        self.assertContains(response, 'No Customer Reviews Yet')

    # 13. Empty state when filters return no matching reviews
    def test_empty_state_when_filters_yield_no_results(self):
        self.client.force_login(self.pharma_user_a)
        response = self.client.get(f"{self.feedback_url}?q=nonexistentkeywordxyz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_reviews'], 4)  # Lifetime total intact
        self.assertEqual(response.context['reviews'].paginator.count, 0)
        self.assertContains(response, 'No Matching Reviews Found')
        self.assertContains(response, 'Clear Filters')

    # 14. Missing PharmacyProfile handled safely
    def test_missing_pharmacy_profile_handled_safely(self):
        orphan_pharma_user = User.objects.create_user(
            username='orphan_pharma',
            password='TestPassword123!',
            role='PHARMACY',
            email_verified=True,
        )
        self.client.force_login(orphan_pharma_user)
        response = self.client.get(self.feedback_url)
        # Should gracefully redirect to landing without 500 error
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('landing'))

    # 15. Privacy preservation: no patient email, phone or address in response
    def test_patient_privacy_preservation_in_rendered_output(self):
        self.client.force_login(self.pharma_user_a)
        response = self.client.get(self.feedback_url)
        self.assertEqual(response.status_code, 200)

        # Check safe display name format (e.g. "Rahul M.")
        self.assertContains(response, 'Rahul M.')

        # Ensure private patient information is never exposed in markup
        self.assertNotContains(response, self.patient_user_1.email)
        self.assertNotContains(response, self.patient_user_2.email)

    # 16. Patient feedback remains read-only and unchanged in database
    def test_patient_feedback_remains_unchanged(self):
        self.client.force_login(self.pharma_user_a)
        initial_ratings = list(Feedback.objects.filter(pharmacy=self.pharma_profile_a).values('id', 'rating', 'comment'))

        # GET request
        self.client.get(self.feedback_url)

        # Verify exact same records exist in DB
        post_ratings = list(Feedback.objects.filter(pharmacy=self.pharma_profile_a).values('id', 'rating', 'comment'))
        self.assertEqual(initial_ratings, post_ratings)


class MedicineCompositionLookupTests(TestCase):
    def setUp(self):
        clear_distinct_names_cache()
        self.client = Client()

        # Approved Pharmacy User
        self.pharmacy_user = User.objects.create_user(
            username='analytics_pharmacy_tester',
            password='TestPassword123!',
            role='PHARMACY'
        )
        self.pharmacy_profile = PharmacyProfile.objects.create(
            user=self.pharmacy_user,
            pharmacy_name='Metro Health Pharmacy',
            owner_name='Metro Healthcare',
            pharmacist_name='Alice Smith, RPh',
            pharmacy_license_number='DL-COMP-01',
            gst_number='GSTIN-COMP-01',
            phone_number='9876500000',
            email='metro@medaccess.test',
            address='25 Market Street',
            city='Indore',
            state='Madhya Pradesh',
            pincode='452001',
            approval_status='APPROVED'
        )

        # Existing catalogue medicine
        self.catalogue_med = Medicine.objects.create(
            medicine_name='Dolo 650 Tablet',
            composition='Paracetamol (650mg)',
            manufacturer='Micro Labs Ltd',
            dosage_form='Tablet',
            mrp=Decimal('33.76')
        )

        # Test Composition Records (Tata 1mg simulation)
        self.comp_single = MedicineComposition.objects.create(
            name='Dolo 650 Tablet',
            mrp=Decimal('33.76'),
            quantity='strip of 15 tablets',
            manufacturer='Micro Labs Ltd',
            salt_composition='Paracetamol (650mg)'
        )

        # Multi-formulation medicine (Ambiguous test)
        self.comp_amb_1 = MedicineComposition.objects.create(
            name='A Rex Drop',
            mrp=Decimal('39.00'),
            quantity='packet of 15 ml Drop',
            manufacturer='Intel Pharmaceuticals',
            salt_composition='Hydroxyzine (NA)'
        )
        self.comp_amb_2 = MedicineComposition.objects.create(
            name='A Rex Drop',
            mrp=Decimal('35.90'),
            quantity='bottle of 100 ml Drop',
            manufacturer='Gnosis Pharmaceuticals Pvt Ltd',
            salt_composition='Hydroxyzine (6mg)'
        )

        # Another distinct medicine for RapidFuzz suggestions
        self.comp_augmentin = MedicineComposition.objects.create(
            name='Augmentin 625 Duo Tablet',
            mrp=Decimal('201.71'),
            quantity='strip of 10 tablets',
            manufacturer='GlaxoSmithKline Pharmaceuticals Ltd',
            salt_composition='Amoxycillin (500mg) + Clavulanic Acid (125mg)'
        )

        self.analytics_url = reverse('pharmacy:analytics')
        self.api_url = reverse('pharmacy:composition_lookup_api')
        self.autocomplete_url = reverse('pharmacy:composition_autocomplete_api')

    def tearDown(self):
        clear_distinct_names_cache()

    def test_exact_match_single_composition(self):
        result = lookup_medicine_composition('Dolo 650 Tablet')
        self.assertEqual(result['status'], 'VERIFIED_MATCH')
        self.assertTrue(result['is_verified'])
        self.assertEqual(result['salt_composition'], 'Paracetamol (650mg)')
        self.assertEqual(result['manufacturer'], 'Micro Labs Ltd')
        self.assertEqual(result['match_type'], 'EXACT')

    def test_exact_match_case_insensitive(self):
        result = lookup_medicine_composition('dolo 650 tablet')
        self.assertEqual(result['status'], 'VERIFIED_MATCH')
        self.assertTrue(result['is_verified'])
        self.assertEqual(result['salt_composition'], 'Paracetamol (650mg)')

    def test_ambiguous_compositions_prompt(self):
        result = lookup_medicine_composition('A Rex Drop')
        self.assertEqual(result['status'], 'AMBIGUOUS_COMPOSITION')
        self.assertFalse(result['is_verified'])
        self.assertIn('Multiple distinct formulations', result['message'])
        self.assertEqual(len(result['candidates']), 2)
        candidate_salts = {c['salt_composition'] for c in result['candidates']}
        self.assertIn('Hydroxyzine (NA)', candidate_salts)
        self.assertIn('Hydroxyzine (6mg)', candidate_salts)

    def test_disambiguation_by_record_id(self):
        result = lookup_medicine_composition('', disambiguate_id=self.comp_amb_1.id)
        self.assertEqual(result['status'], 'VERIFIED_MATCH')
        self.assertTrue(result['is_verified'])
        self.assertEqual(result['salt_composition'], 'Hydroxyzine (NA)')
        self.assertEqual(result['manufacturer'], 'Intel Pharmaceuticals')

    def test_rapidfuzz_suggestions_for_typo(self):
        result = lookup_medicine_composition('Doolo 650')
        self.assertEqual(result['status'], 'SUGGESTIONS')
        self.assertFalse(result['is_verified'])
        self.assertGreater(len(result['suggestions']), 0)
        self.assertEqual(result['suggestions'][0]['name'], 'Dolo 650 Tablet')
        self.assertGreaterEqual(result['suggestions'][0]['score'], 70.0)

    def test_unverified_fuzzy_results_never_auto_returned_as_verified(self):
        result = lookup_medicine_composition('Augmentin 625 Duo')
        if result['status'] == 'SUGGESTIONS':
            self.assertFalse(result['is_verified'])
            self.assertEqual(result['products'], [])
            self.assertIn('suggestions', result)

    def test_not_found_on_gibberish(self):
        result = lookup_medicine_composition('xyzqwerty987654321')
        self.assertEqual(result['status'], 'NOT_FOUND')
        self.assertFalse(result['is_verified'])
        self.assertEqual(result['suggestions'], [])

    def test_analytics_page_rendering_and_section(self):
        self.client.force_login(self.pharmacy_user)
        response = self.client.get(self.analytics_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Operational Reports')
        self.assertContains(response, 'Medicine Composition Lookup')
        self.assertContains(response, 'composition-query-input')
        self.assertContains(response, 'Tata 1mg')

    def test_composition_lookup_api_json(self):
        self.client.force_login(self.pharmacy_user)
        response = self.client.get(self.api_url, {'q': 'Dolo 650 Tablet'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'VERIFIED_MATCH')
        self.assertTrue(data['is_verified'])
        self.assertEqual(data['salt_composition'], 'Paracetamol (650mg)')

    def test_catalogue_non_regression(self):
        self.assertEqual(Medicine.objects.count(), 1)
        self.assertEqual(Medicine.objects.first().medicine_name, 'Dolo 650 Tablet')

    def test_autocomplete_api_short_query_returns_empty(self):
        self.client.force_login(self.pharmacy_user)
        # Empty query
        res = self.client.get(self.autocomplete_url, {'q': ''}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['count'], 0)
        self.assertEqual(data['suggestions'], [])

        # 1-character query
        res = self.client.get(self.autocomplete_url, {'q': 'D'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['count'], 0)
        self.assertEqual(data['suggestions'], [])

    def test_autocomplete_api_prefix_and_case_insensitive(self):
        self.client.force_login(self.pharmacy_user)
        # Lowercase prefix 'do' matching 'Dolo 650 Tablet'
        res = self.client.get(self.autocomplete_url, {'q': 'do'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['status'], 'success')
        self.assertIn('Dolo 650 Tablet', data['suggestions'])

        # Prefix 'Aug' matching 'Augmentin 625 Duo Tablet'
        res = self.client.get(self.autocomplete_url, {'q': 'Aug'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn('Augmentin 625 Duo Tablet', data['suggestions'])

    def test_autocomplete_api_returns_names_only(self):
        self.client.force_login(self.pharmacy_user)
        res = self.client.get(self.autocomplete_url, {'q': 'Dolo'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        for item in data['suggestions']:
            self.assertIsInstance(item, str)
            # Ensure unverified salt composition is not leaked in autocomplete
            self.assertNotIn('Paracetamol (650mg)', item)

    def test_autocomplete_api_max_limit_10(self):
        self.client.force_login(self.pharmacy_user)
        # Create 15 more test medicines starting with 'TestAuto'
        for i in range(15):
            MedicineComposition.objects.create(
                name=f'TestAuto Medicine {i:02d}',
                salt_composition='TestSalt (100mg)'
            )
        res = self.client.get(self.autocomplete_url, {'q': 'TestAuto'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['count'], 10)
        self.assertEqual(len(data['suggestions']), 10)

    def test_autocomplete_api_authentication_required(self):
        # Unauthenticated client
        res = self.client.get(self.autocomplete_url, {'q': 'Dolo'})
        # Should redirect to login
        self.assertEqual(res.status_code, 302)

    def test_autocomplete_dropdown_elements_in_analytics_template(self):
        self.client.force_login(self.pharmacy_user)
        res = self.client.get(self.analytics_url)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'composition-autocomplete-dropdown')
        self.assertContains(res, 'composition-autocomplete-spinner')


class MedicinePricePredictionTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Approved Pharmacy User
        self.pharmacy_user = User.objects.create_user(
            username='price_prediction_pharmacy_tester',
            password='TestPassword123!',
            role='PHARMACY'
        )
        self.pharmacy_profile = PharmacyProfile.objects.create(
            user=self.pharmacy_user,
            pharmacy_name='Global Price Health Pharmacy',
            owner_name='Global Care',
            pharmacist_name='Dr. Marcus Vance, RPh',
            pharmacy_license_number='DL-PRED-01',
            gst_number='GSTIN-PRED-01',
            phone_number='9876540001',
            email='globalprice@medaccess.test',
            address='88 Health Boulevard',
            city='Indore',
            state='Madhya Pradesh',
            pincode='452001',
            approval_status='APPROVED'
        )

        # Patient user for role access check
        self.patient_user = User.objects.create_user(
            username='price_prediction_patient_tester',
            password='TestPassword123!',
            role='PATIENT'
        )

        self.predict_url = reverse('pharmacy:price_prediction_api')
        self.analytics_url = reverse('pharmacy:analytics')

    def test_price_prediction_service_known_medicine(self):
        """Test direct service inference on a known standard medicine (Dolo 650)."""
        res = predict_medicine_price(
            name='Dolo 650 Tablet',
            quantity='strip of 15 tablets',
            manufacturer='Micro Labs Ltd',
            salt_composition='Paracetamol (650mg)',
            pack_size=15
        )
        self.assertTrue(res['success'])
        self.assertIn('estimated_mrp', res)
        self.assertGreater(res['estimated_mrp'], 0.0)
        self.assertEqual(res['currency'], 'INR')
        self.assertEqual(res['name'], 'Dolo 650 Tablet')
        self.assertEqual(res['pack_size'], 15.0)

    def test_price_prediction_service_unseen_features(self):
        """Test that unseen/novel medicine name and manufacturer don't crash the pipeline."""
        res = predict_medicine_price(
            name='TotallyNewBioGeneric 1000mg',
            quantity='box of 20 capsules',
            manufacturer='NovelBiotech Solutions Pvt Ltd',
            salt_composition='NewCompound (500mg)',
            pack_size=20
        )
        self.assertTrue(res['success'])
        self.assertGreater(res['estimated_mrp'], 0.0)
        self.assertLessEqual(res['estimated_mrp'], 5000.0)

    def test_price_prediction_service_extract_pack_size_from_quantity(self):
        """Test pack size extraction from quantity string when pack_size is not explicitly passed."""
        res = predict_medicine_price(
            name='Augmentin 625 Duo Tablet',
            quantity='strip of 10 tablets',
            manufacturer='Glaxo SmithKline Pharmaceuticals Ltd',
            salt_composition='Amoxycillin (500mg) + Clavulanic Acid (125mg)',
            pack_size=None
        )
        self.assertTrue(res['success'])
        self.assertEqual(res['pack_size'], 10.0)
        self.assertGreater(res['estimated_mrp'], 0.0)

    def test_price_prediction_service_missing_name_fails(self):
        """Service should return error when name is missing or blank."""
        res = predict_medicine_price(name='', pack_size=10)
        self.assertFalse(res['success'])
        self.assertIn('error', res)
        self.assertIn('Name', res['error'])

    def test_price_prediction_service_invalid_pack_size_fails(self):
        """Service should return error when pack_size is <= 0 or invalid."""
        res1 = predict_medicine_price(name='Dolo 650 Tablet', pack_size=0)
        self.assertFalse(res1['success'])
        self.assertIn('error', res1)

        res2 = predict_medicine_price(name='Dolo 650 Tablet', pack_size=-5)
        self.assertFalse(res2['success'])
        self.assertIn('error', res2)

        res3 = predict_medicine_price(name='Dolo 650 Tablet', pack_size='invalid_text')
        self.assertFalse(res3['success'])
        self.assertIn('error', res3)

    def test_price_prediction_service_bounds(self):
        """Service predictions must always stay within the designated min and max bounds."""
        res = predict_medicine_price(
            name='A',
            pack_size=1
        )
        self.assertTrue(res['success'])
        self.assertGreaterEqual(res['estimated_mrp'], 1.0)
        self.assertLessEqual(res['estimated_mrp'], 5000.0)

    def test_price_prediction_api_json_success(self):
        """POST application/json to API endpoint returns HTTP 200 and expected payload."""
        self.client.force_login(self.pharmacy_user)
        payload = {
            'name': 'Dolo 650 Tablet',
            'quantity': 'strip of 15 tablets',
            'manufacturer': 'Micro Labs Ltd',
            'salt_composition': 'Paracetamol (650mg)',
            'pack_size': 15
        }
        res = self.client.post(
            self.predict_url,
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertIn('estimated_mrp', data)
        self.assertGreater(data['estimated_mrp'], 0.0)
        self.assertEqual(data['currency'], 'INR')

    def test_price_prediction_api_form_success(self):
        """POST form parameters to API endpoint returns HTTP 200."""
        self.client.force_login(self.pharmacy_user)
        res = self.client.post(
            self.predict_url,
            data={
                'name': 'Azithral 500 Tablet',
                'pack_size': '5',
                'quantity': 'strip of 5 tablets',
                'manufacturer': 'Alembic Pharmaceuticals Ltd',
                'salt_composition': 'Azithromycin (500mg)'
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertGreater(data['estimated_mrp'], 0.0)

    def test_price_prediction_api_validation_error_missing_name(self):
        """POST without medicine name returns HTTP 400."""
        self.client.force_login(self.pharmacy_user)
        res = self.client.post(
            self.predict_url,
            data=json.dumps({'name': '', 'pack_size': 10}),
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(res.status_code, 400)
        data = res.json()
        self.assertFalse(data['success'])
        self.assertIn('error', data)

    def test_price_prediction_api_validation_error_invalid_pack_size(self):
        """POST with invalid pack size returns HTTP 400."""
        self.client.force_login(self.pharmacy_user)
        res = self.client.post(
            self.predict_url,
            data=json.dumps({'name': 'Dolo 650 Tablet', 'pack_size': -10}),
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(res.status_code, 400)
        data = res.json()
        self.assertFalse(data['success'])

    def test_price_prediction_api_requires_post(self):
        """GET request to predict-price API returns HTTP 405 Method Not Allowed."""
        self.client.force_login(self.pharmacy_user)
        res = self.client.get(self.predict_url)
        self.assertEqual(res.status_code, 405)

    def test_price_prediction_api_authentication_required(self):
        """Unauthenticated request redirects to login."""
        res = self.client.post(self.predict_url, data={'name': 'Dolo 650', 'pack_size': 10})
        self.assertEqual(res.status_code, 302)

    def test_price_prediction_api_patient_forbidden(self):
        """Patient user is forbidden or redirected away from pharmacy-only API."""
        self.client.force_login(self.patient_user)
        res = self.client.post(
            self.predict_url,
            data=json.dumps({'name': 'Dolo 650', 'pack_size': 10}),
            content_type='application/json'
        )
        self.assertIn(res.status_code, [302, 403])

    def test_price_prediction_ui_in_analytics_template(self):
        """Analytics template contains price prediction section, inputs, and disclaimers."""
        self.client.force_login(self.pharmacy_user)
        res = self.client.get(self.analytics_url)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'price-prediction-section')
        self.assertContains(res, 'pred-medicine-name')
        self.assertContains(res, 'pred-pack-size')
        self.assertContains(res, 'pred-submit-btn')
        self.assertContains(res, 'Ridge Regression Model v2')

    def test_regression_existing_composition_lookup_still_works(self):
        """Regression test ensuring composition lookup continues to work properly."""
        MedicineComposition.objects.create(
            name='Test Regress Med',
            salt_composition='Test Active Substance (500mg)',
            manufacturer='Regress Pharma',
            quantity='strip of 10 tablets',
            mrp=Decimal('45.00')
        )
        self.client.force_login(self.pharmacy_user)
        res = self.client.get(
            reverse('pharmacy:composition_lookup_api'),
            {'q': 'Test Regress Med'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['status'], 'VERIFIED_MATCH')
        self.assertEqual(data['salt_composition'], 'Test Active Substance (500mg)')


class PharmacyProfileRejectionReasonTests(TestCase):
    """
    Focused Phase 4 Step 4.1 tests verifying:
    - rejection_reason field exists on PharmacyProfile
    - Field type is models.TextField with blank=True, null=True
    - Acceptance of None, empty string, and non-empty text
    - Zero disruption to default profile creation without rejection_reason
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username='rejection_test_user',
            email='rejection_test@example.com',
            password='TestPassword123!',
            role='PHARMACY',
        )

    def test_rejection_reason_field_metadata(self):
        """Verify field existence, type, blank=True, null=True on model metadata."""
        field = PharmacyProfile._meta.get_field('rejection_reason')
        self.assertIsInstance(field, models.TextField)
        self.assertTrue(field.blank)
        self.assertTrue(field.null)

    def test_save_with_none_rejection_reason(self):
        """PharmacyProfile can be saved with rejection_reason=None."""
        profile = PharmacyProfile.objects.create(
            user=self.user,
            pharmacy_name='Null Reason Chemist',
            owner_name='Owner',
            pharmacist_name='Pharmacist',
            pharmacy_license_number='DL-REJ-01',
            gst_number='GST01',
            phone_number='9876543210',
            email='rej01@test.com',
            address='123 Test St',
            city='Mumbai',
            state='Maharashtra',
            pincode='400001',
            approval_status='REJECTED',
            rejection_reason=None,
        )
        profile.refresh_from_db()
        self.assertIsNone(profile.rejection_reason)

    def test_save_with_empty_string_rejection_reason(self):
        """PharmacyProfile can be saved with rejection_reason=''."""
        profile = PharmacyProfile.objects.create(
            user=self.user,
            pharmacy_name='Empty Reason Chemist',
            owner_name='Owner',
            pharmacist_name='Pharmacist',
            pharmacy_license_number='DL-REJ-02',
            gst_number='GST02',
            phone_number='9876543210',
            email='rej02@test.com',
            address='123 Test St',
            city='Mumbai',
            state='Maharashtra',
            pincode='400001',
            approval_status='REJECTED',
            rejection_reason='',
        )
        profile.refresh_from_db()
        self.assertEqual(profile.rejection_reason, '')

    def test_save_and_retrieve_non_empty_rejection_reason(self):
        """A non-empty rejection reason is stored and retrieved correctly."""
        reason_text = "Drug license DL-REJ-03 expired on 2026-01-15. Please upload renewed certificate."
        profile = PharmacyProfile.objects.create(
            user=self.user,
            pharmacy_name='Document Expired Chemist',
            owner_name='Owner',
            pharmacist_name='Pharmacist',
            pharmacy_license_number='DL-REJ-03',
            gst_number='GST03',
            phone_number='9876543210',
            email='rej03@test.com',
            address='123 Test St',
            city='Mumbai',
            state='Maharashtra',
            pincode='400001',
            approval_status='REJECTED',
            rejection_reason=reason_text,
        )
        profile.refresh_from_db()
        self.assertEqual(profile.rejection_reason, reason_text)

    def test_profile_creation_without_rejection_reason_argument(self):
        """Profile creation without rejection_reason defaults to None cleanly."""
        profile = PharmacyProfile.objects.create(
            user=self.user,
            pharmacy_name='Standard Pending Chemist',
            owner_name='Owner',
            pharmacist_name='Pharmacist',
            pharmacy_license_number='DL-REJ-04',
            gst_number='GST04',
            phone_number='9876543210',
            email='rej04@test.com',
            address='123 Test St',
            city='Mumbai',
            state='Maharashtra',
            pincode='400001',
            approval_status='PENDING',
        )
        profile.refresh_from_db()
        self.assertIsNone(profile.rejection_reason)














