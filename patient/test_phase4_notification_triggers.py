"""
Focused Integration Tests for Phase 4 Step 3: Notification Lifecycle Triggers.

Tests:
1. Request submission:
   - Successful submission creates a pharmacy alert (REQUEST_SUBMITTED).
   - Successful submission creates a patient confirmation (REQUEST_SUBMITTED).
   - Both notifications reference the newly created PrescriptionRequest.
   - Notifications have is_read=False, read_at=None, and correct recipient/actor.
   - Failed submission (e.g. unapproved pharmacy, missing consent) creates zero notifications.
2. Patient cancellation:
   - Successful PENDING cancellation creates both pharmacy alert and patient confirmation.
   - Cancellation of a non-PENDING request creates zero notifications.
   - Unauthorized patient cannot cancel another patient's request (creates zero notifications).
3. Pharmacy status transitions:
   - PENDING -> ACCEPTED creates REQUEST_ACCEPTED notification for patient.
   - PENDING -> REJECTED creates REQUEST_REJECTED notification for patient and includes rejection reason.
   - ACCEPTED -> PROCESSING creates REQUEST_PROCESSING notification for patient.
   - PROCESSING -> READY_FOR_PICKUP creates REQUEST_READY notification for patient.
   - READY_FOR_PICKUP -> COMPLETED creates REQUEST_COMPLETED notification for patient.
   - Invalid / out-of-order transitions create zero notifications.
   - Cross-pharmacy access (Pharmacy B updating Pharmacy A) generates 404 and zero notifications.
   - Repeated / duplicate status updates create zero extra notifications.
"""

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
    Notification,
)
from pharmacy.models import Medicine, Inventory, PharmacyProfile

User = get_user_model()


def create_test_image_file():
    """Generate a minimal in-memory JPEG image for prescription upload."""
    img = Image.new('RGB', (100, 100), color=(200, 220, 240))
    buffer = io.BytesIO()
    img.save(buffer, format='JPEG')
    buffer.seek(0)
    return SimpleUploadedFile(
        'test_rx.jpg',
        buffer.getvalue(),
        content_type='image/jpeg'
    )


class NotificationLifecycleTriggersTestCase(TestCase):
    """Exhaustive tests for lifecycle notification triggers."""

    def setUp(self):
        """Set up test users, profiles, pharmacies, inventory, and prescription."""
        self.client = Client()
        self.today = timezone.localdate()

        # 1. Patient User & Profile
        self.patient_user = User.objects.create_user(
            username='trig_patient',
            email='trig_patient@medaccess.test',
            password='testpassword123',
            role='PATIENT',
            is_approved=True,
            email_verified=True,
            first_name='Ananya',
            last_name='Iyer'
        )
        self.patient_profile, _ = PatientProfile.objects.get_or_create(
            user=self.patient_user,
            defaults={
                'current_latitude': Decimal('12.9716'),
                'current_longitude': Decimal('77.5946'),
                'address': 'Koramangala, Bangalore'
            }
        )

        # 2. Another Patient User (for isolation testing)
        self.other_patient_user = User.objects.create_user(
            username='trig_other_patient',
            email='other_p@medaccess.test',
            password='testpassword123',
            role='PATIENT',
            is_approved=True,
            email_verified=True
        )
        self.other_patient_profile, _ = PatientProfile.objects.get_or_create(
            user=self.other_patient_user,
            defaults={
                'current_latitude': Decimal('12.9716'),
                'current_longitude': Decimal('77.5946'),
            }
        )

        # 3. Approved Pharmacy 1
        self.pharm_user = User.objects.create_user(
            username='trig_pharma_1',
            email='pharma1@medaccess.test',
            password='testpassword123',
            role='PHARMACY',
            is_approved=True,
            email_verified=True
        )
        self.pharmacy = PharmacyProfile.objects.create(
            user=self.pharm_user,
            pharmacy_name='Trigger Apollo Pharmacy',
            pharmacy_license_number='KA-BLR-2026-TR1',
            latitude=Decimal('12.9780'),
            longitude=Decimal('77.6000'),
            approval_status='APPROVED',
            phone_number='080-11112222',
            address='100 Feet Rd, Bangalore'
        )

        # 4. Approved Pharmacy 2 (for multi-tenant isolation testing)
        self.pharm_user_2 = User.objects.create_user(
            username='trig_pharma_2',
            email='pharma2@medaccess.test',
            password='testpassword123',
            role='PHARMACY',
            is_approved=True,
            email_verified=True
        )
        self.pharmacy_2 = PharmacyProfile.objects.create(
            user=self.pharm_user_2,
            pharmacy_name='Trigger MedPlus Pharmacy',
            pharmacy_license_number='KA-BLR-2026-TR2',
            latitude=Decimal('12.9352'),
            longitude=Decimal('77.6245'),
            approval_status='APPROVED',
            phone_number='080-33334444',
            address='80 Feet Rd, Bangalore'
        )

        # 5. Catalogue Medicine & Inventory
        self.med = Medicine.objects.create(
            medicine_name='Paracetamol 650mg Tablet',
            composition='Paracetamol (650mg)',
            strength='650mg',
            manufacturer='Micro Labs',
            dosage_form='Tablet',
            pack_size='15 tablets in 1 strip'
        )
        self.inventory = Inventory.objects.create(
            pharmacy=self.pharmacy,
            medicine=self.med,
            batch_number='BATCH-TR-01',
            quantity=100,
            expiry_date=self.today + timezone.timedelta(days=180),
            unit='strip'
        )

        # 6. Prescription & Confirmed Medicine
        self.prescription = Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image=create_test_image_file(),
            status='COMPLETED'
        )
        self.presc_med = PrescriptionMedicine.objects.create(
            prescription=self.prescription,
            medicine_name='Paracetamol 650mg Tablet',
            dosage='650mg',
            frequency='Twice daily',
            duration='5 days',
            quantity='10',
            instructions='After meals',
            source='AI_CONFIRMED',
            extracted_index=1
        )

    # =========================================================================
    # A. Request Submission Notification Triggers
    # =========================================================================

    def test_successful_submission_creates_pharmacy_and_patient_notifications(self):
        """Successful request submission generates both pharmacy alert and patient confirmation."""
        self.client.force_login(self.patient_user)
        url = reverse('patient:send_prescription_request', kwargs={'pk': self.prescription.pk})

        initial_count = Notification.objects.count()

        resp = self.client.post(
            url,
            data={
                'pharmacy_id': self.pharmacy.id,
                'consent': 'true',
                'patient_notes': 'Please keep packed.'
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(resp.status_code, 200)

        # Exactly 2 new notifications must be created
        self.assertEqual(Notification.objects.count(), initial_count + 2)

        created_request = PrescriptionRequest.objects.get(
            prescription=self.prescription,
            pharmacy=self.pharmacy
        )

        # 1. Pharmacy Alert Notification
        pharma_notif = Notification.objects.get(
            prescription_request=created_request,
            recipient=self.pharm_user
        )
        self.assertEqual(pharma_notif.actor, self.patient_user)
        self.assertEqual(pharma_notif.notification_type, 'REQUEST_SUBMITTED')
        self.assertEqual(pharma_notif.title, 'New Prescription Request')
        self.assertIn(str(created_request.id), pharma_notif.message)
        self.assertFalse(pharma_notif.is_read)
        self.assertIsNone(pharma_notif.read_at)

        # 2. Patient Confirmation Notification
        patient_notif = Notification.objects.get(
            prescription_request=created_request,
            recipient=self.patient_user
        )
        self.assertEqual(patient_notif.actor, self.patient_user)
        self.assertEqual(patient_notif.notification_type, 'REQUEST_SUBMITTED')
        self.assertEqual(patient_notif.title, 'Request Submitted Successfully')
        self.assertIn(str(created_request.id), patient_notif.message)
        self.assertIn(self.pharmacy.pharmacy_name, patient_notif.message)
        self.assertFalse(patient_notif.is_read)
        self.assertIsNone(patient_notif.read_at)

    def test_failed_submission_creates_no_notifications(self):
        """When submission fails validation (e.g. missing consent), no notifications are created."""
        self.client.force_login(self.patient_user)
        url = reverse('patient:send_prescription_request', kwargs={'pk': self.prescription.pk})

        initial_count = Notification.objects.count()

        resp = self.client.post(
            url,
            data={
                'pharmacy_id': self.pharmacy.id,
                # 'consent' omitted
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Notification.objects.count(), initial_count)

    # =========================================================================
    # B. Patient Cancellation Notification Triggers
    # =========================================================================

    def test_successful_cancellation_creates_both_notifications(self):
        """Cancelling a PENDING request generates both pharmacy alert and patient confirmation."""
        # Create a pending request
        req = PrescriptionRequest.objects.create(
            prescription=self.prescription,
            patient=self.patient_profile,
            pharmacy=self.pharmacy,
            status='PENDING'
        )

        self.client.force_login(self.patient_user)
        url = reverse('patient:cancel_prescription_request', kwargs={'pk': req.pk})

        initial_count = Notification.objects.count()
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)

        req.refresh_from_db()
        self.assertEqual(req.status, 'CANCELLED')

        # Exactly 2 new notifications must be created
        self.assertEqual(Notification.objects.count(), initial_count + 2)

        # 1. Pharmacy Alert
        pharma_notif = Notification.objects.get(
            prescription_request=req,
            recipient=self.pharm_user,
            notification_type='REQUEST_CANCELLED'
        )
        self.assertEqual(pharma_notif.actor, self.patient_user)
        self.assertEqual(pharma_notif.title, 'Prescription Request Cancelled')
        self.assertIn(str(req.id), pharma_notif.message)
        self.assertFalse(pharma_notif.is_read)

        # 2. Patient Confirmation
        patient_notif = Notification.objects.get(
            prescription_request=req,
            recipient=self.patient_user,
            notification_type='REQUEST_CANCELLED'
        )
        self.assertEqual(patient_notif.actor, self.patient_user)
        self.assertEqual(patient_notif.title, 'Request Cancelled')
        self.assertIn(str(req.id), patient_notif.message)
        self.assertFalse(patient_notif.is_read)

    def test_cancellation_of_non_pending_request_creates_no_notifications(self):
        """Attempting to cancel an ACCEPTED request fails and creates no notifications."""
        req = PrescriptionRequest.objects.create(
            prescription=self.prescription,
            patient=self.patient_profile,
            pharmacy=self.pharmacy,
            status='ACCEPTED'
        )

        self.client.force_login(self.patient_user)
        url = reverse('patient:cancel_prescription_request', kwargs={'pk': req.pk})

        initial_count = Notification.objects.count()
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)

        req.refresh_from_db()
        self.assertEqual(req.status, 'ACCEPTED')
        self.assertEqual(Notification.objects.count(), initial_count)

    def test_unauthorized_patient_cancellation_creates_no_notifications(self):
        """Another patient attempting to cancel Patient A's request receives 404 and creates zero notifications."""
        req = PrescriptionRequest.objects.create(
            prescription=self.prescription,
            patient=self.patient_profile,
            pharmacy=self.pharmacy,
            status='PENDING'
        )

        self.client.force_login(self.other_patient_user)
        url = reverse('patient:cancel_prescription_request', kwargs={'pk': req.pk})

        initial_count = Notification.objects.count()
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 404)

        req.refresh_from_db()
        self.assertEqual(req.status, 'PENDING')
        self.assertEqual(Notification.objects.count(), initial_count)

    # =========================================================================
    # C. Pharmacy Status Update Notification Triggers
    # =========================================================================

    def test_pharmacy_accept_creates_notification(self):
        """Pharmacy accepting a request notifies the patient with REQUEST_ACCEPTED."""
        req = PrescriptionRequest.objects.create(
            prescription=self.prescription,
            patient=self.patient_profile,
            pharmacy=self.pharmacy,
            status='PENDING'
        )

        self.client.force_login(self.pharm_user)
        url = reverse('pharmacy:request_update_status', kwargs={'pk': req.pk})

        initial_count = Notification.objects.count()
        resp = self.client.post(url, data={'status': 'ACCEPTED'})
        self.assertEqual(resp.status_code, 302)

        self.assertEqual(Notification.objects.count(), initial_count + 1)
        notif = Notification.objects.latest('created_at')
        self.assertEqual(notif.prescription_request, req)
        self.assertEqual(notif.recipient, self.patient_user)
        self.assertEqual(notif.actor, self.pharm_user)
        self.assertEqual(notif.notification_type, 'REQUEST_ACCEPTED')
        self.assertEqual(notif.title, 'Request Accepted')
        self.assertIn(str(req.id), notif.message)
        self.assertIn(self.pharmacy.pharmacy_name, notif.message)
        self.assertFalse(notif.is_read)

    def test_pharmacy_reject_creates_notification_with_reason(self):
        """Pharmacy rejecting a request notifies the patient with REQUEST_REJECTED and includes rejection reason."""
        req = PrescriptionRequest.objects.create(
            prescription=self.prescription,
            patient=self.patient_profile,
            pharmacy=self.pharmacy,
            status='PENDING'
        )

        self.client.force_login(self.pharm_user)
        url = reverse('pharmacy:request_update_status', kwargs={'pk': req.pk})

        reason = 'Prescribed formulation is currently out of stock.'
        initial_count = Notification.objects.count()
        resp = self.client.post(url, data={
            'status': 'REJECTED',
            'pharmacy_notes': reason
        })
        self.assertEqual(resp.status_code, 302)

        self.assertEqual(Notification.objects.count(), initial_count + 1)
        notif = Notification.objects.latest('created_at')
        self.assertEqual(notif.prescription_request, req)
        self.assertEqual(notif.recipient, self.patient_user)
        self.assertEqual(notif.actor, self.pharm_user)
        self.assertEqual(notif.notification_type, 'REQUEST_REJECTED')
        self.assertEqual(notif.title, 'Request Rejected')
        self.assertIn(reason, notif.message)

    def test_pharmacy_processing_creates_notification(self):
        """Pharmacy advancing status to PROCESSING notifies the patient with REQUEST_PROCESSING."""
        req = PrescriptionRequest.objects.create(
            prescription=self.prescription,
            patient=self.patient_profile,
            pharmacy=self.pharmacy,
            status='ACCEPTED'
        )

        self.client.force_login(self.pharm_user)
        url = reverse('pharmacy:request_update_status', kwargs={'pk': req.pk})

        initial_count = Notification.objects.count()
        resp = self.client.post(url, data={'status': 'PROCESSING'})
        self.assertEqual(resp.status_code, 302)

        self.assertEqual(Notification.objects.count(), initial_count + 1)
        notif = Notification.objects.latest('created_at')
        self.assertEqual(notif.notification_type, 'REQUEST_PROCESSING')
        self.assertEqual(notif.title, 'Request Processing')
        self.assertEqual(notif.recipient, self.patient_user)

    def test_pharmacy_ready_for_pickup_creates_notification(self):
        """Pharmacy advancing status to READY_FOR_PICKUP notifies the patient with REQUEST_READY."""
        req = PrescriptionRequest.objects.create(
            prescription=self.prescription,
            patient=self.patient_profile,
            pharmacy=self.pharmacy,
            status='PROCESSING'
        )

        self.client.force_login(self.pharm_user)
        url = reverse('pharmacy:request_update_status', kwargs={'pk': req.pk})

        initial_count = Notification.objects.count()
        resp = self.client.post(url, data={'status': 'READY_FOR_PICKUP'})
        self.assertEqual(resp.status_code, 302)

        self.assertEqual(Notification.objects.count(), initial_count + 1)
        notif = Notification.objects.latest('created_at')
        self.assertEqual(notif.notification_type, 'REQUEST_READY')
        self.assertEqual(notif.title, 'Ready for Pickup')
        self.assertEqual(notif.recipient, self.patient_user)

    def test_pharmacy_completed_creates_notification(self):
        """Pharmacy marking request COMPLETED notifies the patient with REQUEST_COMPLETED."""
        req = PrescriptionRequest.objects.create(
            prescription=self.prescription,
            patient=self.patient_profile,
            pharmacy=self.pharmacy,
            status='READY_FOR_PICKUP'
        )

        self.client.force_login(self.pharm_user)
        url = reverse('pharmacy:request_update_status', kwargs={'pk': req.pk})

        initial_count = Notification.objects.count()
        resp = self.client.post(url, data={'status': 'COMPLETED'})
        self.assertEqual(resp.status_code, 302)

        self.assertEqual(Notification.objects.count(), initial_count + 1)
        notif = Notification.objects.latest('created_at')
        self.assertEqual(notif.notification_type, 'REQUEST_COMPLETED')
        self.assertEqual(notif.title, 'Request Completed')
        self.assertEqual(notif.recipient, self.patient_user)

    def test_invalid_status_transition_creates_no_notifications(self):
        """Attempting an invalid jump (PENDING -> COMPLETED) creates zero notifications."""
        req = PrescriptionRequest.objects.create(
            prescription=self.prescription,
            patient=self.patient_profile,
            pharmacy=self.pharmacy,
            status='PENDING'
        )

        self.client.force_login(self.pharm_user)
        url = reverse('pharmacy:request_update_status', kwargs={'pk': req.pk})

        initial_count = Notification.objects.count()
        resp = self.client.post(url, data={'status': 'COMPLETED'})
        self.assertEqual(resp.status_code, 302)

        req.refresh_from_db()
        self.assertEqual(req.status, 'PENDING')
        self.assertEqual(Notification.objects.count(), initial_count)

    def test_cross_pharmacy_status_update_creates_no_notifications(self):
        """Pharmacy 2 attempting to update Pharmacy 1's request returns 404 and creates zero notifications."""
        req = PrescriptionRequest.objects.create(
            prescription=self.prescription,
            patient=self.patient_profile,
            pharmacy=self.pharmacy,
            status='PENDING'
        )

        self.client.force_login(self.pharm_user_2)
        url = reverse('pharmacy:request_update_status', kwargs={'pk': req.pk})

        initial_count = Notification.objects.count()
        resp = self.client.post(url, data={'status': 'ACCEPTED'})
        self.assertEqual(resp.status_code, 404)

        req.refresh_from_db()
        self.assertEqual(req.status, 'PENDING')
        self.assertEqual(Notification.objects.count(), initial_count)

    def test_repeated_status_update_does_not_create_duplicate_notifications(self):
        """Repeatedly posting ACCEPTED when already ACCEPTED fails transition and generates zero duplicate notifications."""
        req = PrescriptionRequest.objects.create(
            prescription=self.prescription,
            patient=self.patient_profile,
            pharmacy=self.pharmacy,
            status='ACCEPTED'
        )

        self.client.force_login(self.pharm_user)
        url = reverse('pharmacy:request_update_status', kwargs={'pk': req.pk})

        initial_count = Notification.objects.count()
        resp = self.client.post(url, data={'status': 'ACCEPTED'})
        self.assertEqual(resp.status_code, 302)

        req.refresh_from_db()
        self.assertEqual(req.status, 'ACCEPTED')
        self.assertEqual(Notification.objects.count(), initial_count)

