"""
Focused Integration Tests for Phase 4 Step 4: Two-Way Messaging Backend.

Tests:
1. Access and Isolation:
   - Patient can retrieve their own request conversation.
   - Selected pharmacy can retrieve its conversation.
   - Unrelated patient is denied (404).
   - Unrelated pharmacy is denied (404).
   - Cross-pharmacy access is denied (404).
   - Unauthenticated access is denied (401).
2. Sending Messages:
   - Patient can send a message to the selected pharmacy.
   - Pharmacy can reply to the patient.
   - Sender and recipient are derived strictly server-side.
   - Client-supplied sender/recipient IDs cannot override server-side identity.
   - Blank and whitespace-only message rejection (400).
   - Exactly 1000 characters accepted (201).
   - More than 1000 characters rejected (400).
   - Message saved with correct request relationship.
3. Status Restrictions:
   - Messaging works in all four writable statuses (PENDING, ACCEPTED, PROCESSING, READY_FOR_PICKUP).
   - Completed request is read-only (403 on POST).
   - Rejected request is read-only (403 on POST).
   - Cancelled request is read-only (403 on POST).
   - Historical messages remain retrievable via GET for archived requests.
4. Read Receipts and Retrieval:
   - Messages are returned in chronological order.
   - after_id returns only newer messages.
   - Maximum response size is 50.
   - Retrieving messages marks only current user's received unread messages as read.
   - Sender's own messages are not marked as received/read by sender.
5. Notifications and Transaction Safety:
   - Successful patient message creates a pharmacy NEW_MESSAGE notification.
   - Successful pharmacy message creates a patient NEW_MESSAGE notification.
   - Sender does not receive their own message notification.
   - Failed message submission creates zero notifications.
   - Atomic rollback: if notification creation fails, the message is not saved.
"""

from decimal import Decimal
import io
import json
from unittest.mock import patch

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
    RequestMessage,
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


class RequestMessagingTestCase(TestCase):
    """Exhaustive tests for request-specific two-way messaging backend."""

    def setUp(self):
        """Set up test users, profiles, pharmacies, prescription, and prescription requests."""
        self.client = Client()
        self.today = timezone.localdate()

        # 1. Patient User & Profile
        self.patient_user = User.objects.create_user(
            username='msg_patient',
            email='msg_patient@medaccess.test',
            password='testpassword123',
            role='PATIENT',
            is_approved=True,
            email_verified=True,
            first_name='Rohan',
            last_name='Verma'
        )
        self.patient_profile, _ = PatientProfile.objects.get_or_create(
            user=self.patient_user,
            defaults={
                'current_latitude': Decimal('12.9716'),
                'current_longitude': Decimal('77.5946'),
                'address': 'Koramangala, Bangalore'
            }
        )

        # 2. Approved Pharmacy 1
        self.pharm_user_1 = User.objects.create_user(
            username='msg_pharma_1',
            email='msg_pharma1@medaccess.test',
            password='testpassword123',
            role='PHARMACY',
            is_approved=True,
            email_verified=True
        )
        self.pharmacy_1 = PharmacyProfile.objects.create(
            user=self.pharm_user_1,
            pharmacy_name='Apollo Pharmacy 1',
            pharmacy_license_number='KA-BLR-2026-M1',
            latitude=Decimal('12.9780'),
            longitude=Decimal('77.6000'),
            approval_status='APPROVED',
            phone_number='080-11112222',
            address='100 Feet Rd, Bangalore'
        )

        # 3. Approved Pharmacy 2 (Unrelated to Request 1)
        self.pharm_user_2 = User.objects.create_user(
            username='msg_pharma_2',
            email='msg_pharma2@medaccess.test',
            password='testpassword123',
            role='PHARMACY',
            is_approved=True,
            email_verified=True
        )
        self.pharmacy_2 = PharmacyProfile.objects.create(
            user=self.pharm_user_2,
            pharmacy_name='MedPlus Pharmacy 2',
            pharmacy_license_number='KA-BLR-2026-M2',
            latitude=Decimal('12.9352'),
            longitude=Decimal('77.6245'),
            approval_status='APPROVED',
            phone_number='080-33334444',
            address='80 Feet Rd, Bangalore'
        )

        # 4. Third-party Unrelated Patient
        self.unrelated_patient_user = User.objects.create_user(
            username='msg_intruder_patient',
            email='intruder_p@medaccess.test',
            password='testpassword123',
            role='PATIENT',
            is_approved=True,
            email_verified=True
        )
        self.unrelated_patient_profile, _ = PatientProfile.objects.get_or_create(
            user=self.unrelated_patient_user,
            defaults={
                'current_latitude': Decimal('12.9716'),
                'current_longitude': Decimal('77.5946'),
            }
        )

        # 5. Prescription
        self.prescription = Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image=create_test_image_file(),
            status='COMPLETED'
        )

        # 6. Active PrescriptionRequest (between patient_user and pharmacy_1)
        self.presc_request = PrescriptionRequest.objects.create(
            prescription=self.prescription,
            patient=self.patient_profile,
            pharmacy=self.pharmacy_1,
            status='PENDING',
            patient_notes='Initial request note.'
        )

        self.messages_url = reverse('patient:request_messages', kwargs={'pk': self.presc_request.pk})
        self.pharm_messages_url = reverse('pharmacy:request_messages', kwargs={'pk': self.presc_request.pk})

    # =========================================================================
    # 1. Access Control & Multi-Tenant Isolation
    # =========================================================================

    def test_unauthenticated_access_denied(self):
        """Unauthenticated GET and POST requests receive 401."""
        resp_get = self.client.get(self.messages_url)
        self.assertEqual(resp_get.status_code, 401)

        resp_post = self.client.post(self.messages_url, data={'message_text': 'Hello'})
        self.assertEqual(resp_post.status_code, 401)

    def test_patient_can_retrieve_own_conversation(self):
        """Patient participant can retrieve message thread."""
        self.client.force_login(self.patient_user)
        resp = self.client.get(self.messages_url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['request_id'], self.presc_request.id)
        self.assertTrue(data['is_writable'])

    def test_selected_pharmacy_can_retrieve_conversation(self):
        """Assigned pharmacy participant can retrieve message thread."""
        self.client.force_login(self.pharm_user_1)
        resp = self.client.get(self.pharm_messages_url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['status'], 'success')

    def test_unrelated_patient_denied_with_404(self):
        """Unrelated patient receives 404 and cannot access conversation."""
        self.client.force_login(self.unrelated_patient_user)
        resp = self.client.get(self.messages_url)
        self.assertEqual(resp.status_code, 404)

        resp_post = self.client.post(self.messages_url, data={'message_text': 'Intruder text'})
        self.assertEqual(resp_post.status_code, 404)

    def test_unrelated_pharmacy_denied_with_404(self):
        """Pharmacy 2 receives 404 for Pharmacy 1's request."""
        self.client.force_login(self.pharm_user_2)
        resp = self.client.get(self.messages_url)
        self.assertEqual(resp.status_code, 404)

        resp_post = self.client.post(self.messages_url, data={'message_text': 'Cross pharmacy text'})
        self.assertEqual(resp_post.status_code, 404)

    def test_unapproved_pharmacy_denied(self):
        """Unapproved pharmacy account receives 403."""
        self.pharmacy_1.approval_status = 'PENDING'
        self.pharmacy_1.save(update_fields=['approval_status'])

        self.client.force_login(self.pharm_user_1)
        resp = self.client.get(self.pharm_messages_url)
        self.assertEqual(resp.status_code, 403)

    # =========================================================================
    # 2. Sending Messages
    # =========================================================================

    def test_patient_sends_message_successfully(self):
        """Patient can send a valid message; sender and recipient are derived correctly."""
        self.client.force_login(self.patient_user)
        resp = self.client.post(
            self.messages_url,
            data={'message_text': 'Hello, do you have Paracetamol in stock?'}
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['message']['message_text'], 'Hello, do you have Paracetamol in stock?')
        self.assertTrue(data['message']['is_current_user'])

        # Verify DB record
        msg = RequestMessage.objects.get(id=data['message']['id'])
        self.assertEqual(msg.request, self.presc_request)
        self.assertEqual(msg.sender, self.patient_user)
        self.assertEqual(msg.recipient, self.pharm_user_1)
        self.assertFalse(msg.is_read)

    def test_pharmacy_replies_to_patient_successfully(self):
        """Pharmacy can reply to the patient; sender and recipient are derived correctly."""
        self.client.force_login(self.pharm_user_1)
        resp = self.client.post(
            self.pharm_messages_url,
            data={'message_text': 'Yes, we have 100 units available in stock.'}
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data['status'], 'success')

        msg = RequestMessage.objects.get(id=data['message']['id'])
        self.assertEqual(msg.request, self.presc_request)
        self.assertEqual(msg.sender, self.pharm_user_1)
        self.assertEqual(msg.recipient, self.patient_user)

    def test_client_supplied_sender_recipient_ids_ignored(self):
        """Client cannot spoof sender or recipient IDs in the POST payload."""
        self.client.force_login(self.patient_user)
        resp = self.client.post(
            self.messages_url,
            data={
                'message_text': 'Legitimate text',
                'sender_id': self.pharm_user_1.id,  # Spoofing attempt
                'recipient_id': self.unrelated_patient_user.id,  # Spoofing attempt
            }
        )
        self.assertEqual(resp.status_code, 201)
        msg = RequestMessage.objects.latest('created_at')
        # Server must override spoofed IDs
        self.assertEqual(msg.sender, self.patient_user)
        self.assertEqual(msg.recipient, self.pharm_user_1)

    def test_json_payload_supported(self):
        """POST endpoint supports application/json content-type."""
        self.client.force_login(self.patient_user)
        resp = self.client.post(
            self.messages_url,
            data=json.dumps({'message_text': 'JSON payload test message'}),
            content_type='application/json'
        )
        self.assertEqual(resp.status_code, 201)
        msg = RequestMessage.objects.latest('created_at')
        self.assertEqual(msg.message_text, 'JSON payload test message')

    def test_blank_message_rejected(self):
        """Empty message_text returns 400."""
        self.client.force_login(self.patient_user)
        resp = self.client.post(self.messages_url, data={'message_text': ''})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('empty', resp.json().get('error', '').lower())

    def test_whitespace_only_message_rejected(self):
        """Whitespace-only message_text returns 400."""
        self.client.force_login(self.patient_user)
        resp = self.client.post(self.messages_url, data={'message_text': '   \n\t   '})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('empty', resp.json().get('error', '').lower())

    def test_message_exactly_1000_chars_accepted(self):
        """Message of exactly 1000 characters is accepted with 201."""
        self.client.force_login(self.patient_user)
        resp = self.client.post(self.messages_url, data={'message_text': 'A' * 1000})
        self.assertEqual(resp.status_code, 201)
        msg = RequestMessage.objects.latest('created_at')
        self.assertEqual(len(msg.message_text), 1000)

    def test_message_exceeding_1000_chars_rejected(self):
        """Message exceeding 1000 characters returns 400."""
        self.client.force_login(self.patient_user)
        resp = self.client.post(self.messages_url, data={'message_text': 'A' * 1001})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('1000', resp.json().get('error', '').lower())

    # =========================================================================
    # 3. Status Restrictions & Read-Only Archives
    # =========================================================================

    def test_messaging_writable_in_all_active_statuses(self):
        """Messaging is writable in PENDING, ACCEPTED, PROCESSING, READY_FOR_PICKUP."""
        self.client.force_login(self.patient_user)
        active_statuses = ['PENDING', 'ACCEPTED', 'PROCESSING', 'READY_FOR_PICKUP']

        for status in active_statuses:
            self.presc_request.status = status
            self.presc_request.save(update_fields=['status'])

            resp = self.client.post(
                self.messages_url,
                data={'message_text': f'Message in {status} status'}
            )
            self.assertEqual(resp.status_code, 201, f"Failed in status {status}")

    def test_completed_request_is_read_only(self):
        """COMPLETED request rejects POST with 403, but allows GET retrieval."""
        self.presc_request.status = 'COMPLETED'
        self.presc_request.save(update_fields=['status'])

        self.client.force_login(self.patient_user)
        resp_post = self.client.post(self.messages_url, data={'message_text': 'Post to completed'})
        self.assertEqual(resp_post.status_code, 403)

        resp_get = self.client.get(self.messages_url)
        self.assertEqual(resp_get.status_code, 200)
        self.assertFalse(resp_get.json()['is_writable'])

    def test_rejected_request_is_read_only(self):
        """REJECTED request rejects POST with 403."""
        self.presc_request.status = 'REJECTED'
        self.presc_request.save(update_fields=['status'])

        self.client.force_login(self.patient_user)
        resp_post = self.client.post(self.messages_url, data={'message_text': 'Post to rejected'})
        self.assertEqual(resp_post.status_code, 403)

    def test_cancelled_request_is_read_only(self):
        """CANCELLED request rejects POST with 403."""
        self.presc_request.status = 'CANCELLED'
        self.presc_request.save(update_fields=['status'])

        self.client.force_login(self.patient_user)
        resp_post = self.client.post(self.messages_url, data={'message_text': 'Post to cancelled'})
        self.assertEqual(resp_post.status_code, 403)

    # =========================================================================
    # 4. Read Receipts & Chronological Retrieval
    # =========================================================================

    def test_messages_returned_chronologically_and_pagination_limit(self):
        """Messages returned in chronological order, capped at 50."""
        # Create 55 messages
        for i in range(55):
            RequestMessage.objects.create(
                request=self.presc_request,
                sender=self.patient_user,
                recipient=self.pharm_user_1,
                message_text=f"Batch msg #{i}"
            )

        self.client.force_login(self.patient_user)
        resp = self.client.get(self.messages_url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['count'], 50)
        self.assertEqual(len(data['messages']), 50)
        self.assertEqual(data['messages'][0]['message_text'], 'Batch msg #0')
        self.assertEqual(data['messages'][49]['message_text'], 'Batch msg #49')

    def test_after_id_incremental_polling(self):
        """after_id returns only messages newer than the specified ID."""
        msg1 = RequestMessage.objects.create(
            request=self.presc_request,
            sender=self.patient_user,
            recipient=self.pharm_user_1,
            message_text="Msg 1"
        )
        msg2 = RequestMessage.objects.create(
            request=self.presc_request,
            sender=self.pharm_user_1,
            recipient=self.patient_user,
            message_text="Msg 2"
        )

        self.client.force_login(self.patient_user)
        resp = self.client.get(f"{self.messages_url}?after_id={msg1.id}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['messages'][0]['id'], msg2.id)

    def test_retrieving_messages_marks_only_received_unread_messages_as_read(self):
        """
        When patient retrieves messages:
        - Messages where recipient == patient become is_read=True.
        - Messages where sender == patient remain is_read=False until pharmacy reads them.
        """
        # Msg 1: sent by pharmacy to patient (unread)
        msg_from_pharm = RequestMessage.objects.create(
            request=self.presc_request,
            sender=self.pharm_user_1,
            recipient=self.patient_user,
            message_text="From pharmacy to patient",
            is_read=False
        )
        # Msg 2: sent by patient to pharmacy (unread)
        msg_from_patient = RequestMessage.objects.create(
            request=self.presc_request,
            sender=self.patient_user,
            recipient=self.pharm_user_1,
            message_text="From patient to pharmacy",
            is_read=False
        )

        # Patient reads thread
        self.client.force_login(self.patient_user)
        resp = self.client.get(self.messages_url)
        self.assertEqual(resp.status_code, 200)

        msg_from_pharm.refresh_from_db()
        msg_from_patient.refresh_from_db()

        # Patient's received message marked as read
        self.assertTrue(msg_from_pharm.is_read)
        self.assertIsNotNone(msg_from_pharm.read_at)

        # Patient's own sent message must NOT be marked read by sender's read
        self.assertFalse(msg_from_patient.is_read)
        self.assertIsNone(msg_from_patient.read_at)

    # =========================================================================
    # 5. Notifications and Transaction Safety
    # =========================================================================

    def test_patient_message_creates_pharmacy_new_message_notification(self):
        """Sending a message creates a NEW_MESSAGE notification for the recipient."""
        self.client.force_login(self.patient_user)
        init_count = Notification.objects.count()

        resp = self.client.post(self.messages_url, data={'message_text': 'Notify test message'})
        self.assertEqual(resp.status_code, 201)

        self.assertEqual(Notification.objects.count(), init_count + 1)
        notif = Notification.objects.latest('created_at')
        self.assertEqual(notif.recipient, self.pharm_user_1)
        self.assertEqual(notif.actor, self.patient_user)
        self.assertEqual(notif.notification_type, 'NEW_MESSAGE')
        self.assertEqual(notif.title, 'New Message')
        self.assertIn(str(self.presc_request.id), notif.message)

    def test_pharmacy_message_creates_patient_new_message_notification(self):
        """Pharmacy sending a message creates a NEW_MESSAGE notification for patient."""
        self.client.force_login(self.pharm_user_1)
        init_count = Notification.objects.count()

        resp = self.client.post(self.pharm_messages_url, data={'message_text': 'Pharma notify test'})
        self.assertEqual(resp.status_code, 201)

        self.assertEqual(Notification.objects.count(), init_count + 1)
        notif = Notification.objects.latest('created_at')
        self.assertEqual(notif.recipient, self.patient_user)
        self.assertEqual(notif.actor, self.pharm_user_1)
        self.assertEqual(notif.notification_type, 'NEW_MESSAGE')

    def test_sender_does_not_receive_own_notification(self):
        """Sender user never receives a notification for their own sent message."""
        self.client.force_login(self.patient_user)
        resp = self.client.post(self.messages_url, data={'message_text': 'No self notification'})
        self.assertEqual(resp.status_code, 201)

        patient_notifs = Notification.objects.filter(
            recipient=self.patient_user,
            notification_type='NEW_MESSAGE'
        )
        self.assertEqual(patient_notifs.count(), 0)

    def test_failed_message_creates_zero_notifications(self):
        """Validation failure on message submission generates zero notifications."""
        self.client.force_login(self.patient_user)
        init_count = Notification.objects.count()

        resp = self.client.post(self.messages_url, data={'message_text': '   '})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Notification.objects.count(), init_count)

    def test_atomic_rollback_if_notification_fails(self):
        """If notification service raises an exception, message persistence rolls back."""
        self.client.force_login(self.patient_user)
        init_msg_count = RequestMessage.objects.count()

        with patch(
            'patient.messaging_views.create_request_notification',
            side_effect=RuntimeError("Simulated notification service failure")
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(self.messages_url, data={'message_text': 'Will rollback'})

        # Verify message was NOT saved in database
        self.assertEqual(RequestMessage.objects.count(), init_msg_count)

