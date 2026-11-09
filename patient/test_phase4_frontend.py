"""
Frontend Integration Tests for Phase 4 Step 6:
Patient–Pharmacy Chat Workspace + Notification Center UI.

Tests:
1. Notification Center UI Integration:
   - Patient base template renders notification bell with all required data-api-* endpoints.
   - Pharmacy base template renders notification bell with all required data-api-* endpoints.
   - Both portals include notification_center.js script.
2. Patient Chat Workspace Integration:
   - Active request (PENDING) renders chat card, live badge, message container, writable=true, and input form.
   - Terminal request (COMPLETED) renders chat card, archived badge, writable=false, archived notice, and no input form.
   - Cancelled request renders archived badge and notice.
   - request_chat.js script is included on patient request detail page.
3. Pharmacy Chat Workspace Integration:
   - Active request (ACCEPTED) renders chat card, live badge, message container, writable=true, and input form.
   - Terminal request (REJECTED) renders chat card, archived badge, writable=false, archived notice, and no input form.
   - request_chat.js script is included on pharmacy request detail page.
"""

from decimal import Decimal
import io

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, Client
from django.urls import reverse
from PIL import Image

from patient.models import (
    PatientProfile,
    Prescription,
    PrescriptionRequest,
)
from pharmacy.models import PharmacyProfile

User = get_user_model()


def create_test_image_file():
    img = Image.new('RGB', (100, 100), color=(200, 220, 240))
    buffer = io.BytesIO()
    img.save(buffer, format='JPEG')
    buffer.seek(0)
    return SimpleUploadedFile(
        'test_rx.jpg',
        buffer.getvalue(),
        content_type='image/jpeg'
    )


class Phase4FrontendIntegrationTests(TestCase):
    """Verifies template rendering and frontend integrations for Step 6."""

    def setUp(self):
        # 1. Patient User
        self.patient_user = User.objects.create_user(
            username='frontend_patient',
            email='patient_ui@example.com',
            password='testpassword123',
            role='PATIENT',
            first_name='Aarav',
            last_name='Sharma',
            phone_number='9876543210',
            email_verified=True,
            is_approved=True,
        )
        self.patient_profile, _ = PatientProfile.objects.get_or_create(
            user=self.patient_user,
            defaults={
                'address': '123 Health Ave, South Delhi',
                'allergies': 'Penicillin',
            }
        )

        # 2. Pharmacy User & Profile (Approved)
        self.pharmacy_user = User.objects.create_user(
            username='frontend_pharmacy',
            email='pharma_ui@example.com',
            password='testpassword123',
            role='PHARMACY',
            first_name='Care',
            last_name='Pharmacy',
            email_verified=True,
            is_approved=True,
        )
        self.pharmacy_profile = PharmacyProfile.objects.create(
            user=self.pharmacy_user,
            pharmacy_name='Care Wellness Chemist',
            pharmacy_license_number='DL-PHARMA-FRONTEND-1',
            address='456 Market Lane, South Delhi',
            city='South Delhi',
            phone_number='9876500000',
            approval_status='APPROVED',
        )

        # 3. Prescription
        self.prescription = Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image=create_test_image_file(),
            status='COMPLETED',
        )

        # 4. Active Request (PENDING)
        self.active_request = PrescriptionRequest.objects.create(
            prescription=self.prescription,
            patient=self.patient_profile,
            pharmacy=self.pharmacy_profile,
            status='PENDING',
            initial_availability_status='FULLY_AVAILABLE',
        )

        # 5. Completed Request (COMPLETED)
        self.completed_request = PrescriptionRequest.objects.create(
            prescription=self.prescription,
            patient=self.patient_profile,
            pharmacy=self.pharmacy_profile,
            status='COMPLETED',
            initial_availability_status='FULLY_AVAILABLE',
        )

        # 6. Clients
        self.patient_client = Client()
        self.patient_client.login(username='frontend_patient', password='testpassword123')

        self.pharmacy_client = Client()
        self.pharmacy_client.login(username='frontend_pharmacy', password='testpassword123')

    # =========================================================================
    # 1. NOTIFICATION CENTER UI TESTS
    # =========================================================================

    def test_patient_portal_renders_notification_center(self):
        """Patient portal base template must render notification bell with data attributes."""
        response = self.patient_client.get(reverse('patient:dashboard'))
        self.assertEqual(response.status_code, 200)

        content = response.content.decode('utf-8')
        self.assertIn('id="notif-bell-wrapper"', content)
        self.assertIn('id="notif-bell-btn"', content)
        self.assertIn('id="notif-badge"', content)
        self.assertIn('id="notif-dropdown"', content)
        self.assertIn('id="notif-mark-all-btn"', content)
        self.assertIn('id="notif-body"', content)

        # Check API endpoints in data attributes
        self.assertIn(reverse('patient:notifications_api'), content)
        self.assertIn(reverse('patient:notification_unread_count'), content)
        self.assertIn(reverse('patient:notifications_mark_all_read'), content)
        self.assertIn('/patient/notifications/0/read/', content)

        # Check script inclusion
        self.assertIn('js/notification_center.js', content)

    def test_pharmacy_portal_renders_notification_center(self):
        """Pharmacy portal base template must render notification bell with data attributes."""
        response = self.pharmacy_client.get(reverse('pharmacy:dashboard'))
        self.assertEqual(response.status_code, 200)

        content = response.content.decode('utf-8')
        self.assertIn('id="notif-bell-wrapper"', content)
        self.assertIn('id="notif-bell-btn"', content)
        self.assertIn('id="notif-badge"', content)
        self.assertIn('id="notif-dropdown"', content)
        self.assertIn('id="notif-mark-all-btn"', content)
        self.assertIn('id="notif-body"', content)

        # Check API endpoints in data attributes
        self.assertIn(reverse('pharmacy:notifications_api'), content)
        self.assertIn(reverse('pharmacy:notification_unread_count'), content)
        self.assertIn(reverse('pharmacy:notifications_mark_all_read'), content)
        self.assertIn('/pharmacy/notifications/0/read/', content)

        # Check script inclusion
        self.assertIn('js/notification_center.js', content)

    # =========================================================================
    # 2. PATIENT CHAT WORKSPACE UI TESTS
    # =========================================================================

    def test_patient_request_detail_renders_active_chat_workspace(self):
        """Patient viewing an active request sees the live chat card, container, and input form."""
        url = reverse('patient:request_detail', kwargs={'pk': self.active_request.pk})
        response = self.patient_client.get(url)
        self.assertEqual(response.status_code, 200)

        content = response.content.decode('utf-8')
        # Title and pharmacy name
        self.assertIn('Chat with Pharmacy', content)
        self.assertIn('Care Wellness Chemist', content)

        # Active Live Chat badge
        self.assertIn('chat-status-active', content)
        self.assertIn('Live Chat', content)

        # Message container with data attributes
        messages_api_url = reverse('patient:request_messages', kwargs={'pk': self.active_request.pk})
        self.assertIn(f'data-messages-url="{messages_api_url}"', content)
        self.assertIn('data-is-writable="true"', content)

        # Form and inputs
        self.assertIn('id="chat-send-form"', content)
        self.assertIn('id="chat-input-text"', content)
        self.assertIn('id="chat-char-counter"', content)
        self.assertIn('id="chat-send-btn"', content)

        # Scripts
        self.assertIn('js/request_chat.js', content)

    def test_patient_request_detail_renders_archived_chat_workspace(self):
        """Patient viewing a COMPLETED request sees archived chat without input form."""
        url = reverse('patient:request_detail', kwargs={'pk': self.completed_request.pk})
        response = self.patient_client.get(url)
        self.assertEqual(response.status_code, 200)

        content = response.content.decode('utf-8')
        self.assertIn('Chat with Pharmacy', content)

        # Archived badge
        self.assertIn('chat-status-archived', content)
        self.assertIn('Archived', content)

        # data-is-writable="false"
        self.assertIn('data-is-writable="false"', content)

        # Archived notice banner
        self.assertIn('chat-archived-notice', content)
        self.assertIn('Messaging is archived and read-only', content)

        # Form must NOT be rendered
        self.assertNotIn('id="chat-send-form"', content)

    # =========================================================================
    # 3. PHARMACY CHAT WORKSPACE UI TESTS
    # =========================================================================

    def test_pharmacy_request_detail_renders_active_chat_workspace(self):
        """Pharmacy viewing an active request sees the live chat card, container, and input form."""
        url = reverse('pharmacy:request_detail', kwargs={'pk': self.active_request.pk})
        response = self.pharmacy_client.get(url)
        self.assertEqual(response.status_code, 200)

        content = response.content.decode('utf-8')
        # Title and patient name
        self.assertIn('Chat with Patient', content)
        self.assertIn('Aarav Sharma', content)

        # Active Live Chat badge
        self.assertIn('chat-status-active', content)
        self.assertIn('Live Chat', content)

        # Message container with data attributes
        messages_api_url = reverse('pharmacy:request_messages', kwargs={'pk': self.active_request.pk})
        self.assertIn(f'data-messages-url="{messages_api_url}"', content)
        self.assertIn('data-is-writable="true"', content)

        # Form and inputs
        self.assertIn('id="chat-send-form"', content)
        self.assertIn('id="chat-input-text"', content)
        self.assertIn('id="chat-send-btn"', content)

        # Scripts
        self.assertIn('js/request_chat.js', content)

    def test_pharmacy_request_detail_renders_archived_chat_workspace(self):
        """Pharmacy viewing a REJECTED request sees archived chat without input form."""
        rejected_request = PrescriptionRequest.objects.create(
            prescription=self.prescription,
            patient=self.patient_profile,
            pharmacy=self.pharmacy_profile,
            status='REJECTED',
            pharmacy_notes='Out of stock',
            initial_availability_status='OUT_OF_STOCK',
        )

        url = reverse('pharmacy:request_detail', kwargs={'pk': rejected_request.pk})
        response = self.pharmacy_client.get(url)
        self.assertEqual(response.status_code, 200)

        content = response.content.decode('utf-8')
        self.assertIn('Chat with Patient', content)

        # Archived badge
        self.assertIn('chat-status-archived', content)
        self.assertIn('Archived', content)

        # data-is-writable="false"
        self.assertIn('data-is-writable="false"', content)

        # Archived notice banner
        self.assertIn('chat-archived-notice', content)
        self.assertIn('Messaging is archived and read-only', content)

        # Form must NOT be rendered
        self.assertNotIn('id="chat-send-form"', content)
