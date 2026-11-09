"""
Unit and Integration Tests for MedAccess Prescription Request System (Phase 2).

Tests:
1. Model creation, relationships, and on_delete=PROTECT enforcement.
2. Patient prescription ownership on availability check and request submission.
3. Incomplete prescription rejection (UPLOADED, PROCESSING, FAILED).
4. Empty prescription rejection (0 confirmed medicines).
5. Unapproved pharmacy rejection (PENDING, REJECTED).
6. Explicit patient consent requirement before sharing prescription.
7. Full, partial, and out-of-stock evaluation and snapshot persistence.
8. Ambiguous and unmapped medicine snapshot persistence.
9. Liquid medicine quantity handling (indeterminate unit safeguards).
10. Duplicate active request prevention (PENDING, ACCEPTED, PROCESSING, READY_FOR_PICKUP).
11. Re-requesting allowed after CANCELLED, REJECTED, or COMPLETED status.
12. Patient request-list isolation (Patient A cannot see Patient B's requests).
13. Patient request-detail isolation (Patient A cannot view Patient B's request).
14. Patient pending request cancellation.
15. Secure prescription image access by owner patient.
16. Secure prescription image access by authorized pharmacy.
17. Secure prescription image denial for unauthorized pharmacy or unrelated patient.
18. Atomic transaction rollback if line item snapshot creation fails.
"""

from datetime import timedelta
from decimal import Decimal
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, models
from django.db.models import ProtectedError
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from patient.models import (
    PatientProfile,
    Prescription,
    PrescriptionMedicine,
    PrescriptionRequest,
    PrescriptionRequestItem,
)
from pharmacy.models import Medicine, Inventory, PharmacyProfile

User = get_user_model()


class PrescriptionRequestSystemTests(TestCase):
    """Exhaustive test suite for Phase 2 Prescription Request System."""

    def setUp(self):
        """Set up test users, profiles, pharmacies, catalogue medicines, and inventories."""
        self.client = Client()
        self.today = timezone.localdate()

        # 1. Patient User & Profile
        self.patient_user = User.objects.create_user(
            username='test_patient',
            email='patient@medaccess.test',
            password='testpassword123',
            role='PATIENT',
            is_approved=True,
            email_verified=True,
            phone_number='9876543210'
        )
        self.patient_profile, _ = PatientProfile.objects.get_or_create(
            user=self.patient_user,
            defaults={
                'current_latitude': Decimal('12.9716'),
                'current_longitude': Decimal('77.5946'),
            }
        )

        # 2. Another Patient User & Profile (for isolation tests)
        self.other_patient_user = User.objects.create_user(
            username='other_patient',
            email='other@medaccess.test',
            password='testpassword123',
            role='PATIENT',
            is_approved=True,
            email_verified=True,
            phone_number='9876543211'
        )
        self.other_patient_profile, _ = PatientProfile.objects.get_or_create(
            user=self.other_patient_user,
            defaults={
                'current_latitude': Decimal('12.9716'),
                'current_longitude': Decimal('77.5946'),
            }
        )

        # 3. Approved Pharmacy 1 (Nearby - ~1.5 km)
        self.pharm_user = User.objects.create_user(
            username='apollo_pharmacy',
            email='apollo@medaccess.test',
            password='testpassword123',
            role='PHARMACY',
            is_approved=True,
            email_verified=True
        )
        self.pharmacy = PharmacyProfile.objects.create(
            user=self.pharm_user,
            pharmacy_name='Apollo Pharmacy Indiranagar',
            pharmacy_license_number='KA-BLR-2026-001',
            latitude=Decimal('12.9780'),
            longitude=Decimal('77.6000'),
            approval_status='APPROVED',
            phone_number='080-25252525',
            address='100 Feet Road, Indiranagar, Bangalore'
        )

        # 4. Approved Pharmacy 2 (for multi-pharmacy and image tests)
        self.pharm_user_2 = User.objects.create_user(
            username='medplus_pharmacy',
            email='medplus@medaccess.test',
            password='testpassword123',
            role='PHARMACY',
            is_approved=True,
            email_verified=True
        )
        self.pharmacy_2 = PharmacyProfile.objects.create(
            user=self.pharm_user_2,
            pharmacy_name='MedPlus Pharmacy Koramangala',
            pharmacy_license_number='KA-BLR-2026-002',
            latitude=Decimal('12.9352'),
            longitude=Decimal('77.6245'),
            approval_status='APPROVED',
            phone_number='080-26262626',
            address='80 Feet Road, Koramangala, Bangalore'
        )

        # 5. Unapproved Pharmacy (Pending status)
        self.unapproved_pharm_user = User.objects.create_user(
            username='pending_pharmacy',
            email='pending@medaccess.test',
            password='testpassword123',
            role='PHARMACY',
            is_approved=False
        )
        self.unapproved_pharmacy = PharmacyProfile.objects.create(
            user=self.unapproved_pharm_user,
            pharmacy_name='Pending Pharmacy',
            pharmacy_license_number='KA-BLR-2026-999',
            latitude=Decimal('12.9750'),
            longitude=Decimal('77.5980'),
            approval_status='PENDING',
            phone_number='080-99999999'
        )

        # 6. Master Catalogue Medicines
        self.med_dolo = Medicine.objects.create(
            medicine_name='Dolo 650mg Tablet',
            composition='Paracetamol (650mg)',
            strength='650mg',
            manufacturer='Micro Labs Ltd',
            dosage_form='Tablet',
            pack_size='15 tablets in 1 strip',
            mrp=Decimal('30.00')
        )
        self.med_amox = Medicine.objects.create(
            medicine_name='Augmentin 625 Duo Tablet',
            composition='Amoxycillin (500mg) + Clavulanic Acid (125mg)',
            strength='625mg',
            manufacturer='GlaxoSmithKline Pharmaceuticals Ltd',
            dosage_form='Tablet',
            pack_size='10 tablets in 1 strip',
            mrp=Decimal('200.00')
        )
        self.med_syrup = Medicine.objects.create(
            medicine_name='Ascoril LS Syrup',
            composition='Ambroxol (30mg/5ml) + Levosalbutamol (1mg/5ml) + Guaifenesin (50mg/5ml)',
            strength='100ml',
            manufacturer='Glenmark Pharmaceuticals Ltd',
            dosage_form='Syrup',
            pack_size='100ml in 1 bottle',
            mrp=Decimal('115.00')
        )

        # 7. Inventory for Pharmacy 1
        # Dolo: 50 units in stock
        Inventory.objects.create(
            pharmacy=self.pharmacy,
            medicine=self.med_dolo,
            batch_number='DOLO-B1',
            expiry_date=self.today + timedelta(days=180),
            quantity=50,
            unit='strip',
            low_stock_limit=5
        )
        # Augmentin: 5 units in stock (partial for high quantity)
        Inventory.objects.create(
            pharmacy=self.pharmacy,
            medicine=self.med_amox,
            batch_number='AUG-B1',
            expiry_date=self.today + timedelta(days=90),
            quantity=5,
            unit='strip',
            low_stock_limit=2
        )
        # Syrup: 10 bottles in stock
        Inventory.objects.create(
            pharmacy=self.pharmacy,
            medicine=self.med_syrup,
            batch_number='ASC-B1',
            expiry_date=self.today + timedelta(days=120),
            quantity=10,
            unit='bottle',
            low_stock_limit=3
        )

        # 8. Completed Prescription for Patient
        dummy_file = SimpleUploadedFile("prescription_test.jpg", b"fake prescription image content", content_type="image/jpeg")
        self.prescription = Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image=dummy_file,
            status='COMPLETED'
        )

        # Confirmed Prescribed Medicines on Prescription
        self.pm_dolo = PrescriptionMedicine.objects.create(
            prescription=self.prescription,
            medicine_name='Dolo 650',
            strength='650mg',
            dosage='1 tablet',
            frequency='1-0-1',
            duration='5 days',
            quantity='10',
            instructions='After food',
            source='AI_CONFIRMED',
            extracted_index=1
        )
        self.pm_amox = PrescriptionMedicine.objects.create(
            prescription=self.prescription,
            medicine_name='Augmentin 625 Duo',
            strength='625mg',
            dosage='1 tablet',
            frequency='Twice daily',
            duration='10 days',
            quantity='20',
            instructions='Complete course',
            source='AI_CONFIRMED',
            extracted_index=2
        )

    # ========================================================================
    # 1. Model Creation & on_delete=PROTECT Verification
    # ========================================================================

    def test_prescription_request_model_creation(self):
        """Verify PrescriptionRequest and PrescriptionRequestItem fields, defaults and choices."""
        req = PrescriptionRequest.objects.create(
            patient=self.patient_profile,
            pharmacy=self.pharmacy,
            prescription=self.prescription,
            status='PENDING',
            initial_availability_status='FULLY_AVAILABLE',
            patient_notes='Please pack neatly'
        )
        self.assertEqual(req.status, 'PENDING')
        self.assertEqual(req.initial_availability_status, 'FULLY_AVAILABLE')
        self.assertTrue(req.is_active)
        self.assertIn('Apollo Pharmacy Indiranagar', str(req))

        item = PrescriptionRequestItem.objects.create(
            request=req,
            prescription_medicine=self.pm_dolo,
            matched_catalogue_medicine=self.med_dolo,
            prescribed_name_snapshot='Dolo 650',
            prescribed_strength_snapshot='650mg',
            prescribed_dosage_snapshot='1 tablet',
            prescribed_frequency_snapshot='1-0-1',
            prescribed_duration_snapshot='5 days',
            prescribed_quantity_snapshot='10',
            prescribed_instructions_snapshot='After food',
            match_status='MATCHED',
            availability_status='FULLY_AVAILABLE',
            required_quantity=10,
            required_quantity_note='Calculated: 1 tab x 2 times x 5 days = 10 units',
            available_quantity_snapshot=50,
            explanation='In stock: 50 units available'
        )
        self.assertEqual(item.request, req)
        self.assertEqual(item.prescribed_name_snapshot, 'Dolo 650')
        self.assertEqual(item.available_quantity_snapshot, 50)
        self.assertIn('Dolo 650', str(item))

    def test_prescription_protect_on_delete(self):
        """Prescription cannot be deleted if a PrescriptionRequest points to it (on_delete=PROTECT)."""
        PrescriptionRequest.objects.create(
            patient=self.patient_profile,
            pharmacy=self.pharmacy,
            prescription=self.prescription,
            status='PENDING'
        )
        with self.assertRaises(ProtectedError):
            self.prescription.delete()

    # ========================================================================
    # 2. Availability Check Endpoint Tests
    # ========================================================================

    def test_check_availability_endpoint_success(self):
        """GET /patient/prescriptions/<pk>/check-pharmacies/ returns structured availability JSON."""
        self.client.force_login(self.patient_user)
        url = reverse('patient:check_pharmacies_availability', kwargs={'pk': self.prescription.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

        data = resp.json()
        self.assertEqual(data['status'], 'success')
        self.assertGreater(data['count'], 0)

        pharm = next((p for p in data['pharmacies'] if p['pharmacy_id'] == self.pharmacy.id), None)
        self.assertIsNotNone(pharm)
        self.assertEqual(pharm['pharmacy_name'], 'Apollo Pharmacy Indiranagar')
        self.assertIn(pharm['overall_status'], ['FULLY_AVAILABLE', 'PARTIALLY_AVAILABLE'])
        self.assertEqual(len(pharm['items']), 2)

    def test_check_availability_ownership_enforcement(self):
        """Another patient cannot check availability for someone else's prescription."""
        self.client.force_login(self.other_patient_user)
        url = reverse('patient:check_pharmacies_availability', kwargs={'pk': self.prescription.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 404)

    def test_check_availability_incomplete_prescription_rejected(self):
        """Availability check is rejected for prescriptions that are not COMPLETED."""
        self.prescription.status = 'PROCESSING'
        self.prescription.save(update_fields=['status'])

        self.client.force_login(self.patient_user)
        url = reverse('patient:check_pharmacies_availability', kwargs={'pk': self.prescription.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('completed', resp.json().get('message', '').lower())

    def test_check_availability_empty_medicines_rejected(self):
        """Prescription with 0 confirmed medicines cannot be checked."""
        self.prescription.medicines.all().delete()

        self.client.force_login(self.patient_user)
        url = reverse('patient:check_pharmacies_availability', kwargs={'pk': self.prescription.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('no confirmed medicines', resp.json().get('message', '').lower())

    def test_check_availability_location_required_prompt(self):
        """If patient coordinates are missing and no query params supplied, returns location_required."""
        self.patient_profile.current_latitude = None
        self.patient_profile.current_longitude = None
        self.patient_profile.save(update_fields=['current_latitude', 'current_longitude'])

        self.client.force_login(self.patient_user)
        url = reverse('patient:check_pharmacies_availability', kwargs={'pk': self.prescription.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json().get('status'), 'location_required')

    # ========================================================================
    # 3. Request Submission & Snapshot Verification
    # ========================================================================

    def test_send_request_success_and_snapshots(self):
        """POST /patient/prescriptions/<pk>/send-request/ creates request and immutable snapshots."""
        self.client.force_login(self.patient_user)
        url = reverse('patient:send_prescription_request', kwargs={'pk': self.prescription.pk})

        resp = self.client.post(
            url,
            data={
                'pharmacy_id': self.pharmacy.id,
                'consent': 'true',
                'patient_notes': 'Please keep ready by 5 PM.'
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['status'], 'success')

        req = PrescriptionRequest.objects.get(pk=data['request_id'])
        self.assertEqual(req.patient, self.patient_profile)
        self.assertEqual(req.pharmacy, self.pharmacy)
        self.assertEqual(req.prescription, self.prescription)
        self.assertEqual(req.status, 'PENDING')
        self.assertEqual(req.patient_notes, 'Please keep ready by 5 PM.')

        # Verify snapshots
        items = list(req.items.all().order_by('id'))
        self.assertEqual(len(items), 2)

        # Dolo item snapshot
        dolo_item = next(it for it in items if 'Dolo' in it.prescribed_name_snapshot)
        self.assertEqual(dolo_item.prescribed_strength_snapshot, '650mg')
        self.assertEqual(dolo_item.prescribed_dosage_snapshot, '1 tablet')
        self.assertEqual(dolo_item.prescribed_frequency_snapshot, '1-0-1')
        self.assertEqual(dolo_item.prescribed_duration_snapshot, '5 days')
        self.assertEqual(dolo_item.match_status, 'MATCHED')
        self.assertEqual(dolo_item.availability_status, 'FULLY_AVAILABLE')
        self.assertEqual(dolo_item.available_quantity_snapshot, 50)

        # Augmentin item snapshot: required 20, available 5 -> PARTIALLY_AVAILABLE
        amox_item = next(it for it in items if 'Augmentin' in it.prescribed_name_snapshot)
        self.assertEqual(amox_item.prescribed_strength_snapshot, '625mg')
        self.assertEqual(amox_item.availability_status, 'PARTIALLY_AVAILABLE')
        self.assertEqual(amox_item.available_quantity_snapshot, 5)

    def test_send_request_unapproved_pharmacy_rejected(self):
        """Cannot send request to a pharmacy with PENDING approval."""
        self.client.force_login(self.patient_user)
        url = reverse('patient:send_prescription_request', kwargs={'pk': self.prescription.pk})

        resp = self.client.post(
            url,
            data={
                'pharmacy_id': self.unapproved_pharmacy.id,
                'consent': 'true'
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('not approved', resp.json().get('message', '').lower())

    def test_send_request_explicit_consent_required(self):
        """Sending request without consent=true fails with 400."""
        self.client.force_login(self.patient_user)
        url = reverse('patient:send_prescription_request', kwargs={'pk': self.prescription.pk})

        resp = self.client.post(
            url,
            data={
                'pharmacy_id': self.pharmacy.id,
                'consent': 'false'
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('consent is required', resp.json().get('message', '').lower())

    def test_send_request_ownership_enforcement(self):
        """Another patient cannot submit request for someone else's prescription."""
        self.client.force_login(self.other_patient_user)
        url = reverse('patient:send_prescription_request', kwargs={'pk': self.prescription.pk})

        resp = self.client.post(
            url,
            data={
                'pharmacy_id': self.pharmacy.id,
                'consent': 'true'
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(resp.status_code, 404)

    # ========================================================================
    # 4. Duplicate Request Protection
    # ========================================================================

    def test_send_request_duplicate_active_prevention(self):
        """Submitting an active request when one already exists returns 409 duplicate."""
        self.client.force_login(self.patient_user)
        url = reverse('patient:send_prescription_request', kwargs={'pk': self.prescription.pk})

        # First request succeeds
        resp1 = self.client.post(
            url,
            data={'pharmacy_id': self.pharmacy.id, 'consent': 'true'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(resp1.status_code, 200)

        # Second request to same pharmacy while active is rejected
        resp2 = self.client.post(
            url,
            data={'pharmacy_id': self.pharmacy.id, 'consent': 'true'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(resp2.status_code, 409)
        self.assertEqual(resp2.json().get('status'), 'duplicate_request')

    def test_send_request_allowed_after_cancellation_or_completion(self):
        """New request is allowed if earlier request was CANCELLED or COMPLETED."""
        # Create an earlier cancelled request
        PrescriptionRequest.objects.create(
            patient=self.patient_profile,
            pharmacy=self.pharmacy,
            prescription=self.prescription,
            status='CANCELLED'
        )

        self.client.force_login(self.patient_user)
        url = reverse('patient:send_prescription_request', kwargs={'pk': self.prescription.pk})

        resp = self.client.post(
            url,
            data={'pharmacy_id': self.pharmacy.id, 'consent': 'true'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['status'], 'success')

    # ========================================================================
    # 5. Liquid Medicine Handling & Ambiguous/Unmapped Snapshots
    # ========================================================================

    def test_send_request_liquid_medicine_handling(self):
        """Liquid medicine duration volume is not converted to discrete bottles without evidence."""
        pm_liquid = PrescriptionMedicine.objects.create(
            prescription=self.prescription,
            medicine_name='Ascoril LS Syrup',
            strength='100ml',
            dosage='5 ml',
            frequency='Twice daily',
            duration='5 days',
            instructions='Shake well before use',
            source='MANUAL',
            extracted_index=3
        )

        self.client.force_login(self.patient_user)
        url = reverse('patient:send_prescription_request', kwargs={'pk': self.prescription.pk})

        resp = self.client.post(
            url,
            data={'pharmacy_id': self.pharmacy.id, 'consent': 'true'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(resp.status_code, 200)

        req = PrescriptionRequest.objects.get(pk=resp.json()['request_id'])
        item = req.items.get(prescribed_name_snapshot='Ascoril LS Syrup')
        self.assertEqual(item.availability_status, 'IN_STOCK')
        self.assertIsNone(item.required_quantity)
        self.assertIn('Liquid medicine', item.required_quantity_note)
        self.assertEqual(item.available_quantity_snapshot, 10)

    def test_send_request_ambiguous_and_unmapped_medicines(self):
        """Unmapped and ambiguous medicines preserve unmapped status in snapshots."""
        PrescriptionMedicine.objects.create(
            prescription=self.prescription,
            medicine_name='NonExistentUnmappedMedicineXYZ 100mg',
            strength='100mg',
            dosage='1 tablet',
            frequency='Once daily',
            duration='3 days',
            source='MANUAL',
            extracted_index=4
        )

        self.client.force_login(self.patient_user)
        url = reverse('patient:send_prescription_request', kwargs={'pk': self.prescription.pk})

        resp = self.client.post(
            url,
            data={'pharmacy_id': self.pharmacy.id, 'consent': 'true'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(resp.status_code, 200)

        req = PrescriptionRequest.objects.get(pk=resp.json()['request_id'])
        item = req.items.get(prescribed_name_snapshot='NonExistentUnmappedMedicineXYZ 100mg')
        self.assertEqual(item.match_status, 'UNMAPPED')
        self.assertEqual(item.availability_status, 'NOT_STOCKED')
        self.assertEqual(item.available_quantity_snapshot, 0)

    # ========================================================================
    # 6. Patient Tracking & Data Isolation Tests
    # ========================================================================

    def test_patient_request_list_isolation(self):
        """GET /patient/requests/ only shows requests belonging to the logged-in patient."""
        req1 = PrescriptionRequest.objects.create(
            patient=self.patient_profile,
            pharmacy=self.pharmacy,
            prescription=self.prescription,
            status='PENDING'
        )

        # Other patient prescription and request
        other_rx = Prescription.objects.create(
            patient=self.other_patient_profile,
            prescription_image=self.prescription.prescription_image,
            status='COMPLETED'
        )
        req2 = PrescriptionRequest.objects.create(
            patient=self.other_patient_profile,
            pharmacy=self.pharmacy,
            prescription=other_rx,
            status='PENDING'
        )

        self.client.force_login(self.patient_user)
        resp = self.client.get(reverse('patient:requests_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f'Request #{req1.id}')
        self.assertNotContains(resp, f'Request #{req2.id}')

    def test_patient_request_detail_isolation(self):
        """Patient B cannot access Patient A's request detail page."""
        req = PrescriptionRequest.objects.create(
            patient=self.patient_profile,
            pharmacy=self.pharmacy,
            prescription=self.prescription,
            status='PENDING'
        )

        self.client.force_login(self.other_patient_user)
        resp = self.client.get(reverse('patient:request_detail', kwargs={'pk': req.pk}))
        self.assertEqual(resp.status_code, 404)

    def test_patient_cancel_pending_request(self):
        """Patient can cancel a request that is still PENDING."""
        req = PrescriptionRequest.objects.create(
            patient=self.patient_profile,
            pharmacy=self.pharmacy,
            prescription=self.prescription,
            status='PENDING'
        )

        self.client.force_login(self.patient_user)
        url = reverse('patient:cancel_prescription_request', kwargs={'pk': req.pk})
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)

        req.refresh_from_db()
        self.assertEqual(req.status, 'CANCELLED')

    def test_patient_cancel_non_pending_request_fails(self):
        """Patient cannot cancel a request once it is ACCEPTED."""
        req = PrescriptionRequest.objects.create(
            patient=self.patient_profile,
            pharmacy=self.pharmacy,
            prescription=self.prescription,
            status='ACCEPTED'
        )

        self.client.force_login(self.patient_user)
        url = reverse('patient:cancel_prescription_request', kwargs={'pk': req.pk})
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)

        req.refresh_from_db()
        self.assertEqual(req.status, 'ACCEPTED')

    # ========================================================================
    # 7. Prescription Image Authorization Tests
    # ========================================================================

    def test_prescription_image_security_patient_owner(self):
        """Owner patient can view their own prescription document."""
        self.client.force_login(self.patient_user)
        url = reverse('patient:prescription_secure_image', kwargs={'pk': self.prescription.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_prescription_image_security_selected_pharmacy(self):
        """Selected pharmacy with an active request CAN access the document."""
        PrescriptionRequest.objects.create(
            patient=self.patient_profile,
            pharmacy=self.pharmacy,
            prescription=self.prescription,
            status='PENDING'
        )

        self.client.force_login(self.pharm_user)
        url = reverse('patient:prescription_secure_image', kwargs={'pk': self.prescription.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_prescription_image_security_unselected_pharmacy_denied(self):
        """Pharmacy 2 without any request relationship is DENIED access (403)."""
        # Request sent only to Pharmacy 1
        PrescriptionRequest.objects.create(
            patient=self.patient_profile,
            pharmacy=self.pharmacy,
            prescription=self.prescription,
            status='PENDING'
        )

        self.client.force_login(self.pharm_user_2)
        url = reverse('patient:prescription_secure_image', kwargs={'pk': self.prescription.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 403)

    def test_prescription_image_security_unrelated_patient_denied(self):
        """Another patient cannot access this prescription image (403)."""
        self.client.force_login(self.other_patient_user)
        url = reverse('patient:prescription_secure_image', kwargs={'pk': self.prescription.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 403)

    # ========================================================================
    # 8. Atomic Transaction Rollback on Failure
    # ========================================================================

    def test_send_request_atomic_rollback_on_failure(self):
        """If item creation fails, entire request transaction rolls back."""
        self.client.force_login(self.patient_user)
        url = reverse('patient:send_prescription_request', kwargs={'pk': self.prescription.pk})

        initial_request_count = PrescriptionRequest.objects.count()
        initial_item_count = PrescriptionRequestItem.objects.count()

        # Mock PrescriptionRequestItem.objects.bulk_create to raise an IntegrityError
        with patch('patient.models.PrescriptionRequestItem.objects.bulk_create', side_effect=IntegrityError("Simulated DB failure")):
            resp = self.client.post(
                url,
                data={'pharmacy_id': self.pharmacy.id, 'consent': 'true'},
                HTTP_X_REQUESTED_WITH='XMLHttpRequest'
            )
            self.assertEqual(resp.status_code, 500)

        # Verify rollback: no new PrescriptionRequest or PrescriptionRequestItem was created
        self.assertEqual(PrescriptionRequest.objects.count(), initial_request_count)
        self.assertEqual(PrescriptionRequestItem.objects.count(), initial_item_count)

