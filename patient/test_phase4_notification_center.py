"""
Focused Integration Tests for Phase 4 Step 5: Notification Center Backend APIs.

Tests:
1. Notification Listing (GET):
   - Patient receives only their own notifications.
   - Pharmacy receives only their own notifications.
   - Unrelated user's notifications never appear.
   - Newest-first ordering (-created_at).
   - Pagination works (page 1 has 20, page 2 has remainder).
   - Pagination metadata is correct (total_count, page, num_pages, has_next, has_previous).
   - Navigation URL resolves to correct role-specific request detail page:
     * Patient -> /patient/requests/<id>/
     * Pharmacy -> /pharmacy/requests/<id>/
   - Optional filter=unread works.
2. Unread Count (GET):
   - Correct patient unread count.
   - Correct pharmacy unread count.
   - Read notifications are excluded from unread count.
   - Another user's unread notifications are excluded.
   - Returns 0 when user has zero notifications.
3. Mark One as Read (POST):
   - Recipient can mark their notification as read (sets is_read=True, read_at timestamp).
   - Repeated requests preserve the original read_at timestamp.
   - Another user cannot mark the notification as read (returns 404).
   - Non-existent notification ID returns 404.
4. Mark All as Read (POST):
   - Only the authenticated user's unread notifications are updated.
   - Other users' notifications remain untouched.
   - Already-read notifications are not altered.
   - Repeated requests return updated_count = 0.
5. Security and Access Control:
   - Unauthenticated access receives 401.
   - Unapproved pharmacy receives 403.
   - Cross-user notification IDs return 404.
   - HTTP method restrictions enforced (GET only on list/count, POST only on mark read).
"""

from decimal import Decimal
import io
import time

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
    Notification,
)
from pharmacy.models import PharmacyProfile

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


class NotificationCenterBackendTestCase(TestCase):
    """Exhaustive tests for Notification Center backend APIs."""

    def setUp(self):
        """Set up test users, profiles, pharmacies, prescription, and prescription requests."""
        self.client = Client()
        self.today = timezone.localdate()

        # 1. Patient User & Profile
        self.patient_user = User.objects.create_user(
            username='center_patient',
            email='center_patient@medaccess.test',
            password='testpassword123',
            role='PATIENT',
            is_approved=True,
            email_verified=True,
            first_name='Priya',
            last_name='Sharma'
        )
        self.patient_profile, _ = PatientProfile.objects.get_or_create(
            user=self.patient_user,
            defaults={
                'current_latitude': Decimal('12.9716'),
                'current_longitude': Decimal('77.5946'),
                'address': 'MG Road, Bangalore'
            }
        )

        # 2. Approved Pharmacy User & Profile
        self.pharm_user = User.objects.create_user(
            username='center_pharma',
            email='center_pharma@medaccess.test',
            password='testpassword123',
            role='PHARMACY',
            is_approved=True,
            email_verified=True
        )
        self.pharmacy_profile = PharmacyProfile.objects.create(
            user=self.pharm_user,
            pharmacy_name='Center Apollo Pharmacy',
            pharmacy_license_number='KA-BLR-2026-NC1',
            latitude=Decimal('12.9780'),
            longitude=Decimal('77.6000'),
            approval_status='APPROVED',
            phone_number='080-77778888',
            address='100 Feet Rd, Bangalore'
        )

        # 3. Third-party Unrelated Patient
        self.unrelated_patient_user = User.objects.create_user(
            username='center_intruder',
            email='intruder@medaccess.test',
            password='testpassword123',
            role='PATIENT',
            is_approved=True,
            email_verified=True
        )

        # 4. Prescription & Request
        self.prescription = Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image=create_test_image_file(),
            status='COMPLETED'
        )
        self.presc_request = PrescriptionRequest.objects.create(
            prescription=self.prescription,
            patient=self.patient_profile,
            pharmacy=self.pharmacy_profile,
            status='PENDING'
        )

        # URL Endpoints
        self.patient_list_url = reverse('patient:notifications_api')
        self.patient_unread_url = reverse('patient:notification_unread_count')
        self.patient_mark_all_url = reverse('patient:notifications_mark_all_read')

        self.pharm_list_url = reverse('pharmacy:notifications_api')
        self.pharm_unread_url = reverse('pharmacy:notification_unread_count')
        self.pharm_mark_all_url = reverse('pharmacy:notifications_mark_all_read')

    # =========================================================================
    # 1. Access Control & Security
    # =========================================================================

    def test_unauthenticated_requests_denied(self):
        """Unauthenticated requests to all notification endpoints receive 401."""
        endpoints = [
            ('GET', self.patient_list_url),
            ('GET', self.patient_unread_url),
            ('POST', self.patient_mark_all_url),
            ('POST', reverse('patient:notification_mark_read', kwargs={'pk': 1})),
        ]
        for method, url in endpoints:
            if method == 'GET':
                resp = self.client.get(url)
            else:
                resp = self.client.post(url)
            self.assertEqual(resp.status_code, 401, f"Failed for {method} {url}")

    def test_unapproved_pharmacy_denied(self):
        """Unapproved pharmacy account receives 403 on notification endpoints."""
        self.pharmacy_profile.approval_status = 'PENDING'
        self.pharmacy_profile.save(update_fields=['approval_status'])

        self.client.force_login(self.pharm_user)
        resp_list = self.client.get(self.pharm_list_url)
        self.assertEqual(resp_list.status_code, 403)

        resp_count = self.client.get(self.pharm_unread_url)
        self.assertEqual(resp_count.status_code, 403)

        resp_mark = self.client.post(self.pharm_mark_all_url)
        self.assertEqual(resp_mark.status_code, 403)

    def test_http_methods_enforced(self):
        """POST on list/count endpoints and GET on mark-read endpoints receive 405."""
        self.client.force_login(self.patient_user)

        # List endpoint is GET only
        resp_post_list = self.client.post(self.patient_list_url)
        self.assertEqual(resp_post_list.status_code, 405)

        # Count endpoint is GET only
        resp_post_count = self.client.post(self.patient_unread_url)
        self.assertEqual(resp_post_count.status_code, 405)

        # Mark-all endpoint is POST only
        resp_get_mark_all = self.client.get(self.patient_mark_all_url)
        self.assertEqual(resp_get_mark_all.status_code, 405)

    # =========================================================================
    # 2. Notification Listing & Isolation
    # =========================================================================

    def test_user_retrieves_only_their_own_notifications(self):
        """Patient sees only patient notifications; Pharmacy sees only pharmacy notifications."""
        notif_patient = Notification.objects.create(
            recipient=self.patient_user,
            actor=self.pharm_user,
            prescription_request=self.presc_request,
            notification_type='REQUEST_ACCEPTED',
            title='Accepted for Patient',
            message='Patient alert'
        )
        notif_pharma = Notification.objects.create(
            recipient=self.pharm_user,
            actor=self.patient_user,
            prescription_request=self.presc_request,
            notification_type='REQUEST_SUBMITTED',
            title='Submitted for Pharmacy',
            message='Pharmacy alert'
        )

        # 1. Patient retrieval
        self.client.force_login(self.patient_user)
        resp_p = self.client.get(self.patient_list_url)
        self.assertEqual(resp_p.status_code, 200)
        data_p = resp_p.json()
        ids_p = [n['id'] for n in data_p['notifications']]
        self.assertIn(notif_patient.id, ids_p)
        self.assertNotIn(notif_pharma.id, ids_p)

        # 2. Pharmacy retrieval
        self.client.force_login(self.pharm_user)
        resp_ph = self.client.get(self.pharm_list_url)
        self.assertEqual(resp_ph.status_code, 200)
        data_ph = resp_ph.json()
        ids_ph = [n['id'] for n in data_ph['notifications']]
        self.assertIn(notif_pharma.id, ids_ph)
        self.assertNotIn(notif_patient.id, ids_ph)

    def test_unrelated_user_sees_zero_notifications(self):
        """Unrelated user querying notifications receives an empty list."""
        Notification.objects.create(
            recipient=self.patient_user,
            actor=self.pharm_user,
            prescription_request=self.presc_request,
            notification_type='REQUEST_ACCEPTED',
            title='Patient Alert',
            message='Hello'
        )
        self.client.force_login(self.unrelated_patient_user)
        resp = self.client.get(self.patient_list_url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data['notifications']), 0)
        self.assertEqual(data['unread_count'], 0)

    def test_navigation_url_generated_correctly_per_role(self):
        """Navigation URLs point to patient request detail for patient, and pharmacy detail for pharmacy."""
        notif_patient = Notification.objects.create(
            recipient=self.patient_user,
            actor=self.pharm_user,
            prescription_request=self.presc_request,
            notification_type='REQUEST_ACCEPTED',
            title='Patient Notice',
            message='Msg'
        )
        notif_pharma = Notification.objects.create(
            recipient=self.pharm_user,
            actor=self.patient_user,
            prescription_request=self.presc_request,
            notification_type='REQUEST_SUBMITTED',
            title='Pharmacy Notice',
            message='Msg'
        )

        # Patient view
        self.client.force_login(self.patient_user)
        resp_p = self.client.get(self.patient_list_url)
        item_p = resp_p.json()['notifications'][0]
        expected_patient_url = reverse('patient:request_detail', kwargs={'pk': self.presc_request.pk})
        self.assertEqual(item_p['navigation_url'], expected_patient_url)

        # Pharmacy view
        self.client.force_login(self.pharm_user)
        resp_ph = self.client.get(self.pharm_list_url)
        item_ph = resp_ph.json()['notifications'][0]
        expected_pharm_url = reverse('pharmacy:request_detail', kwargs={'pk': self.presc_request.pk})
        self.assertEqual(item_ph['navigation_url'], expected_pharm_url)

    def test_pagination_and_newest_first_ordering(self):
        """Endpoint paginates with 20 items per page and returns newest notifications first."""
        # Create 25 notifications for patient
        created_ids = []
        for i in range(25):
            n = Notification.objects.create(
                recipient=self.patient_user,
                actor=self.pharm_user,
                prescription_request=self.presc_request,
                notification_type='REQUEST_PROCESSING',
                title=f'Notif #{i}',
                message=f'Message #{i}'
            )
            created_ids.append(n.id)

        self.client.force_login(self.patient_user)

        # Page 1
        resp_p1 = self.client.get(self.patient_list_url, {'page': 1})
        self.assertEqual(resp_p1.status_code, 200)
        data_p1 = resp_p1.json()
        self.assertEqual(len(data_p1['notifications']), 20)
        self.assertEqual(data_p1['pagination']['page'], 1)
        self.assertEqual(data_p1['pagination']['num_pages'], 2)
        self.assertEqual(data_p1['pagination']['total_count'], 25)
        self.assertTrue(data_p1['pagination']['has_next'])
        self.assertFalse(data_p1['pagination']['has_previous'])

        # Newest first: first item on page 1 is the latest created (Notif #24)
        self.assertEqual(data_p1['notifications'][0]['id'], created_ids[-1])

        # Page 2
        resp_p2 = self.client.get(self.patient_list_url, {'page': 2})
        self.assertEqual(resp_p2.status_code, 200)
        data_p2 = resp_p2.json()
        self.assertEqual(len(data_p2['notifications']), 5)
        self.assertEqual(data_p2['pagination']['page'], 2)
        self.assertFalse(data_p2['pagination']['has_next'])
        self.assertTrue(data_p2['pagination']['has_previous'])

    def test_filter_unread_parameter(self):
        """Querying ?filter=unread returns only notifications where is_read=False."""
        n_unread = Notification.objects.create(
            recipient=self.patient_user,
            actor=self.pharm_user,
            prescription_request=self.presc_request,
            notification_type='REQUEST_ACCEPTED',
            title='Unread Notif',
            message='Unread',
            is_read=False
        )
        n_read = Notification.objects.create(
            recipient=self.patient_user,
            actor=self.pharm_user,
            prescription_request=self.presc_request,
            notification_type='REQUEST_PROCESSING',
            title='Read Notif',
            message='Read',
            is_read=True,
            read_at=timezone.now()
        )

        self.client.force_login(self.patient_user)
        resp = self.client.get(self.patient_list_url, {'filter': 'unread'})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        ids = [n['id'] for n in data['notifications']]
        self.assertIn(n_unread.id, ids)
        self.assertNotIn(n_read.id, ids)

    # =========================================================================
    # 3. Unread Count Endpoint
    # =========================================================================

    def test_unread_count_accurate_and_excludes_read_records(self):
        """Unread count matches exact is_read=False count and excludes read ones."""
        # Create 3 unread and 2 read for patient
        for i in range(3):
            Notification.objects.create(
                recipient=self.patient_user,
                actor=self.pharm_user,
                prescription_request=self.presc_request,
                notification_type='REQUEST_ACCEPTED',
                title=f'Unread {i}',
                message='Msg',
                is_read=False
            )
        for i in range(2):
            Notification.objects.create(
                recipient=self.patient_user,
                actor=self.pharm_user,
                prescription_request=self.presc_request,
                notification_type='REQUEST_ACCEPTED',
                title=f'Read {i}',
                message='Msg',
                is_read=True,
                read_at=timezone.now()
            )

        # Create 5 unread for pharmacy (must not affect patient)
        for i in range(5):
            Notification.objects.create(
                recipient=self.pharm_user,
                actor=self.patient_user,
                prescription_request=self.presc_request,
                notification_type='REQUEST_SUBMITTED',
                title=f'Pharm Unread {i}',
                message='Msg',
                is_read=False
            )

        # Check patient unread count
        self.client.force_login(self.patient_user)
        resp_p = self.client.get(self.patient_unread_url)
        self.assertEqual(resp_p.status_code, 200)
        self.assertEqual(resp_p.json()['unread_count'], 3)

        # Check pharmacy unread count
        self.client.force_login(self.pharm_user)
        resp_ph = self.client.get(self.pharm_unread_url)
        self.assertEqual(resp_ph.status_code, 200)
        self.assertEqual(resp_ph.json()['unread_count'], 5)

    def test_unread_count_returns_zero_when_no_notifications(self):
        """Unread count returns 0 when user has zero notifications."""
        self.client.force_login(self.unrelated_patient_user)
        resp = self.client.get(self.patient_unread_url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['unread_count'], 0)

    # =========================================================================
    # 4. Mark One as Read Endpoint
    # =========================================================================

    def test_mark_one_notification_as_read(self):
        """Recipient can mark their notification as read; sets is_read=True and read_at."""
        notif = Notification.objects.create(
            recipient=self.patient_user,
            actor=self.pharm_user,
            prescription_request=self.presc_request,
            notification_type='REQUEST_ACCEPTED',
            title='Mark Read Test',
            message='Msg',
            is_read=False
        )
        url = reverse('patient:notification_mark_read', kwargs={'pk': notif.pk})

        self.client.force_login(self.patient_user)
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['is_read'])
        self.assertTrue(data['was_unread'])
        self.assertIsNotNone(data['read_at'])

        notif.refresh_from_db()
        self.assertTrue(notif.is_read)
        self.assertIsNotNone(notif.read_at)

    def test_mark_one_repeated_request_preserves_original_read_at(self):
        """Repeatedly calling mark-read preserves the original read_at timestamp."""
        notif = Notification.objects.create(
            recipient=self.patient_user,
            actor=self.pharm_user,
            prescription_request=self.presc_request,
            notification_type='REQUEST_ACCEPTED',
            title='Idempotency Test',
            message='Msg',
            is_read=False
        )
        url = reverse('patient:notification_mark_read', kwargs={'pk': notif.pk})

        self.client.force_login(self.patient_user)

        # First call: sets read_at
        resp1 = self.client.post(url)
        self.assertEqual(resp1.status_code, 200)
        notif.refresh_from_db()
        first_read_at = notif.read_at

        # Second call
        time.sleep(0.01)
        resp2 = self.client.post(url)
        self.assertEqual(resp2.status_code, 200)
        notif.refresh_from_db()

        self.assertEqual(notif.read_at, first_read_at)
        self.assertFalse(resp2.json()['was_unread'])

    def test_another_user_cannot_mark_notification_as_read(self):
        """Attempting to mark another user's notification as read returns 404."""
        notif = Notification.objects.create(
            recipient=self.patient_user,
            actor=self.pharm_user,
            prescription_request=self.presc_request,
            notification_type='REQUEST_ACCEPTED',
            title='Other User Notif',
            message='Msg',
            is_read=False
        )
        url = reverse('patient:notification_mark_read', kwargs={'pk': notif.pk})

        # Intruder attempts to mark patient's notification as read
        self.client.force_login(self.unrelated_patient_user)
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 404)

        notif.refresh_from_db()
        self.assertFalse(notif.is_read)

    def test_invalid_notification_id_returns_404(self):
        """Mark-read returns 404 for non-existent notification ID."""
        url = reverse('patient:notification_mark_read', kwargs={'pk': 999999})
        self.client.force_login(self.patient_user)
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 404)

    # =========================================================================
    # 5. Mark All as Read Endpoint
    # =========================================================================

    def test_mark_all_notifications_as_read_scoped_to_current_user(self):
        """Mark all as read updates only the current user's unread records."""
        # Patient: 3 unread, 1 read
        p_unreads = [
            Notification.objects.create(
                recipient=self.patient_user,
                actor=self.pharm_user,
                prescription_request=self.presc_request,
                notification_type='REQUEST_ACCEPTED',
                title=f'P Unread {i}',
                message='Msg',
                is_read=False
            )
            for i in range(3)
        ]
        p_read = Notification.objects.create(
            recipient=self.patient_user,
            actor=self.pharm_user,
            prescription_request=self.presc_request,
            notification_type='REQUEST_ACCEPTED',
            title='P Read',
            message='Msg',
            is_read=True,
            read_at=timezone.now()
        )

        # Pharmacy: 2 unread
        ph_unreads = [
            Notification.objects.create(
                recipient=self.pharm_user,
                actor=self.patient_user,
                prescription_request=self.presc_request,
                notification_type='REQUEST_SUBMITTED',
                title=f'Ph Unread {i}',
                message='Msg',
                is_read=False
            )
            for i in range(2)
        ]

        # Patient marks all read
        self.client.force_login(self.patient_user)
        resp = self.client.post(self.patient_mark_all_url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['updated_count'], 3)

        # Verify patient's notifications are now read
        for n in p_unreads:
            n.refresh_from_db()
            self.assertTrue(n.is_read)
            self.assertIsNotNone(n.read_at)

        # Pharmacy's notifications must remain unread
        for n in ph_unreads:
            n.refresh_from_db()
            self.assertFalse(n.is_read)
            self.assertIsNone(n.read_at)

    def test_mark_all_repeated_request_returns_zero_updates(self):
        """Repeatedly calling mark-all returns updated_count = 0 without error."""
        Notification.objects.create(
            recipient=self.patient_user,
            actor=self.pharm_user,
            prescription_request=self.presc_request,
            notification_type='REQUEST_ACCEPTED',
            title='Unread 1',
            message='Msg',
            is_read=False
        )

        self.client.force_login(self.patient_user)

        # First call updates 1
        resp1 = self.client.post(self.patient_mark_all_url)
        self.assertEqual(resp1.json()['updated_count'], 1)

        # Second call updates 0
        resp2 = self.client.post(self.patient_mark_all_url)
        self.assertEqual(resp2.json()['updated_count'], 0)

