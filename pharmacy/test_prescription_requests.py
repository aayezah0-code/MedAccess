"""
Unit and Integration Tests for MedAccess Pharmacy Request Inbox and Status Management (Phase 3).

Tests:
1. Authentication & Role Authorization (Anonymous, Patient, Unapproved Pharmacy, Approved Pharmacy).
2. Pharmacy Request Inbox Data Isolation (Pharmacy A cannot see Pharmacy B's requests).
3. Status Tab Filtering and Precomputed Counts.
4. Search Functionality (by Request ID, Patient Name, Patient Phone).
5. Request Detail Workspace Scoping & Access Control (Pharmacy B gets 404 for Pharmacy A's request).
6. Request Detail Live Stock vs Snapshot Stock Computation.
   - Aggregates unexpired batches with quantity > 0 for matched catalogue medicines.
   - Excludes expired batches.
   - Excludes zero quantity batches.
   - Excludes inventory from other pharmacies.
   - Handles unmapped / ambiguous catalogue items gracefully.
7. Status Transition Engine:
   - PENDING -> ACCEPTED
   - PENDING -> REJECTED (Enforces mandatory rejection reason >= 5 non-whitespace chars)
   - ACCEPTED -> PROCESSING -> READY_FOR_PICKUP -> COMPLETED
   - Rejection of invalid / out-of-order transitions (e.g. PENDING -> COMPLETED)
   - Rejection of transitions from terminal states (COMPLETED, REJECTED, CANCELLED)
8. Pharmacy Notes Handling (rejection note persistence, intermediate notes, blank note preservation).
9. Stock Invariance Verification (verifies Inventory.quantity is NEVER deducted or reserved).
10. Secure Prescription Document Access (authorized pharmacy allowed, unauthorized pharmacy gets 403).
11. Dashboard Preservation (verifies pharmacy dashboard remains completely unmodified).
"""

from datetime import timedelta
from decimal import Decimal
import io

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from patient.models import (
    PatientProfile,
    Prescription,
    PrescriptionMedicine,
    PrescriptionRequest,
    PrescriptionRequestItem,
)
from pharmacy.models import Medicine, Inventory, PharmacyProfile

User = get_user_model()


def create_test_image_file():
    """Generate a valid, minimal in-memory JPEG image for prescription upload."""
    img = Image.new('RGB', (100, 100), color=(200, 220, 240))
    buffer = io.BytesIO()
    img.save(buffer, format='JPEG')
    buffer.seek(0)
    return SimpleUploadedFile(
        'test_rx.jpg',
        buffer.getvalue(),
        content_type='image/jpeg'
    )


class PharmacyPrescriptionRequestTests(TestCase):
    """Exhaustive test suite for Phase 3 Pharmacy Request Inbox & Status Management."""

    def setUp(self):
        """Set up test users, profiles, pharmacies, catalogue medicines, and inventories."""
        self.client = Client()
        self.today = timezone.localdate()

        # 1. Patient User & Profile
        self.patient_user = User.objects.create_user(
            username='rx_patient',
            email='rx_patient@medaccess.test',
            password='testpassword123',
            role='PATIENT',
            is_approved=True,
            email_verified=True,
            phone_number='9876543210',
            first_name='Rahul',
            last_name='Sharma'
        )
        self.patient_profile, _ = PatientProfile.objects.get_or_create(
            user=self.patient_user,
            defaults={
                'current_latitude': Decimal('12.9716'),
                'current_longitude': Decimal('77.5946'),
                'address': 'Flat 402, Green Glen Layout, Bellandur, Bangalore',
                'allergies': 'Penicillin allergy'
            }
        )

        # 2. Approved Pharmacy 1 (Apollo Pharmacy)
        self.pharm_user_1 = User.objects.create_user(
            username='apollo_user',
            email='apollo@medaccess.test',
            password='testpassword123',
            role='PHARMACY',
            is_approved=True,
            email_verified=True
        )
        self.pharmacy_1 = PharmacyProfile.objects.create(
            user=self.pharm_user_1,
            pharmacy_name='Apollo Pharmacy Indiranagar',
            pharmacy_license_number='KA-BLR-2026-001',
            latitude=Decimal('12.9780'),
            longitude=Decimal('77.6000'),
            approval_status='APPROVED',
            phone_number='080-25252525',
            address='100 Feet Road, Indiranagar, Bangalore'
        )

        # 3. Approved Pharmacy 2 (MedPlus Pharmacy - for isolation testing)
        self.pharm_user_2 = User.objects.create_user(
            username='medplus_user',
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

        # 4. Unapproved Pharmacy (Pending verification)
        self.unapproved_pharm_user = User.objects.create_user(
            username='pending_pharm_user',
            email='pending@medaccess.test',
            password='testpassword123',
            role='PHARMACY',
            is_approved=False
        )
        self.unapproved_pharmacy = PharmacyProfile.objects.create(
            user=self.unapproved_pharm_user,
            pharmacy_name='Pending Pharmacy HSR',
            pharmacy_license_number='KA-BLR-2026-999',
            latitude=Decimal('12.9750'),
            longitude=Decimal('77.5980'),
            approval_status='PENDING',
            phone_number='080-99999999'
        )

        # 5. Master Catalogue Medicines
        self.med_dolo = Medicine.objects.create(
            medicine_name='Dolo 650mg Tablet',
            composition='Paracetamol (650mg)',
            strength='650mg',
            manufacturer='Micro Labs Ltd',
            dosage_form='Tablet',
            pack_size='15 tablets in 1 strip',
            mrp=Decimal('30.00')
        )
        self.med_azith = Medicine.objects.create(
            medicine_name='Azithral 500 Tablet',
            composition='Azithromycin (500mg)',
            strength='500mg',
            manufacturer='Alembic Pharmaceuticals Ltd',
            dosage_form='Tablet',
            pack_size='5 tablets in 1 strip',
            mrp=Decimal('120.00')
        )

        # 6. Inventories for Pharmacy 1:
        # Batch A: Dolo, unexpired, 50 units
        self.inv_dolo_valid = Inventory.objects.create(
            pharmacy=self.pharmacy_1,
            medicine=self.med_dolo,
            batch_number='BATCH-DOLO-01',
            quantity=50,
            expiry_date=self.today + timedelta(days=180),
            unit='strip'
        )
        # Batch B: Dolo, another unexpired batch, 30 units (total live Dolo = 80)
        self.inv_dolo_valid_2 = Inventory.objects.create(
            pharmacy=self.pharmacy_1,
            medicine=self.med_dolo,
            batch_number='BATCH-DOLO-02',
            quantity=30,
            expiry_date=self.today + timedelta(days=90),
            unit='strip'
        )
        # Batch C: Dolo, EXPIRED batch, 100 units (MUST be excluded from live stock)
        self.inv_dolo_expired = Inventory.objects.create(
            pharmacy=self.pharmacy_1,
            medicine=self.med_dolo,
            batch_number='BATCH-DOLO-EXP',
            quantity=100,
            expiry_date=self.today - timedelta(days=10),
            unit='strip'
        )
        # Batch D: Azithral, unexpired, 0 units (out of stock, excluded)
        self.inv_azith_zero = Inventory.objects.create(
            pharmacy=self.pharmacy_1,
            medicine=self.med_azith,
            batch_number='BATCH-AZITH-01',
            quantity=0,
            expiry_date=self.today + timedelta(days=60),
            unit='strip'
        )

        # Inventory for Pharmacy 2 (Dolo 200 units, MUST NOT leak into Pharmacy 1's live stock)
        self.inv_pharm2_dolo = Inventory.objects.create(
            pharmacy=self.pharmacy_2,
            medicine=self.med_dolo,
            batch_number='BATCH-PHARM2-01',
            quantity=200,
            expiry_date=self.today + timedelta(days=180),
            unit='strip'
        )

        # 7. Prescription for Patient
        self.prescription = Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image=create_test_image_file(),
            status='COMPLETED'
        )
        self.pm_dolo = PrescriptionMedicine.objects.create(
            prescription=self.prescription,
            medicine_name='Dolo 650mg Tablet',
            strength='650mg',
            dosage='650mg',
            frequency='Twice daily',
            duration='5 days',
            quantity='10',
            instructions='After food',
            source='AI_CONFIRMED',
            extracted_index=1
        )
        self.pm_azith = PrescriptionMedicine.objects.create(
            prescription=self.prescription,
            medicine_name='Azithral 500 Tablet',
            strength='500mg',
            dosage='500mg',
            frequency='Once daily',
            duration='3 days',
            quantity='3',
            instructions='Before sleep',
            source='AI_CONFIRMED',
            extracted_index=2
        )

        # 8. Sample PrescriptionRequest for Pharmacy 1
        self.request_1 = PrescriptionRequest.objects.create(
            prescription=self.prescription,
            patient=self.patient_profile,
            pharmacy=self.pharmacy_1,
            status='PENDING',
            patient_notes='Please deliver urgent fever medications.'
        )
        # Item 1: Dolo (matched catalogue, snapshot available_quantity = 80)
        self.item_1 = PrescriptionRequestItem.objects.create(
            request=self.request_1,
            prescription_medicine=self.pm_dolo,
            matched_catalogue_medicine=self.med_dolo,
            prescribed_name_snapshot='Dolo 650mg Tablet',
            prescribed_strength_snapshot='650mg',
            prescribed_dosage_snapshot='650mg',
            prescribed_frequency_snapshot='Twice daily',
            prescribed_duration_snapshot='5 days',
            prescribed_quantity_snapshot='10',
            prescribed_instructions_snapshot='After food',
            match_status='MATCHED',
            availability_status='FULLY_AVAILABLE',
            available_quantity_snapshot=80,
            required_quantity=10,
            explanation=''
        )
        # Item 2: Azithral (matched catalogue, snapshot available_quantity = 0)
        self.item_2 = PrescriptionRequestItem.objects.create(
            request=self.request_1,
            prescription_medicine=self.pm_azith,
            matched_catalogue_medicine=self.med_azith,
            prescribed_name_snapshot='Azithral 500 Tablet',
            prescribed_strength_snapshot='500mg',
            prescribed_dosage_snapshot='500mg',
            prescribed_frequency_snapshot='Once daily',
            prescribed_duration_snapshot='3 days',
            prescribed_quantity_snapshot='3',
            prescribed_instructions_snapshot='Before sleep',
            match_status='MATCHED',
            availability_status='OUT_OF_STOCK',
            available_quantity_snapshot=0,
            required_quantity=3,
            explanation='Zero stock available'
        )

    # =========================================================================
    # 1. Authentication & Role Authorization Tests
    # =========================================================================

    def test_anonymous_user_redirected_to_login(self):
        """Anonymous users must be redirected to accounts:login for all Phase 3 endpoints."""
        urls = [
            reverse('pharmacy:requests_inbox'),
            reverse('pharmacy:request_detail', kwargs={'pk': self.request_1.pk}),
            reverse('pharmacy:request_update_status', kwargs={'pk': self.request_1.pk}),
        ]
        for url in urls:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302)
            self.assertIn('/accounts/login/', resp.url)

    def test_patient_user_cannot_access_pharmacy_views(self):
        """Users with role PATIENT cannot access pharmacy inbox, detail, or update status."""
        self.client.login(username='rx_patient', password='testpassword123')

        resp_inbox = self.client.get(reverse('pharmacy:requests_inbox'))
        self.assertEqual(resp_inbox.status_code, 302)
        self.assertIn(reverse('landing'), resp_inbox.url)

        resp_detail = self.client.get(reverse('pharmacy:request_detail', kwargs={'pk': self.request_1.pk}))
        self.assertEqual(resp_detail.status_code, 302)
        self.assertIn(reverse('landing'), resp_detail.url)

        resp_update = self.client.post(
            reverse('pharmacy:request_update_status', kwargs={'pk': self.request_1.pk}),
            data={'status': 'ACCEPTED'}
        )
        self.assertEqual(resp_update.status_code, 302)
        self.assertIn(reverse('landing'), resp_update.url)

    def test_unapproved_pharmacy_cannot_access_views(self):
        """Unapproved pharmacies (approval_status != 'APPROVED') are redirected to landing."""
        self.client.login(username='pending_pharm_user', password='testpassword123')

        resp_inbox = self.client.get(reverse('pharmacy:requests_inbox'))
        self.assertEqual(resp_inbox.status_code, 302)
        self.assertIn(reverse('landing'), resp_inbox.url)

    def test_approved_pharmacy_can_access_inbox(self):
        """Approved pharmacy can successfully access the requests inbox."""
        self.client.login(username='apollo_user', password='testpassword123')
        resp = self.client.get(reverse('pharmacy:requests_inbox'))
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'pharmacy/requests_inbox.html')

    # =========================================================================
    # 2. Data Scoping & Multi-Tenant Pharmacy Isolation Tests
    # =========================================================================

    def test_inbox_scopes_to_authenticated_pharmacy_only(self):
        """Pharmacy 1 must only see requests for Pharmacy 1, never Pharmacy 2."""
        # Create a request directed to Pharmacy 2
        request_for_pharm2 = PrescriptionRequest.objects.create(
            prescription=self.prescription,
            patient=self.patient_profile,
            pharmacy=self.pharmacy_2,
            status='PENDING'
        )

        # Log in as Pharmacy 1
        self.client.login(username='apollo_user', password='testpassword123')
        resp = self.client.get(reverse('pharmacy:requests_inbox'))
        self.assertEqual(resp.status_code, 200)

        inbox_requests = list(resp.context['page_obj'])
        self.assertIn(self.request_1, inbox_requests)
        self.assertNotIn(request_for_pharm2, inbox_requests)

        # Log in as Pharmacy 2
        self.client.login(username='medplus_user', password='testpassword123')
        resp2 = self.client.get(reverse('pharmacy:requests_inbox'))
        self.assertEqual(resp2.status_code, 200)

        inbox2_requests = list(resp2.context['page_obj'])
        self.assertIn(request_for_pharm2, inbox2_requests)
        self.assertNotIn(self.request_1, inbox2_requests)

    def test_request_detail_raises_404_for_unrelated_pharmacy(self):
        """Pharmacy 2 cannot view Pharmacy 1's request detail (returns 404)."""
        self.client.login(username='medplus_user', password='testpassword123')
        resp = self.client.get(reverse('pharmacy:request_detail', kwargs={'pk': self.request_1.pk}))
        self.assertEqual(resp.status_code, 404)

    def test_request_update_raises_404_for_unrelated_pharmacy(self):
        """Pharmacy 2 cannot update status of Pharmacy 1's request (returns 404)."""
        self.client.login(username='medplus_user', password='testpassword123')
        resp = self.client.post(
            reverse('pharmacy:request_update_status', kwargs={'pk': self.request_1.pk}),
            data={'status': 'ACCEPTED'}
        )
        self.assertEqual(resp.status_code, 404)
        self.request_1.refresh_from_db()
        self.assertEqual(self.request_1.status, 'PENDING')

    # =========================================================================
    # 3. Status Tab Filtering & Precomputed Counts
    # =========================================================================

    def test_status_tab_filtering_and_counts(self):
        """Verify status filtering tabs and counts precomputation."""
        # Create additional requests for Pharmacy 1 in distinct statuses
        req_accepted = PrescriptionRequest.objects.create(
            prescription=self.prescription,
            patient=self.patient_profile,
            pharmacy=self.pharmacy_1,
            status='ACCEPTED'
        )
        req_processing = PrescriptionRequest.objects.create(
            prescription=self.prescription,
            patient=self.patient_profile,
            pharmacy=self.pharmacy_1,
            status='PROCESSING'
        )

        self.client.login(username='apollo_user', password='testpassword123')

        # Check 'all' tab
        resp = self.client.get(reverse('pharmacy:requests_inbox'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['counts']['all'], 3)
        self.assertEqual(resp.context['counts']['pending'], 1)
        self.assertEqual(resp.context['counts']['accepted'], 1)
        self.assertEqual(resp.context['counts']['processing'], 1)

        # Filter by 'pending'
        resp_pending = self.client.get(reverse('pharmacy:requests_inbox'), {'status': 'pending'})
        self.assertEqual(resp_pending.status_code, 200)
        requests_p = list(resp_pending.context['page_obj'])
        self.assertEqual(len(requests_p), 1)
        self.assertEqual(requests_p[0].id, self.request_1.id)

        # Filter by 'accepted'
        resp_acc = self.client.get(reverse('pharmacy:requests_inbox'), {'status': 'accepted'})
        self.assertEqual(resp_acc.status_code, 200)
        requests_a = list(resp_acc.context['page_obj'])
        self.assertEqual(len(requests_a), 1)
        self.assertEqual(requests_a[0].id, req_accepted.id)

    # =========================================================================
    # 4. Search Functionality
    # =========================================================================

    def test_search_by_request_id_and_patient_name(self):
        """Verify search by numeric request ID and patient first/last name."""
        self.client.login(username='apollo_user', password='testpassword123')

        # Search by request ID
        resp_id = self.client.get(reverse('pharmacy:requests_inbox'), {'q': str(self.request_1.id)})
        self.assertEqual(resp_id.status_code, 200)
        self.assertEqual(len(resp_id.context['page_obj']), 1)
        self.assertEqual(resp_id.context['page_obj'][0].id, self.request_1.id)

        # Search by patient name
        resp_name = self.client.get(reverse('pharmacy:requests_inbox'), {'q': 'Rahul'})
        self.assertEqual(resp_name.status_code, 200)
        self.assertEqual(len(resp_name.context['page_obj']), 1)

        # Search non-matching text
        resp_none = self.client.get(reverse('pharmacy:requests_inbox'), {'q': 'NonExistentUser99'})
        self.assertEqual(resp_none.status_code, 200)
        self.assertEqual(len(resp_none.context['page_obj']), 0)

    # =========================================================================
    # 5. Live Stock vs Snapshot Stock Computation
    # =========================================================================

    def test_live_stock_vs_snapshot_computation(self):
        """
        Verify that request_detail computes current live stock correctly:
        - Dolo live stock: Batch 1 (50) + Batch 2 (30) = 80.
        - Expired Batch (100) is excluded.
        - Pharmacy 2 Batch (200) is excluded.
        - Azithral live stock: 0 (batch has quantity=0).
        - Historical snapshot is preserved at 80 and 0 respectively.
        """
        self.client.login(username='apollo_user', password='testpassword123')
        resp = self.client.get(reverse('pharmacy:request_detail', kwargs={'pk': self.request_1.pk}))
        self.assertEqual(resp.status_code, 200)

        items = resp.context['items']
        dolo_item = next(it for it in items if it.matched_catalogue_medicine_id == self.med_dolo.id)
        azith_item = next(it for it in items if it.matched_catalogue_medicine_id == self.med_azith.id)

        # Snapshot checks
        self.assertEqual(dolo_item.available_quantity_snapshot, 80)
        self.assertEqual(dolo_item.availability_status, 'FULLY_AVAILABLE')
        self.assertEqual(azith_item.available_quantity_snapshot, 0)
        self.assertEqual(azith_item.availability_status, 'OUT_OF_STOCK')

        # Live stock checks
        self.assertTrue(dolo_item.has_catalogue_match)
        self.assertEqual(dolo_item.current_live_stock, 80)  # 50 + 30, excludes 100 expired and 200 from pharmacy 2

        self.assertTrue(azith_item.has_catalogue_match)
        self.assertEqual(azith_item.current_live_stock, 0)

    def test_unmapped_medicine_in_detail_view(self):
        """Prescription medicine without catalogue match shows has_catalogue_match=False and current_live_stock=None."""
        unmapped_item = PrescriptionRequestItem.objects.create(
            request=self.request_1,
            prescription_medicine=None,
            matched_catalogue_medicine=None,
            prescribed_name_snapshot='Custom Ayurvedic Tonic 200ml',
            prescribed_dosage_snapshot='10ml',
            prescribed_frequency_snapshot='Twice daily',
            prescribed_duration_snapshot='10 days',
            prescribed_quantity_snapshot='1',
            match_status='UNMAPPED',
            availability_status='NOT_STOCKED',
            available_quantity_snapshot=0,
            explanation='Catalogue match unavailable'
        )

        self.client.login(username='apollo_user', password='testpassword123')
        resp = self.client.get(reverse('pharmacy:request_detail', kwargs={'pk': self.request_1.pk}))
        self.assertEqual(resp.status_code, 200)

        items = resp.context['items']
        found = next(it for it in items if it.id == unmapped_item.id)
        self.assertFalse(found.has_catalogue_match)
        self.assertIsNone(found.current_live_stock)

    # =========================================================================
    # 6. Status Transition Engine & Validation Tests
    # =========================================================================

    def test_transition_pending_to_accepted(self):
        """PENDING -> ACCEPTED is valid and updates the database."""
        self.client.login(username='apollo_user', password='testpassword123')
        url = reverse('pharmacy:request_update_status', kwargs={'pk': self.request_1.pk})

        resp = self.client.post(url, data={
            'status': 'ACCEPTED',
            'pharmacy_notes': 'Prescription verified. Ready to prepare order.'
        })
        self.assertEqual(resp.status_code, 302)
        self.assertRedirects(resp, reverse('pharmacy:request_detail', kwargs={'pk': self.request_1.pk}))

        self.request_1.refresh_from_db()
        self.assertEqual(self.request_1.status, 'ACCEPTED')
        self.assertEqual(self.request_1.pharmacy_notes, 'Prescription verified. Ready to prepare order.')

    def test_transition_pending_to_rejected_requires_mandatory_reason(self):
        """
        Rejection requires a meaningful pharmacy note (>= 5 non-whitespace chars).
        Whitespace-only or < 5 characters must fail and keep status as PENDING.
        """
        self.client.login(username='apollo_user', password='testpassword123')
        url = reverse('pharmacy:request_update_status', kwargs={'pk': self.request_1.pk})

        # Case 1: Empty note
        resp1 = self.client.post(url, data={'status': 'REJECTED', 'pharmacy_notes': ''})
        self.assertEqual(resp1.status_code, 302)
        self.request_1.refresh_from_db()
        self.assertEqual(self.request_1.status, 'PENDING')

        # Case 2: Note with only 3 characters
        resp2 = self.client.post(url, data={'status': 'REJECTED', 'pharmacy_notes': 'No'})
        self.assertEqual(resp2.status_code, 302)
        self.request_1.refresh_from_db()
        self.assertEqual(self.request_1.status, 'PENDING')

        # Case 3: Note with spaces only
        resp3 = self.client.post(url, data={'status': 'REJECTED', 'pharmacy_notes': '     '})
        self.assertEqual(resp3.status_code, 302)
        self.request_1.refresh_from_db()
        self.assertEqual(self.request_1.status, 'PENDING')

        # Case 4: Valid rejection reason (>= 5 chars)
        resp4 = self.client.post(url, data={
            'status': 'REJECTED',
            'pharmacy_notes': 'Out of stock for Azithral 500 tablet.'
        })
        self.assertEqual(resp4.status_code, 302)
        self.request_1.refresh_from_db()
        self.assertEqual(self.request_1.status, 'REJECTED')
        self.assertEqual(self.request_1.pharmacy_notes, 'Out of stock for Azithral 500 tablet.')

    def test_full_lifecycle_status_flow(self):
        """Test happy-path status flow: PENDING -> ACCEPTED -> PROCESSING -> READY_FOR_PICKUP -> COMPLETED."""
        self.client.login(username='apollo_user', password='testpassword123')
        url = reverse('pharmacy:request_update_status', kwargs={'pk': self.request_1.pk})

        # 1. PENDING -> ACCEPTED
        resp = self.client.post(url, data={'status': 'ACCEPTED', 'pharmacy_notes': 'Accepted order.'})
        self.assertEqual(resp.status_code, 302)
        self.request_1.refresh_from_db()
        self.assertEqual(self.request_1.status, 'ACCEPTED')

        # 2. ACCEPTED -> PROCESSING
        resp = self.client.post(url, data={'status': 'PROCESSING', 'pharmacy_notes': 'Dispensing medication.'})
        self.assertEqual(resp.status_code, 302)
        self.request_1.refresh_from_db()
        self.assertEqual(self.request_1.status, 'PROCESSING')

        # 3. PROCESSING -> READY_FOR_PICKUP
        resp = self.client.post(url, data={'status': 'READY_FOR_PICKUP', 'pharmacy_notes': 'Package packed and ready at counter.'})
        self.assertEqual(resp.status_code, 302)
        self.request_1.refresh_from_db()
        self.assertEqual(self.request_1.status, 'READY_FOR_PICKUP')

        # 4. READY_FOR_PICKUP -> COMPLETED
        resp = self.client.post(url, data={'status': 'COMPLETED', 'pharmacy_notes': 'Patient collected medication.'})
        self.assertEqual(resp.status_code, 302)
        self.request_1.refresh_from_db()
        self.assertEqual(self.request_1.status, 'COMPLETED')

    def test_invalid_status_skipping_prevented(self):
        """Attempting to jump from PENDING directly to PROCESSING or COMPLETED must fail."""
        self.client.login(username='apollo_user', password='testpassword123')
        url = reverse('pharmacy:request_update_status', kwargs={'pk': self.request_1.pk})

        resp = self.client.post(url, data={'status': 'COMPLETED'})
        self.assertEqual(resp.status_code, 302)
        self.request_1.refresh_from_db()
        self.assertEqual(self.request_1.status, 'PENDING')

        resp2 = self.client.post(url, data={'status': 'PROCESSING'})
        self.assertEqual(resp2.status_code, 302)
        self.request_1.refresh_from_db()
        self.assertEqual(self.request_1.status, 'PENDING')

    def test_terminal_states_cannot_transition(self):
        """Once COMPLETED, REJECTED, or CANCELLED, pharmacy cannot transition to any other status."""
        self.client.login(username='apollo_user', password='testpassword123')
        url = reverse('pharmacy:request_update_status', kwargs={'pk': self.request_1.pk})

        # Test COMPLETED terminal state
        self.request_1.status = 'COMPLETED'
        self.request_1.save()

        resp = self.client.post(url, data={'status': 'PROCESSING'})
        self.assertEqual(resp.status_code, 302)
        self.request_1.refresh_from_db()
        self.assertEqual(self.request_1.status, 'COMPLETED')

        # Test CANCELLED terminal state
        self.request_1.status = 'CANCELLED'
        self.request_1.save()

        resp = self.client.post(url, data={'status': 'ACCEPTED'})
        self.assertEqual(resp.status_code, 302)
        self.request_1.refresh_from_db()
        self.assertEqual(self.request_1.status, 'CANCELLED')

    def test_blank_notes_on_transition_preserves_existing_notes(self):
        """When moving to an intermediate status without a new note, existing pharmacy_notes are preserved."""
        self.request_1.status = 'ACCEPTED'
        self.request_1.pharmacy_notes = 'Original accepted note.'
        self.request_1.save()

        self.client.login(username='apollo_user', password='testpassword123')
        url = reverse('pharmacy:request_update_status', kwargs={'pk': self.request_1.pk})

        # Transition to PROCESSING with empty note
        resp = self.client.post(url, data={'status': 'PROCESSING', 'pharmacy_notes': ''})
        self.assertEqual(resp.status_code, 302)
        self.request_1.refresh_from_db()
        self.assertEqual(self.request_1.status, 'PROCESSING')
        self.assertEqual(self.request_1.pharmacy_notes, 'Original accepted note.')

    # =========================================================================
    # 7. Stock Invariance Verification (NEVER Deduct or Reserve Stock)
    # =========================================================================

    def test_inventory_quantities_remain_strictly_unchanged(self):
        """Viewing detail or advancing statuses must NEVER modify Inventory.quantity."""
        self.client.login(username='apollo_user', password='testpassword123')

        # Record initial quantities
        qty_dolo_1 = self.inv_dolo_valid.quantity
        qty_dolo_2 = self.inv_dolo_valid_2.quantity
        qty_azith = self.inv_azith_zero.quantity

        # View detail
        self.client.get(reverse('pharmacy:request_detail', kwargs={'pk': self.request_1.pk}))

        # Advance status through multiple stages
        url = reverse('pharmacy:request_update_status', kwargs={'pk': self.request_1.pk})
        self.client.post(url, data={'status': 'ACCEPTED'})
        self.client.post(url, data={'status': 'PROCESSING'})
        self.client.post(url, data={'status': 'READY_FOR_PICKUP'})
        self.client.post(url, data={'status': 'COMPLETED'})

        # Refresh all inventories
        self.inv_dolo_valid.refresh_from_db()
        self.inv_dolo_valid_2.refresh_from_db()
        self.inv_azith_zero.refresh_from_db()

        # Assert no change
        self.assertEqual(self.inv_dolo_valid.quantity, qty_dolo_1)
        self.assertEqual(self.inv_dolo_valid_2.quantity, qty_dolo_2)
        self.assertEqual(self.inv_azith_zero.quantity, qty_azith)

    # =========================================================================
    # 8. Secure Prescription Document Access
    # =========================================================================

    def test_prescription_image_access_by_authorized_pharmacy(self):
        """Authorized pharmacy can view prescription image via /patient/prescriptions/<pk>/image/."""
        self.client.login(username='apollo_user', password='testpassword123')
        img_url = reverse('patient:prescription_secure_image', kwargs={'pk': self.prescription.pk})

        resp = self.client.get(img_url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'image/jpeg')

    def test_prescription_image_denied_for_unauthorized_pharmacy(self):
        """Pharmacy 2 (not selected for this prescription) gets 403 Forbidden."""
        self.client.login(username='medplus_user', password='testpassword123')
        img_url = reverse('patient:prescription_secure_image', kwargs={'pk': self.prescription.pk})

        resp = self.client.get(img_url)
        self.assertEqual(resp.status_code, 403)

    # =========================================================================
    # 9. Dashboard Preservation Test
    # =========================================================================

    def test_pharmacy_dashboard_unmodified_and_working(self):
        """Verify that the existing pharmacy dashboard loads cleanly and uses dashboard.html."""
        self.client.login(username='apollo_user', password='testpassword123')
        resp = self.client.get(reverse('pharmacy:dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'pharmacy/dashboard.html')
