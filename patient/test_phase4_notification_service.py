"""
Focused Unit Tests for Phase 4 Notification Service (create_request_notification).

Tests:
1. Patient receives a notification successfully.
2. Pharmacy receives a notification successfully.
3. System notification works with actor=None.
4. Patient can be the actor.
5. Pharmacy can be the actor.
6. Unrelated recipient is rejected.
7. Unrelated actor is rejected.
8. Invalid notification type is rejected.
9. Blank title is rejected.
10. Whitespace-only title is rejected.
11. Blank message is rejected.
12. Whitespace-only message is rejected.
13. Title exceeding 200 characters is rejected.
14. Created notification has is_read=False.
15. Created notification has read_at=None.
16. Correct request, recipient, actor, and notification type are saved.
17. Function returns the created Notification instance.
18. Invalid request argument is rejected.
"""

from decimal import Decimal
import io

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from PIL import Image

from patient.models import (
    PatientProfile,
    Prescription,
    PrescriptionMedicine,
    PrescriptionRequest,
    Notification,
)
from patient.services.notification_service import create_request_notification
from pharmacy.models import PharmacyProfile

User = get_user_model()


def create_test_image_file():
    """Generate a valid in-memory JPEG image for prescription upload."""
    img = Image.new('RGB', (100, 100), color=(200, 220, 240))
    buffer = io.BytesIO()
    img.save(buffer, format='JPEG')
    buffer.seek(0)
    return SimpleUploadedFile(
        'test_rx.jpg',
        buffer.getvalue(),
        content_type='image/jpeg'
    )


class NotificationServiceTestCase(TestCase):
    """Exhaustive unit tests for create_request_notification function."""

    def setUp(self):
        """Set up test users, profiles, pharmacies, prescription, and prescription request."""
        self.today = timezone.localdate()

        # 1. Patient User & Profile
        self.patient_user = User.objects.create_user(
            username='service_patient',
            email='service_patient@medaccess.test',
            password='testpassword123',
            role='PATIENT',
            is_approved=True,
            email_verified=True,
            first_name='Kavita',
            last_name='Nair'
        )
        self.patient_profile, _ = PatientProfile.objects.get_or_create(
            user=self.patient_user,
            defaults={
                'current_latitude': Decimal('12.9716'),
                'current_longitude': Decimal('77.5946'),
                'address': 'Indiranagar, Bangalore'
            }
        )

        # 2. Pharmacy User & Profile
        self.pharm_user = User.objects.create_user(
            username='service_pharma',
            email='service_pharma@medaccess.test',
            password='testpassword123',
            role='PHARMACY',
            is_approved=True,
            email_verified=True
        )
        self.pharmacy_profile = PharmacyProfile.objects.create(
            user=self.pharm_user,
            pharmacy_name='Service Care Pharmacy',
            pharmacy_license_number='KA-BLR-2026-SVC',
            latitude=Decimal('12.9780'),
            longitude=Decimal('77.6000'),
            approval_status='APPROVED',
            phone_number='080-44445555',
            address='Koramangala, Bangalore'
        )

        # 3. Third-party Unrelated User
        self.unrelated_user = User.objects.create_user(
            username='service_intruder',
            email='intruder@medaccess.test',
            password='testpassword123',
            role='PATIENT',
            is_approved=True,
            email_verified=True
        )

        # 4. Prescription & Confirmed Medicine
        self.prescription = Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image=create_test_image_file(),
            status='COMPLETED'
        )
        self.presc_med = PrescriptionMedicine.objects.create(
            prescription=self.prescription,
            medicine_name='Amoxicillin 500mg',
            dosage='500mg',
            frequency='Three times daily',
            duration='5 days',
            quantity='15',
            source='AI_CONFIRMED',
            extracted_index=1
        )

        # 5. Prescription Request
        self.presc_request = PrescriptionRequest.objects.create(
            prescription=self.prescription,
            patient=self.patient_profile,
            pharmacy=self.pharmacy_profile,
            status='PENDING',
            patient_notes='Please verify stock.'
        )

    def test_patient_receives_notification_successfully(self):
        """Patient receives a notification initiated by pharmacy."""
        notif = create_request_notification(
            request=self.presc_request,
            recipient=self.patient_user,
            notification_type='REQUEST_ACCEPTED',
            title='Prescription Request Accepted',
            message='Your prescription request has been accepted.',
            actor=self.pharm_user
        )
        self.assertIsInstance(notif, Notification)
        self.assertIsNotNone(notif.pk)
        self.assertEqual(notif.recipient, self.patient_user)
        self.assertEqual(notif.actor, self.pharm_user)
        self.assertEqual(notif.prescription_request, self.presc_request)
        self.assertEqual(notif.notification_type, 'REQUEST_ACCEPTED')
        self.assertEqual(notif.title, 'Prescription Request Accepted')
        self.assertEqual(notif.message, 'Your prescription request has been accepted.')
        self.assertFalse(notif.is_read)
        self.assertIsNone(notif.read_at)

    def test_pharmacy_receives_notification_successfully(self):
        """Pharmacy receives a notification initiated by patient."""
        notif = create_request_notification(
            request=self.presc_request,
            recipient=self.pharm_user,
            notification_type='REQUEST_SUBMITTED',
            title='New Prescription Request',
            message='A new prescription request has been placed.',
            actor=self.patient_user
        )
        self.assertIsInstance(notif, Notification)
        self.assertEqual(notif.recipient, self.pharm_user)
        self.assertEqual(notif.actor, self.patient_user)
        self.assertEqual(notif.notification_type, 'REQUEST_SUBMITTED')

    def test_system_notification_with_none_actor(self):
        """System notification works when actor is explicitly None."""
        notif = create_request_notification(
            request=self.presc_request,
            recipient=self.patient_user,
            notification_type='REQUEST_PROCESSING',
            title='Processing Started',
            message='Medications are being prepared.',
            actor=None
        )
        self.assertIsNone(notif.actor)
        self.assertEqual(notif.recipient, self.patient_user)

    def test_patient_can_be_actor(self):
        """Patient can be the actor on a notification."""
        notif = create_request_notification(
            request=self.presc_request,
            recipient=self.pharm_user,
            notification_type='REQUEST_CANCELLED',
            title='Request Cancelled',
            message='Patient has cancelled the request.',
            actor=self.patient_user
        )
        self.assertEqual(notif.actor, self.patient_user)

    def test_pharmacy_can_be_actor(self):
        """Pharmacy can be the actor on a notification."""
        notif = create_request_notification(
            request=self.presc_request,
            recipient=self.patient_user,
            notification_type='REQUEST_READY',
            title='Ready for Pickup',
            message='Your prescription order is packed and ready.',
            actor=self.pharm_user
        )
        self.assertEqual(notif.actor, self.pharm_user)

    def test_unrelated_recipient_rejected(self):
        """An unrelated user cannot be the recipient."""
        with self.assertRaises(ValidationError) as ctx:
            create_request_notification(
                request=self.presc_request,
                recipient=self.unrelated_user,
                notification_type='REQUEST_SUBMITTED',
                title='Test Title',
                message='Test Message',
                actor=self.patient_user
            )
        self.assertIn('recipient', ctx.exception.message_dict)

    def test_unrelated_actor_rejected(self):
        """An unrelated user cannot be the actor."""
        with self.assertRaises(ValidationError) as ctx:
            create_request_notification(
                request=self.presc_request,
                recipient=self.patient_user,
                notification_type='REQUEST_SUBMITTED',
                title='Test Title',
                message='Test Message',
                actor=self.unrelated_user
            )
        self.assertIn('actor', ctx.exception.message_dict)

    def test_invalid_notification_type_rejected(self):
        """Invalid notification_type raises ValidationError."""
        with self.assertRaises(ValidationError) as ctx:
            create_request_notification(
                request=self.presc_request,
                recipient=self.patient_user,
                notification_type='INVALID_ACTION_TYPE',
                title='Test Title',
                message='Test Message'
            )
        self.assertIn('notification_type', ctx.exception.message_dict)

    def test_blank_title_rejected(self):
        """Empty title raises ValidationError."""
        with self.assertRaises(ValidationError) as ctx:
            create_request_notification(
                request=self.presc_request,
                recipient=self.patient_user,
                notification_type='REQUEST_SUBMITTED',
                title='',
                message='Valid Message'
            )
        self.assertIn('title', ctx.exception.message_dict)

    def test_whitespace_only_title_rejected(self):
        """Whitespace-only title raises ValidationError."""
        with self.assertRaises(ValidationError) as ctx:
            create_request_notification(
                request=self.presc_request,
                recipient=self.patient_user,
                notification_type='REQUEST_SUBMITTED',
                title='   \n\t   ',
                message='Valid Message'
            )
        self.assertIn('title', ctx.exception.message_dict)

    def test_blank_message_rejected(self):
        """Empty message raises ValidationError."""
        with self.assertRaises(ValidationError) as ctx:
            create_request_notification(
                request=self.presc_request,
                recipient=self.patient_user,
                notification_type='REQUEST_SUBMITTED',
                title='Valid Title',
                message=''
            )
        self.assertIn('message', ctx.exception.message_dict)

    def test_whitespace_only_message_rejected(self):
        """Whitespace-only message raises ValidationError."""
        with self.assertRaises(ValidationError) as ctx:
            create_request_notification(
                request=self.presc_request,
                recipient=self.patient_user,
                notification_type='REQUEST_SUBMITTED',
                title='Valid Title',
                message='    \t\n   '
            )
        self.assertIn('message', ctx.exception.message_dict)

    def test_title_exceeding_200_chars_rejected(self):
        """Title longer than 200 characters raises ValidationError."""
        long_title = 'A' * 201
        with self.assertRaises(ValidationError) as ctx:
            create_request_notification(
                request=self.presc_request,
                recipient=self.patient_user,
                notification_type='REQUEST_SUBMITTED',
                title=long_title,
                message='Valid Message'
            )
        self.assertIn('title', ctx.exception.message_dict)

    def test_title_exactly_200_chars_accepted(self):
        """Title with exactly 200 characters is accepted."""
        title_200 = 'A' * 200
        notif = create_request_notification(
            request=self.presc_request,
            recipient=self.patient_user,
            notification_type='REQUEST_SUBMITTED',
            title=title_200,
            message='Valid Message'
        )
        self.assertEqual(len(notif.title), 200)

    def test_invalid_request_rejected(self):
        """Non-PrescriptionRequest instance raises ValidationError."""
        with self.assertRaises(ValidationError) as ctx:
            create_request_notification(
                request="not-a-request",
                recipient=self.patient_user,
                notification_type='REQUEST_SUBMITTED',
                title='Valid Title',
                message='Valid Message'
            )
        self.assertIn('request', ctx.exception.message_dict)

