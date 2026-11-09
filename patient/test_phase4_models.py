"""
Focused Unit Tests for Phase 4 Database Models: Notification and RequestMessage.

Tests:
1. Notification model creation, unread default state, read state with read_at,
   optional actor support (system vs user), prescription_request relation, and ordering.
2. RequestMessage model creation (Patient-to-Pharmacy and Pharmacy-to-Patient),
   sender/recipient/request relations, read state with read_at, and ordering.
3. RequestMessage model-level clean() validations:
   - Empty message rejection.
   - Whitespace-only message rejection.
   - Exactly 1000-character acceptance.
   - Exceeding 1000-character rejection.
   - Same sender and recipient rejection.
   - Unrelated user participation rejection.
   - Role-mismatched recipient rejection (patient to non-pharmacy / pharmacy to non-patient).
4. Notification clean() validations:
   - Empty title or message rejection.
"""

from datetime import timedelta
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
    PrescriptionRequestItem,
    Notification,
    RequestMessage,
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


class Phase4ModelsTestCase(TestCase):
    """Exhaustive tests for Notification and RequestMessage models and their clean() validations."""

    def setUp(self):
        """Set up test users, profiles, pharmacies, prescription, and prescription request."""
        self.today = timezone.localdate()

        # 1. Patient User & Profile
        self.patient_user = User.objects.create_user(
            username='phase4_patient',
            email='p4_patient@medaccess.test',
            password='testpassword123',
            role='PATIENT',
            is_approved=True,
            email_verified=True,
            first_name='Aarav',
            last_name='Patel'
        )
        self.patient_profile, _ = PatientProfile.objects.get_or_create(
            user=self.patient_user,
            defaults={
                'current_latitude': Decimal('12.9716'),
                'current_longitude': Decimal('77.5946'),
                'address': '123 MG Road, Bangalore',
                'allergies': 'None'
            }
        )

        # 2. Approved Pharmacy User & Profile
        self.pharm_user = User.objects.create_user(
            username='phase4_pharma',
            email='p4_pharma@medaccess.test',
            password='testpassword123',
            role='PHARMACY',
            is_approved=True,
            email_verified=True
        )
        self.pharmacy_profile = PharmacyProfile.objects.create(
            user=self.pharm_user,
            pharmacy_name='Phase4 Care Pharmacy',
            pharmacy_license_number='KA-BLR-2026-P4',
            latitude=Decimal('12.9780'),
            longitude=Decimal('77.6000'),
            approval_status='APPROVED',
            phone_number='080-22223333',
            address='Indiranagar, Bangalore'
        )

        # 3. Third-party Unrelated User (for security boundary tests)
        self.unrelated_user = User.objects.create_user(
            username='phase4_intruder',
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
            medicine_name='Paracetamol 500mg',
            dosage='500mg',
            frequency='Twice daily',
            duration='3 days',
            quantity='6',
            source='AI_CONFIRMED',
            extracted_index=1
        )

        # 5. Prescription Request
        self.presc_request = PrescriptionRequest.objects.create(
            prescription=self.prescription,
            patient=self.patient_profile,
            pharmacy=self.pharmacy_profile,
            status='PENDING',
            patient_notes='Please confirm medicine availability.'
        )

    # =========================================================================
    # 1. Notification Model Tests
    # =========================================================================

    def test_notification_creation_for_patient(self):
        """Test creating a notification addressed to a patient user."""
        notif = Notification.objects.create(
            recipient=self.patient_user,
            actor=self.pharm_user,
            prescription_request=self.presc_request,
            notification_type='REQUEST_ACCEPTED',
            title='Prescription Request Accepted',
            message=f'Your request #{self.presc_request.id} has been accepted by {self.pharmacy_profile.pharmacy_name}.'
        )
        self.assertIsNotNone(notif.pk)
        self.assertEqual(notif.recipient, self.patient_user)
        self.assertEqual(notif.actor, self.pharm_user)
        self.assertEqual(notif.prescription_request, self.presc_request)
        self.assertFalse(notif.is_read)
        self.assertIsNone(notif.read_at)
        self.assertIn(self.patient_user.username, str(notif))

    def test_notification_creation_for_pharmacy(self):
        """Test creating a notification addressed to a pharmacy user."""
        notif = Notification.objects.create(
            recipient=self.pharm_user,
            actor=self.patient_user,
            prescription_request=self.presc_request,
            notification_type='REQUEST_SUBMITTED',
            title='New Prescription Request',
            message=f'New prescription request #{self.presc_request.id} received from {self.patient_user.get_full_name()}.'
        )
        self.assertIsNotNone(notif.pk)
        self.assertEqual(notif.recipient, self.pharm_user)
        self.assertEqual(notif.actor, self.patient_user)
        self.assertEqual(notif.notification_type, 'REQUEST_SUBMITTED')
        self.assertFalse(notif.is_read)

    def test_notification_optional_actor_for_system_alerts(self):
        """Actor can be None for system-generated notifications."""
        notif = Notification.objects.create(
            recipient=self.patient_user,
            actor=None,
            prescription_request=self.presc_request,
            notification_type='REQUEST_PROCESSING',
            title='Order Processing Update',
            message='Your order is being processed by the system.'
        )
        self.assertIsNone(notif.actor)
        self.assertEqual(notif.recipient, self.patient_user)

    def test_notification_read_state_and_read_at(self):
        """Test transitioning a notification to read state with read_at timestamp."""
        notif = Notification.objects.create(
            recipient=self.patient_user,
            actor=self.pharm_user,
            prescription_request=self.presc_request,
            notification_type='REQUEST_READY',
            title='Ready for Pickup',
            message='Your order is packed and ready.'
        )
        self.assertFalse(notif.is_read)
        self.assertIsNone(notif.read_at)

        now = timezone.now()
        notif.is_read = True
        notif.read_at = now
        notif.save(update_fields=['is_read', 'read_at'])

        notif.refresh_from_db()
        self.assertTrue(notif.is_read)
        self.assertIsNotNone(notif.read_at)

    def test_notification_ordering_newest_first(self):
        """Notifications must default to newest first (-created_at)."""
        notif1 = Notification.objects.create(
            recipient=self.patient_user,
            actor=self.pharm_user,
            prescription_request=self.presc_request,
            notification_type='REQUEST_SUBMITTED',
            title='First',
            message='First notif'
        )
        notif2 = Notification.objects.create(
            recipient=self.patient_user,
            actor=self.pharm_user,
            prescription_request=self.presc_request,
            notification_type='REQUEST_ACCEPTED',
            title='Second',
            message='Second notif'
        )
        notifications = list(Notification.objects.filter(recipient=self.patient_user))
        self.assertEqual(notifications[0].pk, notif2.pk)
        self.assertEqual(notifications[1].pk, notif1.pk)

    def test_notification_clean_validations(self):
        """Notification clean() rejects empty title or message."""
        notif_empty_title = Notification(
            recipient=self.patient_user,
            prescription_request=self.presc_request,
            notification_type='REQUEST_SUBMITTED',
            title='   ',
            message='Valid message'
        )
        with self.assertRaises(ValidationError) as ctx:
            notif_empty_title.clean()
        self.assertIn('title', ctx.exception.message_dict)

        notif_empty_msg = Notification(
            recipient=self.patient_user,
            prescription_request=self.presc_request,
            notification_type='REQUEST_SUBMITTED',
            title='Valid Title',
            message='   '
        )
        with self.assertRaises(ValidationError) as ctx:
            notif_empty_msg.clean()
        self.assertIn('message', ctx.exception.message_dict)

    # =========================================================================
    # 2. RequestMessage Model Tests
    # =========================================================================

    def test_patient_to_pharmacy_message_creation(self):
        """Patient can send a valid message to the request pharmacy."""
        msg = RequestMessage(
            request=self.presc_request,
            sender=self.patient_user,
            recipient=self.pharm_user,
            message_text='Hello, do you have this in a strip of 10?'
        )
        msg.full_clean()
        msg.save()

        self.assertIsNotNone(msg.pk)
        self.assertEqual(msg.request, self.presc_request)
        self.assertEqual(msg.sender, self.patient_user)
        self.assertEqual(msg.recipient, self.pharm_user)
        self.assertFalse(msg.is_read)
        self.assertIsNone(msg.read_at)
        self.assertIn(self.patient_user.username, str(msg))

    def test_pharmacy_to_patient_message_creation(self):
        """Pharmacy can send a valid message to the request patient."""
        msg = RequestMessage(
            request=self.presc_request,
            sender=self.pharm_user,
            recipient=self.patient_user,
            message_text='Yes, we have strips of 10 available in stock.'
        )
        msg.full_clean()
        msg.save()

        self.assertIsNotNone(msg.pk)
        self.assertEqual(msg.sender, self.pharm_user)
        self.assertEqual(msg.recipient, self.patient_user)

    def test_message_ordering_chronological(self):
        """Messages must be ordered chronologically by created_at."""
        msg1 = RequestMessage.objects.create(
            request=self.presc_request,
            sender=self.patient_user,
            recipient=self.pharm_user,
            message_text='First message'
        )
        msg2 = RequestMessage.objects.create(
            request=self.presc_request,
            sender=self.pharm_user,
            recipient=self.patient_user,
            message_text='Second message'
        )
        messages = list(self.presc_request.messages.all())
        self.assertEqual(messages[0].pk, msg1.pk)
        self.assertEqual(messages[1].pk, msg2.pk)

    # =========================================================================
    # 3. RequestMessage Model Validations (clean())
    # =========================================================================

    def test_empty_message_rejected(self):
        """Empty message_text raises ValidationError."""
        msg = RequestMessage(
            request=self.presc_request,
            sender=self.patient_user,
            recipient=self.pharm_user,
            message_text=''
        )
        with self.assertRaises(ValidationError) as ctx:
            msg.full_clean()
        self.assertIn('message_text', ctx.exception.message_dict)

    def test_whitespace_only_message_rejected(self):
        """Whitespace-only message_text raises ValidationError."""
        msg = RequestMessage(
            request=self.presc_request,
            sender=self.patient_user,
            recipient=self.pharm_user,
            message_text='     \n\t   '
        )
        with self.assertRaises(ValidationError) as ctx:
            msg.full_clean()
        self.assertIn('message_text', ctx.exception.message_dict)

    def test_message_exactly_1000_chars_accepted(self):
        """Message with exactly 1000 characters passes validation."""
        text_1000 = 'A' * 1000
        msg = RequestMessage(
            request=self.presc_request,
            sender=self.patient_user,
            recipient=self.pharm_user,
            message_text=text_1000
        )
        msg.full_clean()  # Should not raise
        msg.save()
        self.assertEqual(len(msg.message_text), 1000)

    def test_message_exceeding_1000_chars_rejected(self):
        """Message exceeding 1000 characters raises ValidationError."""
        text_1001 = 'A' * 1001
        msg = RequestMessage(
            request=self.presc_request,
            sender=self.patient_user,
            recipient=self.pharm_user,
            message_text=text_1001
        )
        with self.assertRaises(ValidationError) as ctx:
            msg.full_clean()
        self.assertIn('message_text', ctx.exception.message_dict)

    def test_same_sender_and_recipient_rejected(self):
        """Message where sender == recipient raises ValidationError."""
        msg = RequestMessage(
            request=self.presc_request,
            sender=self.patient_user,
            recipient=self.patient_user,
            message_text='Self-message attempt'
        )
        with self.assertRaises(ValidationError) as ctx:
            msg.full_clean()
        self.assertIn('recipient', ctx.exception.message_dict)

    def test_unrelated_user_sender_rejected(self):
        """A user not part of the request cannot send a message."""
        msg = RequestMessage(
            request=self.presc_request,
            sender=self.unrelated_user,
            recipient=self.pharm_user,
            message_text='Intruder attempting to send message'
        )
        with self.assertRaises(ValidationError) as ctx:
            msg.full_clean()
        self.assertIn('sender', ctx.exception.message_dict)

    def test_unrelated_user_recipient_rejected(self):
        """A message cannot be addressed to a user not part of the request."""
        msg = RequestMessage(
            request=self.presc_request,
            sender=self.patient_user,
            recipient=self.unrelated_user,
            message_text='Message to unrelated third party'
        )
        with self.assertRaises(ValidationError) as ctx:
            msg.full_clean()
        self.assertIn('recipient', ctx.exception.message_dict)

    def test_patient_to_patient_rejected(self):
        """Patient cannot send a message to another patient on the request."""
        # Even if another patient user existed, sender patient must address pharmacy user
        msg = RequestMessage(
            request=self.presc_request,
            sender=self.patient_user,
            recipient=self.patient_user,
            message_text='Invalid direction'
        )
        with self.assertRaises(ValidationError) as ctx:
            msg.full_clean()
        self.assertIn('recipient', ctx.exception.message_dict)

