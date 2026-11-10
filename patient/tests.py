import io
import json
from decimal import Decimal
from unittest.mock import patch, MagicMock
from PIL import Image
import pymupdf
from django.test import TestCase, Client
from django.urls import reverse
from django.db import IntegrityError
from django.core.files.uploadedfile import SimpleUploadedFile
from accounts.models import User
from patient.models import PatientProfile, Prescription, SavedPharmacy, PrescriptionMedicine, PrescriptionRequest
from pharmacy.models import PharmacyProfile, Feedback
from patient.services.gemini_vision import (
    GeminiExtractionResult,
    GeminiVisionError,
    GeminiNetworkError,
    GeminiQuotaError,
    GeminiConfigurationError,
    GeminiValidationError,
)


class PatientDashboardTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.patient_user = User.objects.create_user(
            username='patient_test',
            email='patient@example.com',
            password='TestPassword123!',
            role='PATIENT',
            first_name='Rahul'
        )
        self.patient_profile = PatientProfile.objects.create(
            user=self.patient_user,
            blood_group='O+',
            allergies='Peanuts'
        )

        self.dashboard_url = reverse('patient:dashboard')

    def test_anonymous_user_redirected(self):
        """Unauthenticated user should be redirected to login."""
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('accounts:login'), response.url)

    def test_patient_dashboard_renders_successfully(self):
        """Authenticated patient should be able to view their dashboard."""
        self.client.login(username='patient_test', password='TestPassword123!')
        response = self.client.get(self.dashboard_url)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'patient/dashboard.html')
        self.assertContains(response, 'Welcome back, Rahul!')
        self.assertEqual(response.context['total_prescriptions'], 0)
        self.assertEqual(response.context['saved_pharmacies_count'], 0)
        # 4 out of 10 core fields filled (first_name, email, blood_group, allergies) = 40%
        self.assertEqual(response.context['profile_completion'], 40)

    def test_patient_dashboard_with_prescriptions_and_saved_pharmacies(self):
        """Dashboard displays dynamic counts and latest prescriptions."""
        dummy_image = SimpleUploadedFile(
            name='test_rx.jpg',
            content=b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b',
            content_type='image/gif'
        )
        Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image=dummy_image,
            status='COMPLETED'
        )

        pharmacy_user = User.objects.create_user(
            username='pharmacy_test',
            email='pharmacy@example.com',
            password='TestPassword123!',
            role='PHARMACY'
        )
        pharmacy_profile = PharmacyProfile.objects.create(
            user=pharmacy_user,
            pharmacy_name='City Care Pharmacy',
            owner_name='Rajesh Kumar',
            pharmacist_name='Suresh Sharma',
            pharmacy_license_number='LIC-12345',
            gst_number='GSTIN1234567890',
            phone_number='9876543211',
            email='citycare@example.com',
            address='123 Main Street',
            city='Mumbai',
            state='Maharashtra',
            pincode='400001',
            approval_status='APPROVED'
        )
        SavedPharmacy.objects.create(
            patient=self.patient_profile,
            pharmacy=pharmacy_profile
        )

        self.client.login(username='patient_test', password='TestPassword123!')
        response = self.client.get(self.dashboard_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_prescriptions'], 1)
        self.assertEqual(response.context['saved_pharmacies_count'], 1)
        self.assertEqual(len(response.context['recent_prescriptions']), 1)
        self.assertContains(response, 'Completed')


class PatientProfileTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.patient_user = User.objects.create_user(
            username='patient_john',
            email='john@example.com',
            password='Password123!',
            role='PATIENT',
            first_name='John',
            last_name='Doe',
            phone_number='9876543210'
        )
        self.patient_profile = PatientProfile.objects.create(
            user=self.patient_user,
            date_of_birth='1990-05-15',
            gender='Male',
            blood_group='B+',
            address='456 Elm Street',
            allergies='Dust'
        )

        self.profile_url = reverse('patient:profile')
        self.location_url = reverse('patient:update_location')

    def test_anonymous_user_redirected_to_login(self):
        """Unauthenticated user accessing profile is redirected to login."""
        response = self.client.get(self.profile_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('accounts:login'), response.url)

    def test_other_role_cannot_access_patient_profile(self):
        """User with PHARMACY role cannot access patient profile."""
        User.objects.create_user(
            username='pharmacy_user',
            email='rx@example.com',
            password='Password123!',
            role='PHARMACY'
        )
        self.client.login(username='pharmacy_user', password='Password123!')
        response = self.client.get(self.profile_url)
        self.assertEqual(response.status_code, 302)

    def test_patient_can_view_profile(self):
        """Authenticated patient views their own profile page."""
        self.client.login(username='patient_john', password='Password123!')
        response = self.client.get(self.profile_url)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'patient/profile.html')
        self.assertContains(response, 'John Doe')
        self.assertContains(response, 'john@example.com')
        self.assertContains(response, '9876543210')
        self.assertContains(response, '456 Elm Street')

    def test_patient_can_update_profile(self):
        """Patient updates both User fields and PatientProfile fields."""
        self.client.login(username='patient_john', password='Password123!')
        data = {
            'first_name': 'Johnny',
            'last_name': 'Smith',
            'email': 'johnny.smith@example.com',
            'phone_number': '9988776655',
            'date_of_birth': '1992-08-20',
            'gender': 'Male',
            'blood_group': 'AB+',
            'address': '789 Oak Avenue',
            'allergies': 'Pollen and Penicillin',
        }
        response = self.client.post(self.profile_url, data, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Your profile details have been saved successfully.')

        # Verify DB updates
        self.patient_user.refresh_from_db()
        self.patient_profile.refresh_from_db()

        self.assertEqual(self.patient_user.first_name, 'Johnny')
        self.assertEqual(self.patient_user.last_name, 'Smith')
        self.assertEqual(self.patient_user.email, 'johnny.smith@example.com')
        self.assertEqual(self.patient_user.phone_number, '9988776655')

        self.assertEqual(str(self.patient_profile.date_of_birth), '1992-08-20')
        self.assertEqual(self.patient_profile.gender, 'Male')
        self.assertEqual(self.patient_profile.blood_group, 'AB+')
        self.assertEqual(self.patient_profile.address, '789 Oak Avenue')
        self.assertEqual(self.patient_profile.allergies, 'Pollen and Penicillin')

    def test_invalid_image_upload_rejected(self):
        """Uploading an invalid file or unsupported image format as profile photo is rejected."""
        self.client.login(username='patient_john', password='Password123!')
        
        # 1. Non-image file rejected by ImageField validation
        bad_text_file = SimpleUploadedFile(
            name='script.txt',
            content=b'Hello world plain text',
            content_type='text/plain'
        )
        data = {
            'first_name': 'John',
            'last_name': 'Doe',
            'email': 'john@example.com',
            'phone_number': '9876543210',
            'profile_photo': bad_text_file
        }
        response = self.client.post(self.profile_url, data)
        self.assertEqual(response.status_code, 200)
        self.assertFormError(
            response.context['profile_form'],
            'profile_photo',
            'Upload a valid image. The file you uploaded was either not an image or a corrupted image.'
        )

        # 2. Valid image but unsupported extension (.gif) rejected by custom validator
        valid_gif = SimpleUploadedFile(
            name='test.gif',
            content=b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b',
            content_type='image/gif'
        )
        data['profile_photo'] = valid_gif
        response = self.client.post(self.profile_url, data)
        self.assertEqual(response.status_code, 200)
        self.assertFormError(
            response.context['profile_form'],
            'profile_photo',
            'Unsupported image format. Allowed formats: JPG, PNG, WebP.'
        )

    def test_gps_coordinate_update_success(self):
        """Patient posts GPS coordinates via AJAX and PatientProfile updates."""
        self.client.login(username='patient_john', password='Password123!')
        payload = {
            'latitude': 19.0760900,
            'longitude': 72.8774260
        }
        response = self.client.post(
            self.location_url,
            data=json.dumps(payload),
            content_type='application/json'
        )

        self.assertEqual(response.status_code, 200)
        resp_data = response.json()
        self.assertEqual(resp_data['status'], 'success')

        self.patient_profile.refresh_from_db()
        self.assertAlmostEqual(float(self.patient_profile.current_latitude), 19.0760900, places=5)
        self.assertAlmostEqual(float(self.patient_profile.current_longitude), 72.8774260, places=5)
        self.assertIsNotNone(self.patient_profile.location_updated_at)

    def test_gps_coordinate_update_invalid_data(self):
        """Invalid coordinate payload returns 400 Bad Request."""
        self.client.login(username='patient_john', password='Password123!')
        payload = {
            'latitude': 999.0,  # Invalid latitude > 90
            'longitude': 72.8774260
        }
        response = self.client.post(
            self.location_url,
            data=json.dumps(payload),
            content_type='application/json'
        )

        self.assertEqual(response.status_code, 400)
        resp_data = response.json()
        self.assertEqual(resp_data['status'], 'error')



class PrescriptionUploadTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.patient_user = User.objects.create_user(
            username='rx_patient',
            email='rx_patient@example.com',
            password='TestPassword123!',
            role='PATIENT',
            first_name='Ananya'
        )
        self.patient_profile = PatientProfile.objects.create(
            user=self.patient_user,
            blood_group='A+'
        )
        self.upload_url = reverse('patient:upload_prescription')
        self.dashboard_url = reverse('patient:dashboard')

        # Dynamically generate guaranteed valid JPEG bytes via Pillow
        img = Image.new('RGB', (100, 100), color='white')
        buf = io.BytesIO()
        img.save(buf, format='JPEG')
        self.valid_image_bytes = buf.getvalue()

    def test_anonymous_user_redirected_to_login(self):
        """Unauthenticated user accessing upload page is redirected to login."""
        response = self.client.get(self.upload_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('accounts:login'), response.url)

    def test_pharmacy_and_admin_cannot_access_upload_page(self):
        """Users with PHARMACY or ADMIN roles cannot access prescription upload."""
        # 1. Pharmacy user
        User.objects.create_user(
            username='rx_pharmacy',
            email='pharmacy_staff@example.com',
            password='TestPassword123!',
            role='PHARMACY'
        )
        self.client.login(username='rx_pharmacy', password='TestPassword123!')
        response = self.client.get(self.upload_url)
        self.assertEqual(response.status_code, 302)

        # 2. Admin user
        User.objects.create_superuser(
            username='rx_admin',
            email='admin_staff@example.com',
            password='TestPassword123!'
        )
        self.client.login(username='rx_admin', password='TestPassword123!')
        response = self.client.get(self.upload_url)
        self.assertEqual(response.status_code, 302)

    def test_patient_can_view_upload_page(self):
        """Authenticated patient can view the upload prescription page."""
        self.client.login(username='rx_patient', password='TestPassword123!')
        response = self.client.get(self.upload_url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'patient/upload_prescription.html')
        self.assertContains(response, "Upload Doctor's Prescription")

    @patch('patient.views.extract_prescription_details')
    def test_patient_can_upload_valid_prescription(self, mock_extract):
        """Patient uploads a valid image; it processes through Gemini Vision and saves with status COMPLETED."""
        mock_extract.return_value = GeminiExtractionResult(
            medicines=[{
                'medicine_name': 'Amoxicillin',
                'strength': '500 mg',
                'dosage': '1 capsule',
                'frequency': '1-0-1',
                'duration': '5 days',
                'instructions': 'After meals',
                'needs_verification': False,
                'verification_reason': '',
            }],
            unclear_details=[],
            raw_json='{}',
            metadata={'model': 'gemini-3.5-flash-lite', 'page_count': 1, 'duration_seconds': 0.1}
        )
        self.client.login(username='rx_patient', password='TestPassword123!')
        valid_file = SimpleUploadedFile(
            name='doctor_rx.jpg',
            content=self.valid_image_bytes,
            content_type='image/jpeg'
        )
        response = self.client.post(self.upload_url, {'prescription_image': valid_file}, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertRedirects(response, self.dashboard_url)

        # Check DB entry
        prescription = Prescription.objects.filter(patient=self.patient_profile).first()
        self.assertIsNotNone(prescription)
        self.assertEqual(prescription.patient, self.patient_profile)
        self.assertEqual(prescription.status, 'COMPLETED')
        self.assertEqual(prescription.extracted_data['medicines'][0]['medicine_name'], 'Amoxicillin')
        self.assertIsNotNone(prescription.uploaded_at)
        self.assertIn('Prescription uploaded and analyzed successfully', response.content.decode('utf-8'))

    @patch('patient.views.extract_prescription_details')
    def test_dashboard_prescription_count_updates_after_upload(self, mock_extract):
        """Dashboard dynamic count increments and shows the uploaded prescription."""
        mock_extract.return_value = GeminiExtractionResult(
            medicines=[],
            unclear_details=[],
            raw_json='{}',
            metadata={'model': 'gemini-3.5-flash-lite', 'page_count': 1, 'duration_seconds': 0.1}
        )
        self.client.login(username='rx_patient', password='TestPassword123!')

        # Before upload: total_prescriptions is 0
        resp_before = self.client.get(self.dashboard_url)
        self.assertEqual(resp_before.context['total_prescriptions'], 0)
        self.assertEqual(len(resp_before.context['recent_prescriptions']), 0)

        # Upload prescription
        valid_file = SimpleUploadedFile(
            name='rx_scan.jpg',
            content=self.valid_image_bytes,
            content_type='image/jpeg'
        )
        self.client.post(self.upload_url, {'prescription_image': valid_file})

        # After upload: total_prescriptions is 1 and appears in recent list
        resp_after = self.client.get(self.dashboard_url)
        self.assertEqual(resp_after.context['total_prescriptions'], 1)
        self.assertEqual(len(resp_after.context['recent_prescriptions']), 1)
        self.assertEqual(resp_after.context['recent_prescriptions'][0].status, 'COMPLETED')

    def test_invalid_file_type_rejected(self):
        """Uploading non-image files (.txt) is rejected."""
        self.client.login(username='rx_patient', password='TestPassword123!')
        text_file = SimpleUploadedFile(
            name='prescription.txt',
            content=b'Prescription details in text',
            content_type='text/plain'
        )
        response = self.client.post(self.upload_url, {'prescription_image': text_file})
        self.assertEqual(response.status_code, 200)
        self.assertFormError(
            response.context['form'],
            'prescription_image',
            'Unsupported image format. Please upload JPG, JPEG, PNG, or WEBP.'
        )
        self.assertEqual(Prescription.objects.count(), 0)

    def test_file_larger_than_10mb_rejected(self):
        """Uploading files larger than 10 MB is rejected."""
        self.client.login(username='rx_patient', password='TestPassword123!')
        oversized_content = self.valid_image_bytes + (b'0' * (10 * 1024 * 1024 + 1024))
        oversized_file = SimpleUploadedFile(
            name='giant_rx.jpg',
            content=oversized_content,
            content_type='image/jpeg'
        )

        response = self.client.post(self.upload_url, {'prescription_image': oversized_file})
        self.assertEqual(response.status_code, 200)
        self.assertFormError(
            response.context['form'],
            'prescription_image',
            'File size (10.0 MB) exceeds the limit of 10 MB.'
        )
        self.assertEqual(Prescription.objects.count(), 0)

    @patch('patient.views.extract_prescription_details')
    def test_upload_preserves_prescription_as_failed_when_gemini_fails(self, mock_extract):
        """When Gemini Vision extraction fails, upload is preserved in DB with status FAILED and safe empty data."""
        mock_extract.side_effect = GeminiNetworkError("Connection refused by Gemini API")
        self.client.login(username='rx_patient', password='TestPassword123!')
        valid_file = SimpleUploadedFile(
            name='doctor_rx.jpg',
            content=self.valid_image_bytes,
            content_type='image/jpeg'
        )
        response = self.client.post(self.upload_url, {'prescription_image': valid_file}, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertRedirects(response, self.dashboard_url)

        prescription = Prescription.objects.filter(patient=self.patient_profile).first()
        self.assertIsNotNone(prescription)
        self.assertEqual(prescription.status, 'FAILED')
        self.assertEqual(prescription.extracted_data, {'medicines': [], 'unclear_details': []})
        self.assertIn('automatic medicine extraction was unsuccessful', response.content.decode('utf-8'))

    @patch('patient.views.extract_prescription_details')
    def test_upload_pdf_prescription_successfully_processed(self, mock_extract):
        """Uploading a multi-page PDF renders pages and successfully stores extracted details."""
        import pymupdf
        doc = pymupdf.open()
        doc.new_page()
        pdf_bytes = doc.tobytes()
        doc.close()

        mock_extract.return_value = GeminiExtractionResult(
            medicines=[{
                'medicine_name': 'Paracetamol',
                'strength': '650 mg',
                'dosage': '1 tablet',
                'frequency': 'TDS',
                'duration': '3 days',
                'instructions': 'After food',
                'needs_verification': False,
                'verification_reason': '',
            }],
            unclear_details=[],
            raw_json='{}',
            metadata={'model': 'gemini-3.5-flash-lite', 'page_count': 1, 'duration_seconds': 0.1}
        )

        self.client.login(username='rx_patient', password='TestPassword123!')
        pdf_file = SimpleUploadedFile(
            name='prescription.pdf',
            content=pdf_bytes,
            content_type='application/pdf'
        )
        response = self.client.post(self.upload_url, {'prescription_image': pdf_file}, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertRedirects(response, self.dashboard_url)

        prescription = Prescription.objects.filter(patient=self.patient_profile).first()
        self.assertIsNotNone(prescription)
        self.assertEqual(prescription.status, 'COMPLETED')
        self.assertTrue(prescription.prescription_image.name.endswith('.pdf'))

    def test_manual_medicine_confirmation_does_not_overwrite_gemini_data(self):
        """Manually confirming medicines creates PrescriptionMedicine rows without modifying extracted_data JSON."""
        prescription = Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image='patient/prescriptions/sample.jpg',
            status='COMPLETED',
            extracted_data={
                'medicines': [{
                    'medicine_name': 'AI Extracted Drug',
                    'strength': '10 mg',
                    'dosage': '1 tab',
                    'frequency': 'OD',
                    'duration': '10 days',
                    'instructions': '',
                    'needs_verification': False,
                    'verification_reason': ''
                }],
                'unclear_details': []
            }
        )
        self.client.login(username='rx_patient', password='TestPassword123!')
        confirm_url = reverse('patient:confirm_prescription_medicine', kwargs={'pk': prescription.pk})
        post_data = {
            'medicine_name': 'Manual Confirmed Drug',
            'strength': '20 mg',
            'dosage': '2 tabs',
            'duration': '5 days',
            'quantity': '10'
        }
        response = self.client.post(confirm_url, post_data)
        self.assertEqual(response.status_code, 302)

        # Verify PrescriptionMedicine record created
        manual_med = prescription.medicines.first()
        self.assertIsNotNone(manual_med)
        self.assertEqual(manual_med.medicine_name, 'Manual Confirmed Drug')

        # Verify extracted_data JSONField was not modified or overwritten
        prescription.refresh_from_db()
        self.assertEqual(prescription.extracted_data['medicines'][0]['medicine_name'], 'AI Extracted Drug')



class PrescriptionListAndDetailTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Patient 1
        self.user1 = User.objects.create_user(
            username='patient_one',
            email='one@example.com',
            password='TestPassword123!',
            role='PATIENT',
            first_name='Aarav'
        )
        self.profile1 = PatientProfile.objects.create(user=self.user1)

        # Patient 2
        self.user2 = User.objects.create_user(
            username='patient_two',
            email='two@example.com',
            password='TestPassword123!',
            role='PATIENT',
            first_name='Priya'
        )
        self.profile2 = PatientProfile.objects.create(user=self.user2)

        # 1x1 dummy image
        self.dummy_image = SimpleUploadedFile(
            name='rx.jpg',
            content=(
                b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff'
                b'\x00\x00\x00\x21\xf9\x04\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00'
                b'\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'
            ),
            content_type='image/jpeg'
        )

        # Prescription for Patient 1
        self.rx1 = Prescription.objects.create(
            patient=self.profile1,
            prescription_image=self.dummy_image,
            status='UPLOADED',
            extracted_text='Tab Paracetamol 650mg'
        )

        # Prescription for Patient 2
        self.rx2 = Prescription.objects.create(
            patient=self.profile2,
            prescription_image=self.dummy_image,
            status='COMPLETED',
            extracted_text='Cap Omeprazole 20mg'
        )

        self.list_url = reverse('patient:prescriptions_list')
        self.detail_url_1 = reverse('patient:prescription_detail', kwargs={'pk': self.rx1.id})
        self.detail_url_2 = reverse('patient:prescription_detail', kwargs={'pk': self.rx2.id})

    def test_anonymous_user_redirected_to_login(self):
        """Unauthenticated user accessing prescription list or details is redirected to login."""
        resp_list = self.client.get(self.list_url)
        self.assertEqual(resp_list.status_code, 302)
        self.assertIn(reverse('accounts:login'), resp_list.url)

        resp_detail = self.client.get(self.detail_url_1)
        self.assertEqual(resp_detail.status_code, 302)
        self.assertIn(reverse('accounts:login'), resp_detail.url)

    def test_pharmacy_and_admin_users_denied_access(self):
        """Pharmacy and Admin roles cannot access patient prescription views."""
        # 1. Pharmacy user
        User.objects.create_user(
            username='pharmacy_doc',
            email='staff@rx.com',
            password='TestPassword123!',
            role='PHARMACY'
        )
        self.client.login(username='pharmacy_doc', password='TestPassword123!')
        self.assertEqual(self.client.get(self.list_url).status_code, 302)
        self.assertEqual(self.client.get(self.detail_url_1).status_code, 302)

        # 2. Admin user
        User.objects.create_superuser(
            username='admin_doc',
            email='admin@medaccess.com',
            password='TestPassword123!'
        )
        self.client.login(username='admin_doc', password='TestPassword123!')
        self.assertEqual(self.client.get(self.list_url).status_code, 302)
        self.assertEqual(self.client.get(self.detail_url_1).status_code, 302)

    def test_empty_prescription_list_displays_correctly(self):
        """Patient with no prescriptions sees the empty state with upload action."""
        empty_user = User.objects.create_user(
            username='new_patient',
            email='new@example.com',
            password='TestPassword123!',
            role='PATIENT'
        )
        PatientProfile.objects.create(user=empty_user)

        self.client.login(username='new_patient', password='TestPassword123!')
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No Prescriptions Uploaded Yet')
        self.assertContains(response, 'Upload Your First Prescription')

    def test_patient_can_view_own_prescription_list(self):
        """Patient 1 sees their prescription and does not see Patient 2's prescription."""
        self.client.login(username='patient_one', password='TestPassword123!')
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'patient/prescriptions_list.html')
        self.assertContains(response, f"Prescription #{self.rx1.id}")
        self.assertNotContains(response, f"Prescription #{self.rx2.id}")

    def test_patient_can_view_own_prescription_details(self):
        """Patient 1 can view the detail page for their prescription."""
        self.client.login(username='patient_one', password='TestPassword123!')
        response = self.client.get(self.detail_url_1)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'patient/prescription_detail.html')
        self.assertContains(response, f"Prescription #{self.rx1.id}")
        self.assertContains(response, "Prescription Document")
        self.assertContains(response, "Prescribed Medicines")
        self.assertNotContains(response, "Raw OCR Extracted Text")

    def test_another_patient_cannot_view_prescription_details_returns_404(self):
        """Security: If Patient 2 tries to access Patient 1's prescription ID, returns 404."""
        self.client.login(username='patient_two', password='TestPassword123!')
        response = self.client.get(self.detail_url_1)
        self.assertEqual(response.status_code, 404)

    def test_prescription_medicines_display_correctly_when_present(self):
        """Associated PrescriptionMedicine items are rendered properly on detail page."""
        PrescriptionMedicine.objects.create(
            prescription=self.rx1,
            medicine_name='Amoxicillin',
            strength='500mg',
            dosage='1 tab twice daily',
            duration='5 days',
            quantity='10'
        )

        self.client.login(username='patient_one', password='TestPassword123!')
        response = self.client.get(self.detail_url_1)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Amoxicillin')
        self.assertContains(response, '500mg')
        self.assertContains(response, '1 tab twice daily')
        self.assertContains(response, '5 days')
        self.assertContains(response, '10')

    def test_prescription_detail_page_loads_cleanly(self):
        """Prescription detail view renders cleanly without any OCR elements."""
        self.client.login(username='patient_one', password='TestPassword123!')
        response = self.client.get(self.detail_url_1)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Prescription Document')
        self.assertContains(response, 'Prescribed Medicines')
        self.assertNotContains(response, 'Retry OCR')
        self.assertNotContains(response, 'Raw OCR Extracted Text')
        self.assertNotContains(response, 'Medical Integrity Notice')


# ==============================================================================
# PHASE 4, TASK 4A: MY PRESCRIPTIONS UI INTEGRATION TESTS
# ==============================================================================
class MyPrescriptionsUITests(TestCase):
    """
    Phase 4, Task 4A: My Prescriptions UI Integration Tests.
    Tests user-friendly extraction status display, extracted medicine name chips preview,
    PDF vs image rendering, patient ownership isolation, safe handling of malformed data,
    and guarantee that no PrescriptionMedicine records are created or modified by list rendering.
    """
    def setUp(self):
        self.client = Client()
        self.patient_user = User.objects.create_user(
            username='my_rx_patient',
            email='myrx@example.com',
            password='TestPassword123!',
            role='PATIENT',
            first_name='Kavita'
        )
        self.patient_profile = PatientProfile.objects.create(
            user=self.patient_user,
            blood_group='B+'
        )
        self.other_user = User.objects.create_user(
            username='other_patient',
            email='other@example.com',
            password='TestPassword123!',
            role='PATIENT',
            first_name='Rohan'
        )
        self.other_profile = PatientProfile.objects.create(
            user=self.other_user,
            blood_group='O+'
        )
        self.list_url = reverse('patient:prescriptions_list')

    def test_completed_prescription_displays_medicine_chips(self):
        """COMPLETED prescription displays up to 3 medicine name chips and 'Analysis completed'."""
        Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image='patient/prescriptions/rx1.jpg',
            status='COMPLETED',
            extracted_data={
                'medicines': [
                    {'medicine_name': 'Paracetamol', 'strength': '650 mg', 'dosage': '1 tab'},
                    {'medicine_name': 'Amoxicillin', 'strength': '500 mg', 'dosage': '1 cap'},
                ],
                'unclear_details': []
            }
        )
        self.client.login(username='my_rx_patient', password='TestPassword123!')
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Analysis completed')
        self.assertContains(response, 'Extracted Medicines')
        self.assertContains(response, 'Paracetamol')
        self.assertContains(response, 'Amoxicillin')
        self.assertNotContains(response, '+')

    def test_more_than_three_medicines_shows_more_indicator(self):
        """Prescription with 5 medicines displays 3 chips and a '+2 more' indicator."""
        Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image='patient/prescriptions/rx2.jpg',
            status='COMPLETED',
            extracted_data={
                'medicines': [
                    {'medicine_name': 'Metformin'},
                    {'medicine_name': 'Glimepiride'},
                    {'medicine_name': 'Atorvastatin'},
                    {'medicine_name': 'Aspirin'},
                    {'medicine_name': 'Telmisartan'},
                ],
                'unclear_details': []
            }
        )
        self.client.login(username='my_rx_patient', password='TestPassword123!')
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Metformin')
        self.assertContains(response, 'Glimepiride')
        self.assertContains(response, 'Atorvastatin')
        self.assertContains(response, '+2 more')
        # Ensure 4th and 5th names are not rendered as chips on the card preview
        self.assertNotContains(response, '<span class="rx-med-chip" title="Telmisartan">Telmisartan</span>')
        # Verify 5th medicine is available in the modal details table
        self.assertContains(response, '<div class="rx-medname-title">Telmisartan</div>')

    def test_completed_extraction_with_empty_medicines_list(self):
        """Completed prescription without medicines displays 'No medicines identified'."""
        Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image='patient/prescriptions/rx_empty.jpg',
            status='COMPLETED',
            extracted_data={'medicines': [], 'unclear_details': ['Lab test document']}
        )
        self.client.login(username='my_rx_patient', password='TestPassword123!')
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Analysis completed')
        self.assertContains(response, 'No medicines identified')

    def test_failed_status_display(self):
        """FAILED status displays patient-friendly message without exposing error traces."""
        Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image='patient/prescriptions/rx_fail.jpg',
            status='FAILED',
            extracted_data={'medicines': [], 'unclear_details': []}
        )
        self.client.login(username='my_rx_patient', password='TestPassword123!')
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Automatic extraction unsuccessful – Manual entry available')
        self.assertContains(response, 'Manual entry available on details page')

    def test_processing_status_display(self):
        """PROCESSING status displays 'Analyzing prescription'."""
        Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image='patient/prescriptions/rx_proc.jpg',
            status='PROCESSING',
            extracted_data={}
        )
        self.client.login(username='my_rx_patient', password='TestPassword123!')
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Analyzing prescription')
        self.assertContains(response, 'Analyzing prescription document...')

    def test_uploaded_status_display(self):
        """UPLOADED status displays 'Uploaded – Awaiting analysis'."""
        Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image='patient/prescriptions/rx_up.jpg',
            status='UPLOADED',
            extracted_data={}
        )
        self.client.login(username='my_rx_patient', password='TestPassword123!')
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Uploaded – Awaiting analysis')
        self.assertContains(response, 'Awaiting analysis...')

    def test_pdf_preview_rendering(self):
        """PDF prescription renders PDF Document icon badge rather than a broken img tag."""
        Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image='patient/prescriptions/doc.pdf',
            status='COMPLETED',
            extracted_data={'medicines': [{'medicine_name': 'Cefixime'}], 'unclear_details': []}
        )
        self.client.login(username='my_rx_patient', password='TestPassword123!')
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'PDF Document')
        self.assertContains(response, 'rx-card-thumb-pdf')

    def test_image_thumbnail_rendering(self):
        """Image prescription renders an img tag with the secure authenticated image URL."""
        rx = Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image='patient/prescriptions/photo.png',
            status='COMPLETED',
            extracted_data={'medicines': [{'medicine_name': 'Cetirizine'}], 'unclear_details': []}
        )
        self.client.login(username='my_rx_patient', password='TestPassword123!')
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'rx-card-thumb')
        self.assertContains(response, reverse('patient:prescription_secure_image', kwargs={'pk': rx.id}))

    def test_patient_ownership_and_access_protection(self):
        """Patient only sees their own prescriptions; other patients' records are excluded."""
        my_rx = Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image='patient/prescriptions/mine.jpg',
            status='COMPLETED',
            extracted_data={'medicines': [{'medicine_name': 'My Medicine'}], 'unclear_details': []}
        )
        other_rx = Prescription.objects.create(
            patient=self.other_profile,
            prescription_image='patient/prescriptions/theirs.jpg',
            status='COMPLETED',
            extracted_data={'medicines': [{'medicine_name': 'Secret Medicine'}], 'unclear_details': []}
        )
        self.client.login(username='my_rx_patient', password='TestPassword123!')
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"Prescription #{my_rx.id}")
        self.assertContains(response, 'My Medicine')
        self.assertNotContains(response, f"Prescription #{other_rx.id}")
        self.assertNotContains(response, 'Secret Medicine')

    def test_malformed_extracted_data_handled_gracefully(self):
        """Corrupt, None, or unexpected JSON types in extracted_data do not cause template crash."""
        Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image='patient/prescriptions/corrupt1.jpg',
            status='COMPLETED',
            extracted_data=None
        )
        Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image='patient/prescriptions/corrupt2.jpg',
            status='COMPLETED',
            extracted_data={'medicines': 'invalid_string_not_list'}
        )
        Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image='patient/prescriptions/corrupt3.jpg',
            status='COMPLETED',
            extracted_data={'medicines': [None, 123, {'wrong_key': 'val'}]}
        )
        self.client.login(username='my_rx_patient', password='TestPassword123!')
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No medicines identified')

    def test_no_prescription_medicine_records_created_or_modified(self):
        """Viewing the prescriptions list never creates, modifies, or deletes PrescriptionMedicine records."""
        rx = Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image='patient/prescriptions/rx_safe.jpg',
            status='COMPLETED',
            extracted_data={'medicines': [{'medicine_name': 'Ibuprofen'}], 'unclear_details': []}
        )
        PrescriptionMedicine.objects.create(
            prescription=rx,
            medicine_name='Confirmed Paracetamol',
            strength='500mg'
        )
        initial_count = PrescriptionMedicine.objects.count()

        self.client.login(username='my_rx_patient', password='TestPassword123!')
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)

        # Confirm count is unchanged
        self.assertEqual(PrescriptionMedicine.objects.count(), initial_count)
        self.assertEqual(PrescriptionMedicine.objects.first().medicine_name, 'Confirmed Paracetamol')

    def test_modal_rendered_with_prescription_id_and_trigger_button(self):
        """Card View Details button targets the matching prescription modal dialog."""
        rx = Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image='patient/prescriptions/rx_modal.jpg',
            status='COMPLETED',
            extracted_data={'medicines': [{'medicine_name': 'Dolo 650'}], 'unclear_details': []}
        )
        self.client.login(username='my_rx_patient', password='TestPassword123!')
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)

        # Trigger button attributes
        self.assertContains(response, f'data-modal-target="rx-modal-{rx.id}"')
        self.assertContains(response, f'aria-controls="rx-modal-{rx.id}"')

        # Modal dialog attributes
        self.assertContains(response, f'id="rx-modal-{rx.id}"')
        self.assertContains(response, 'role="dialog"')
        self.assertContains(response, 'aria-modal="true"')
        self.assertContains(response, f'aria-labelledby="rx-modal-title-{rx.id}"')

    def test_modal_displays_extracted_medicine_details_table(self):
        """Modal table displays all extracted medication fields: name, strength, dosage, frequency, duration, instructions."""
        rx = Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image='patient/prescriptions/table_test.jpg',
            status='COMPLETED',
            extracted_data={
                'medicines': [{
                    'medicine_name': 'Amoxicillin Trihydrate',
                    'strength': '500 mg',
                    'dosage': '1 capsule',
                    'frequency': '1-0-1',
                    'duration': '7 days',
                    'instructions': 'Take after food with water',
                    'needs_verification': False,
                    'verification_reason': '',
                }],
                'unclear_details': []
            }
        )
        self.client.login(username='my_rx_patient', password='TestPassword123!')
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)

        self.assertContains(response, 'Amoxicillin Trihydrate')
        self.assertContains(response, '500 mg')
        self.assertContains(response, '1 capsule')
        self.assertContains(response, '1-0-1')
        self.assertContains(response, '7 days')
        self.assertContains(response, 'Take after food with water')

    def test_modal_displays_verification_warning_for_uncertain_medicine(self):
        """Modal highlights medicine requiring verification with visible warning and explanation reason."""
        rx = Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image='patient/prescriptions/verify_test.jpg',
            status='COMPLETED',
            extracted_data={
                'medicines': [{
                    'medicine_name': 'Azithromycin',
                    'strength': '250 mg',
                    'dosage': '1 tab',
                    'frequency': 'OD',
                    'duration': '3 days',
                    'instructions': 'Before food',
                    'needs_verification': True,
                    'verification_reason': 'Dosage duration text partially smudged by doctor stamp',
                }],
                'unclear_details': []
            }
        )
        self.client.login(username='my_rx_patient', password='TestPassword123!')
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)

        self.assertContains(response, 'rx-row-verify-needed')
        self.assertContains(response, 'Needs Verification:')
        self.assertContains(response, 'Dosage duration text partially smudged by doctor stamp')

    def test_modal_displays_unclear_details_warning_box(self):
        """Modal displays a distinct attention callout box when unclear or illegible items are noted."""
        rx = Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image='patient/prescriptions/unclear_test.jpg',
            status='COMPLETED',
            extracted_data={
                'medicines': [{'medicine_name': 'Pantoprazole'}],
                'unclear_details': [
                    'Illegible handwriting beneath medicine #1',
                    'Faint doctor signature in lower right corner'
                ]
            }
        )
        self.client.login(username='my_rx_patient', password='TestPassword123!')
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)

        self.assertContains(response, 'Unclear or Uncertain Information Noted')
        self.assertContains(response, 'Illegible handwriting beneath medicine #1')
        self.assertContains(response, 'Faint doctor signature in lower right corner')

    def test_modal_displays_pdf_document_view_link_for_pdf_prescription(self):
        """Modal displays PDF medical document view and open button for PDF prescriptions."""
        rx = Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image='patient/prescriptions/report.pdf',
            status='COMPLETED',
            extracted_data={'medicines': [{'medicine_name': 'Vitamin D3'}], 'unclear_details': []}
        )
        self.client.login(username='my_rx_patient', password='TestPassword123!')
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)

        self.assertContains(response, 'PDF Medical Document')
        self.assertContains(response, 'Open PDF Document')
        self.assertContains(response, reverse('patient:prescription_secure_image', kwargs={'pk': rx.id}))

    def test_modal_footer_contains_open_full_prescription_page_link(self):
        """Modal footer contains a link to open the full prescription details page and a Close button."""
        rx = Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image='patient/prescriptions/footer_test.jpg',
            status='COMPLETED',
            extracted_data={'medicines': [{'medicine_name': 'Metformin'}], 'unclear_details': []}
        )
        self.client.login(username='my_rx_patient', password='TestPassword123!')
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)

        detail_url = reverse('patient:prescription_detail', kwargs={'pk': rx.id})
        self.assertContains(response, f'href="{detail_url}"')
        self.assertContains(response, 'Open Full Prescription Page')
        self.assertContains(response, 'data-modal-close')

    def test_multiple_prescription_cards_render_distinct_modals(self):
        """Multiple prescriptions render distinct modal containers matching their unique IDs."""
        rx1 = Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image='patient/prescriptions/multi1.jpg',
            status='COMPLETED',
            extracted_data={'medicines': [{'medicine_name': 'First Prescription Drug'}], 'unclear_details': []}
        )
        rx2 = Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image='patient/prescriptions/multi2.jpg',
            status='COMPLETED',
            extracted_data={'medicines': [{'medicine_name': 'Second Prescription Drug'}], 'unclear_details': []}
        )
        self.client.login(username='my_rx_patient', password='TestPassword123!')
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)

        # Both modals exist in the DOM with their specific IDs
        self.assertContains(response, f'id="rx-modal-{rx1.id}"')
        self.assertContains(response, f'id="rx-modal-{rx2.id}"')
        self.assertContains(response, f'data-modal-target="rx-modal-{rx1.id}"')
        self.assertContains(response, f'data-modal-target="rx-modal-{rx2.id}"')
        self.assertContains(response, 'First Prescription Drug')
        self.assertContains(response, 'Second Prescription Drug')


class PrescriptionSecureImageSecurityTests(TestCase):
    """
    Security and access control audit for the authenticated prescription image endpoint:
    /patient/prescriptions/<pk>/image/
    """
    def setUp(self):
        self.client = Client()

        # Patient 1 (Owner)
        self.patient1_user = User.objects.create_user(
            username='secure_patient1',
            email='p1@medaccess.test',
            password='TestPassword123!',
            role='PATIENT'
        )
        self.patient1_user.email_verified = True
        self.patient1_user.save()
        self.patient1_profile = PatientProfile.objects.create(
            user=self.patient1_user
        )

        # Patient 2 (Attacker / Different Patient)
        self.patient2_user = User.objects.create_user(
            username='secure_patient2',
            email='p2@medaccess.test',
            password='TestPassword123!',
            role='PATIENT'
        )
        self.patient2_user.email_verified = True
        self.patient2_user.save()
        self.patient2_profile = PatientProfile.objects.create(
            user=self.patient2_user
        )

        # Pharmacy 1 (Authorized)
        self.pharmacy1_user = User.objects.create_user(
            username='secure_pharmacy1',
            email='pharm1@medaccess.test',
            password='TestPassword123!',
            role='PHARMACY'
        )
        self.pharmacy1_user.email_verified = True
        self.pharmacy1_user.save()
        self.pharmacy1_profile = PharmacyProfile.objects.create(
            user=self.pharmacy1_user,
            pharmacy_name='Authorized Pharmacy',
            owner_name='Owner One',
            pharmacist_name='Pharmacist One',
            pharmacy_license_number='PHARM-AUTH-100',
            gst_number='GST123456',
            phone_number='9876543212',
            email='pharm1@medaccess.test',
            address='123 Auth Street',
            city='Health City',
            state='State',
            pincode='500001'
        )

        # Pharmacy 2 (Unauthorized)
        self.pharmacy2_user = User.objects.create_user(
            username='secure_pharmacy2',
            email='pharm2@medaccess.test',
            password='TestPassword123!',
            role='PHARMACY'
        )
        self.pharmacy2_user.email_verified = True
        self.pharmacy2_user.save()
        self.pharmacy2_profile = PharmacyProfile.objects.create(
            user=self.pharmacy2_user,
            pharmacy_name='Unauthorized Pharmacy',
            owner_name='Owner Two',
            pharmacist_name='Pharmacist Two',
            pharmacy_license_number='PHARM-UNAUTH-200',
            gst_number='GST654321',
            phone_number='9876543213',
            email='pharm2@medaccess.test',
            address='456 Unauth Street',
            city='Health City',
            state='State',
            pincode='500001'
        )

        # Real file for prescription
        dummy_img = SimpleUploadedFile("rx_doc.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 32, content_type="image/jpeg")
        self.prescription = Prescription.objects.create(
            patient=self.patient1_profile,
            prescription_image=dummy_img,
            status='COMPLETED'
        )
        self.image_url = reverse('patient:prescription_secure_image', kwargs={'pk': self.prescription.id})

    def tearDown(self):
        try:
            if self.prescription.prescription_image and self.prescription.prescription_image.storage.exists(self.prescription.prescription_image.name):
                self.prescription.prescription_image.storage.delete(self.prescription.prescription_image.name)
        except Exception:
            pass

    def test_anonymous_user_redirected_to_login(self):
        """Unauthenticated request must be redirected to login page."""
        response = self.client.get(self.image_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)

    def test_patient_can_access_own_prescription_image(self):
        """Owner patient gets HTTP 200 with private cache-control headers."""
        self.client.login(username='secure_patient1', password='TestPassword123!')
        response = self.client.get(self.image_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/jpeg')
        self.assertIn('private', response['Cache-Control'])
        self.assertIn('no-store', response['Cache-Control'])
        response.close()

    def test_other_patient_denied_access(self):
        """Different patient attempting to access prescription receives HTTP 403 Forbidden."""
        self.client.login(username='secure_patient2', password='TestPassword123!')
        response = self.client.get(self.image_url)
        self.assertEqual(response.status_code, 403)

    def test_authorized_pharmacy_can_access_prescription_image(self):
        """Pharmacy with an active request can access the prescription image."""
        PrescriptionRequest.objects.create(
            patient=self.patient1_profile,
            prescription=self.prescription,
            pharmacy=self.pharmacy1_profile,
            status='PENDING'
        )
        self.client.login(username='secure_pharmacy1', password='TestPassword123!')
        response = self.client.get(self.image_url)
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_unauthorized_pharmacy_denied_access(self):
        """Pharmacy with no request for this prescription receives HTTP 403 Forbidden."""
        self.client.login(username='secure_pharmacy2', password='TestPassword123!')
        response = self.client.get(self.image_url)
        self.assertEqual(response.status_code, 403)

    def test_missing_file_returns_404_without_path_leak(self):
        """If image file does not exist on disk, view returns 404 cleanly."""
        missing_rx = Prescription.objects.create(
            patient=self.patient1_profile,
            prescription_image='patient/prescriptions/non_existent.jpg',
            status='COMPLETED'
        )
        missing_url = reverse('patient:prescription_secure_image', kwargs={'pk': missing_rx.id})
        self.client.login(username='secure_patient1', password='TestPassword123!')
        response = self.client.get(missing_url)
        self.assertEqual(response.status_code, 404)
        missing_rx.delete()



from unittest.mock import patch, MagicMock
from io import BytesIO
import urllib.error
from django.core.cache import cache
from patient.views import calculate_haversine_distance, fetch_osm_pharmacies, OsmResultList


class NearbyPharmacyTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()

        # Patient user
        self.patient_user = User.objects.create_user(
            username='nearby_patient',
            email='nearby@example.com',
            password='TestPassword123!',
            role='PATIENT',
            first_name='Kavita'
        )
        self.patient_profile = PatientProfile.objects.create(
            user=self.patient_user,
            current_latitude=Decimal('19.0760000'),
            current_longitude=Decimal('72.8777000')
        )

        self.page_url = reverse('patient:nearby_pharmacies')
        self.api_url = reverse('patient:nearby_pharmacies_api')

    def tearDown(self):
        cache.clear()

    def test_haversine_distance_calculation(self):
        """Haversine formula computes accurate great-circle distance in kilometers."""
        # Distance between Mumbai CST (18.9400, 72.8353) and Gateway of India (18.9220, 72.8347) ~ 2.0 km
        dist = calculate_haversine_distance(18.9400, 72.8353, 18.9220, 72.8347)
        self.assertAlmostEqual(dist, 2.0, delta=0.2)

    def test_anonymous_user_redirected_to_login(self):
        """Unauthenticated requests to nearby pharmacy page and API are redirected."""
        self.assertEqual(self.client.get(self.page_url).status_code, 302)
        self.assertEqual(self.client.get(f"{self.api_url}?lat=19.0&lon=72.0").status_code, 302)

    def test_pharmacy_and_admin_users_denied_access(self):
        """Pharmacy and Admin roles are not permitted to access patient nearby pharmacies."""
        # 1. Pharmacy user
        User.objects.create_user(
            username='pharmacy_geo',
            email='pharmacy_geo@example.com',
            password='TestPassword123!',
            role='PHARMACY'
        )
        self.client.login(username='pharmacy_geo', password='TestPassword123!')
        self.assertEqual(self.client.get(self.page_url).status_code, 302)
        self.assertEqual(self.client.get(f"{self.api_url}?lat=19.0&lon=72.0").status_code, 302)

        # 2. Admin user
        User.objects.create_superuser(
            username='admin_geo',
            email='admin_geo@example.com',
            password='TestPassword123!'
        )
        self.client.login(username='admin_geo', password='TestPassword123!')
        self.assertEqual(self.client.get(self.page_url).status_code, 302)
        self.assertEqual(self.client.get(f"{self.api_url}?lat=19.0&lon=72.0").status_code, 302)

    def test_patient_can_view_nearby_pharmacies_page(self):
        """Authenticated patient views the Leaflet map nearby pharmacies interface."""
        self.client.login(username='nearby_patient', password='TestPassword123!')
        response = self.client.get(self.page_url)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'patient/nearby_pharmacies.html')
        self.assertContains(response, 'Find Nearby Pharmacies')
        self.assertContains(response, 'pharmacy-map')

    @patch('patient.views.fetch_osm_pharmacies')
    def test_approved_pharmacies_displayed_and_pending_rejected_excluded(self, mock_osm):
        """Only APPROVED pharmacies appear; PENDING and REJECTED are excluded."""
        mock_osm.return_value = []

        # 1. Approved Pharmacy (1 km away)
        user_app = User.objects.create_user(username='pharma_app', email='app@pharma.com', password='Password123!', role='PHARMACY')
        PharmacyProfile.objects.create(
            user=user_app,
            pharmacy_name='Approved Wellness Pharmacy',
            owner_name='Owner 1',
            pharmacist_name='Pharm 1',
            pharmacy_license_number='LIC-APP-1',
            gst_number='GST1111111111',
            phone_number='9876540001',
            email='app@pharma.com',
            address='1st Lane, Bandra',
            city='Mumbai',
            state='Maharashtra',
            pincode='400050',
            latitude=Decimal('19.0800000'),
            longitude=Decimal('72.8800000'),
            approval_status='APPROVED'
        )

        # 2. Pending Pharmacy (same coords)
        user_pen = User.objects.create_user(username='pharma_pen', email='pen@pharma.com', password='Password123!', role='PHARMACY')
        PharmacyProfile.objects.create(
            user=user_pen,
            pharmacy_name='Pending Meds Store',
            owner_name='Owner 2',
            pharmacist_name='Pharm 2',
            pharmacy_license_number='LIC-PEN-2',
            gst_number='GST2222222222',
            phone_number='9876540002',
            email='pen@pharma.com',
            address='2nd Lane, Bandra',
            city='Mumbai',
            state='Maharashtra',
            pincode='400050',
            latitude=Decimal('19.0800000'),
            longitude=Decimal('72.8800000'),
            approval_status='PENDING'
        )

        # 3. Rejected Pharmacy (same coords)
        user_rej = User.objects.create_user(username='pharma_rej', email='rej@pharma.com', password='Password123!', role='PHARMACY')
        PharmacyProfile.objects.create(
            user=user_rej,
            pharmacy_name='Rejected Pharmacy',
            owner_name='Owner 3',
            pharmacist_name='Pharm 3',
            pharmacy_license_number='LIC-REJ-3',
            gst_number='GST3333333333',
            phone_number='9876540003',
            email='rej@pharma.com',
            address='3rd Lane, Bandra',
            city='Mumbai',
            state='Maharashtra',
            pincode='400050',
            latitude=Decimal('19.0800000'),
            longitude=Decimal('72.8800000'),
            approval_status='REJECTED'
        )

        self.client.login(username='nearby_patient', password='TestPassword123!')
        response = self.client.get(f"{self.api_url}?lat=19.0760&lon=72.8777&radius=10")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['medaccess_count'], 1)

        names = [p['name'] for p in data['pharmacies']]
        self.assertIn('Approved Wellness Pharmacy', names)
        self.assertNotIn('Pending Meds Store', names)
        self.assertNotIn('Rejected Pharmacy', names)

    @patch('patient.views.fetch_osm_pharmacies')
    def test_missing_coordinates_handled_safely(self, mock_osm):
        """Pharmacies without latitude or longitude are excluded safely without errors."""
        mock_osm.return_value = []

        user_nocoords = User.objects.create_user(username='pharma_nocoords', email='nocoords@pharma.com', password='Password123!', role='PHARMACY')
        PharmacyProfile.objects.create(
            user=user_nocoords,
            pharmacy_name='No GPS Pharmacy',
            owner_name='Owner NoCoords',
            pharmacist_name='Pharm NoCoords',
            pharmacy_license_number='LIC-NOCOORDS',
            gst_number='GST4444444444',
            phone_number='9876540004',
            email='nocoords@pharma.com',
            address='Remote Area',
            city='Mumbai',
            state='Maharashtra',
            pincode='400001',
            latitude=None,
            longitude=None,
            approval_status='APPROVED'
        )

        self.client.login(username='nearby_patient', password='TestPassword123!')
        response = self.client.get(f"{self.api_url}?lat=19.0760&lon=72.8777&radius=10")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        names = [p['name'] for p in data['pharmacies']]
        self.assertNotIn('No GPS Pharmacy', names)

    @patch('patient.views.fetch_osm_pharmacies')
    def test_distance_sorting_and_radius_filter(self, mock_osm):
        """Results are sorted nearest first, and pharmacies outside radius are excluded."""
        mock_osm.return_value = []

        # Near Pharmacy (~0.5 km away)
        user_near = User.objects.create_user(username='pharma_near', email='near@pharma.com', password='Password123!', role='PHARMACY')
        PharmacyProfile.objects.create(
            user=user_near,
            pharmacy_name='Near Pharmacy',
            owner_name='Owner Near',
            pharmacist_name='Pharm Near',
            pharmacy_license_number='LIC-NEAR',
            gst_number='GST5555555555',
            phone_number='9876540005',
            email='near@pharma.com',
            address='Near Street',
            city='Mumbai',
            state='Maharashtra',
            pincode='400050',
            latitude=Decimal('19.0780000'),
            longitude=Decimal('72.8790000'),
            approval_status='APPROVED'
        )

        # Mid Pharmacy (~4 km away)
        user_mid = User.objects.create_user(username='pharma_mid', email='mid@pharma.com', password='Password123!', role='PHARMACY')
        PharmacyProfile.objects.create(
            user=user_mid,
            pharmacy_name='Mid Pharmacy',
            owner_name='Owner Mid',
            pharmacist_name='Pharm Mid',
            pharmacy_license_number='LIC-MID',
            gst_number='GST6666666666',
            phone_number='9876540006',
            email='mid@pharma.com',
            address='Mid Street',
            city='Mumbai',
            state='Maharashtra',
            pincode='400050',
            latitude=Decimal('19.1100000'),
            longitude=Decimal('72.8900000'),
            approval_status='APPROVED'
        )

        # Far Pharmacy (~30 km away, outside 10 km radius)
        user_far = User.objects.create_user(username='pharma_far', email='far@pharma.com', password='Password123!', role='PHARMACY')
        PharmacyProfile.objects.create(
            user=user_far,
            pharmacy_name='Far Pharmacy',
            owner_name='Owner Far',
            pharmacist_name='Pharm Far',
            pharmacy_license_number='LIC-FAR',
            gst_number='GST7777777777',
            phone_number='9876540007',
            email='far@pharma.com',
            address='Far Street',
            city='Thane',
            state='Maharashtra',
            pincode='400601',
            latitude=Decimal('19.3500000'),
            longitude=Decimal('72.9500000'),
            approval_status='APPROVED'
        )

        self.client.login(username='nearby_patient', password='TestPassword123!')
        response = self.client.get(f"{self.api_url}?lat=19.0760&lon=72.8777&radius=10")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['total_count'], 2)

        # Nearest first verification
        self.assertEqual(data['pharmacies'][0]['name'], 'Near Pharmacy')
        self.assertEqual(data['pharmacies'][1]['name'], 'Mid Pharmacy')
        self.assertTrue(data['pharmacies'][0]['distance_km'] < data['pharmacies'][1]['distance_km'])

        # Far pharmacy excluded
        names = [p['name'] for p in data['pharmacies']]
        self.assertNotIn('Far Pharmacy', names)

    @patch('urllib.request.urlopen')
    def test_osm_api_failure_handled_gracefully(self, mock_urlopen):
        """When OpenStreetMap Overpass API fails or times out, fetch_osm_pharmacies returns empty list safely."""
        mock_urlopen.side_effect = Exception("Overpass timeout")

        results = fetch_osm_pharmacies(19.0760, 72.8777, 10.0)
        self.assertEqual(results, [])

    @patch('patient.views.fetch_osm_pharmacies')
    def test_public_and_registered_pharmacy_labels_are_distinct(self, mock_osm):
        """MedAccess verified and OpenStreetMap public listings carry distinct labels and sources."""
        # Mock 1 public OSM pharmacy
        mock_osm.return_value = [{
            'id': 'osm_999999',
            'name': 'Public Chemist OSM',
            'source': 'OPENSTREETMAP',
            'is_verified': False,
            'badge_label': 'OpenStreetMap Listing',
            'address': 'Main Road, Chembur',
            'phone_number': '',
            'latitude': 19.0810,
            'longitude': 72.8810,
            'distance_km': 0.65
        }]

        # 1 MedAccess registered pharmacy
        user_reg = User.objects.create_user(username='pharma_reg', email='reg@pharma.com', password='Password123!', role='PHARMACY')
        PharmacyProfile.objects.create(
            user=user_reg,
            pharmacy_name='Registered MedAccess Chemist',
            owner_name='Owner Reg',
            pharmacist_name='Pharm Reg',
            pharmacy_license_number='LIC-REG',
            gst_number='GST8888888888',
            phone_number='9876540008',
            email='reg@pharma.com',
            address='Bandra West',
            city='Mumbai',
            state='Maharashtra',
            pincode='400050',
            latitude=Decimal('19.0790000'),
            longitude=Decimal('72.8790000'),
            approval_status='APPROVED'
        )

        self.client.login(username='nearby_patient', password='TestPassword123!')
        response = self.client.get(f"{self.api_url}?lat=19.0760&lon=72.8777&radius=10")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['total_count'], 2)
        self.assertEqual(data['medaccess_count'], 1)
        self.assertEqual(data['public_count'], 1)

        med_item = next(p for p in data['pharmacies'] if p['name'] == 'Registered MedAccess Chemist')
        osm_item = next(p for p in data['pharmacies'] if p['name'] == 'Public Chemist OSM')

        self.assertEqual(med_item['source'], 'MEDACCESS')
        self.assertTrue(med_item['is_verified'])
        self.assertEqual(med_item['badge_label'], 'MedAccess Verified')

        self.assertEqual(osm_item['source'], 'OPENSTREETMAP')
        self.assertFalse(osm_item['is_verified'])
        self.assertEqual(osm_item['badge_label'], 'OpenStreetMap Listing')

    def test_invalid_and_out_of_range_coordinates_rejected(self):
        """API rejects invalid and out-of-range coordinates with 400 Bad Request."""
        self.client.login(username='nearby_patient', password='TestPassword123!')

        # Missing params
        resp_missing = self.client.get(self.api_url)
        self.assertEqual(resp_missing.status_code, 400)

        # Invalid format
        resp_invalid = self.client.get(f"{self.api_url}?lat=not_a_num&lon=72.0")
        self.assertEqual(resp_invalid.status_code, 400)

        # Latitude > 90
        resp_out_of_range = self.client.get(f"{self.api_url}?lat=120.0&lon=72.0")
        self.assertEqual(resp_out_of_range.status_code, 400)

    @patch('urllib.request.urlopen')
    def test_overpass_endpoint_fallback_and_parsing(self, mock_urlopen):
        """When first Overpass mirror fails with 502, second mirror succeeds and parses elements."""
        cache.clear()

        # Endpoint 1 fails with 502 Bad Gateway
        err_502 = urllib.error.HTTPError(
            'https://overpass-api.de/api/interpreter', 502, 'Bad Gateway', {}, None
        )

        # Endpoint 2 succeeds with Overpass elements
        overpass_payload = {
            'elements': [
                {
                    'id': 101,
                    'type': 'node',
                    'lat': 19.0770,
                    'lon': 72.8780,
                    'tags': {
                        'name': 'Fallback Overpass Chemist',
                        'addr:street': 'Station Road',
                        'addr:city': 'Mumbai',
                        'phone': '022-12345678'
                    }
                }
            ]
        }
        mock_resp_2 = MagicMock()
        mock_resp_2.status = 200
        mock_resp_2.read.return_value = json.dumps(overpass_payload).encode('utf-8')
        mock_resp_2.__enter__.return_value = mock_resp_2

        mock_urlopen.side_effect = [err_502, mock_resp_2]

        results = fetch_osm_pharmacies(19.0760, 72.8777, 10.0)
        self.assertIsNone(getattr(results, 'osm_error', None))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['name'], 'Fallback Overpass Chemist')
        self.assertEqual(results[0]['source'], 'OPENSTREETMAP')
        self.assertIn('Station Road', results[0]['address'])

    @patch('urllib.request.urlopen')
    def test_overpass_all_fail_nominatim_fallback_succeeds(self, mock_urlopen):
        """When Overpass mirrors time out, Nominatim search API succeeds as official fallback."""
        cache.clear()

        # Nominatim returns OpenStreetMap elements
        nominatim_payload = [
            {
                'place_id': 9991,
                'osm_type': 'node',
                'osm_id': 555001,
                'lat': '19.0780',
                'lon': '72.8790',
                'name': 'Nominatim Rescue Meds',
                'display_name': 'Nominatim Rescue Meds, Bandra East, Mumbai, Maharashtra, 400051',
                'address': {'amenity': 'Nominatim Rescue Meds'},
                'extratags': {'phone': '022-99887766'}
            }
        ]
        nom_mock = MagicMock()
        nom_mock.status = 200
        nom_mock.read.return_value = json.dumps(nominatim_payload).encode('utf-8')
        nom_mock.__enter__.return_value = nom_mock

        # All Overpass mirrors fail (raise URLError/timeout), then Nominatim succeeds
        def mock_side_effect(req, *args, **kwargs):
            url = req.full_url if hasattr(req, 'full_url') else str(req)
            if 'nominatim.openstreetmap.org' in url:
                return nom_mock
            raise urllib.error.URLError("Connection timed out")

        mock_urlopen.side_effect = mock_side_effect

        results = fetch_osm_pharmacies(19.0760, 72.8777, 10.0)
        self.assertIsNone(getattr(results, 'osm_error', None))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['name'], 'Nominatim Rescue Meds')
        self.assertEqual(results[0]['osm_id'], 555001)

    @patch('patient.views.fetch_osm_pharmacies')
    def test_api_distinguishes_empty_results_from_api_failure(self, mock_osm):
        """API returns 'warning' and osm_unavailable=True when service fails, vs 'success' when 0 found."""
        self.client.login(username='nearby_patient', password='TestPassword123!')

        # Scenario 1: External API Failure (unreachable)
        mock_osm.return_value = OsmResultList([], osm_error="OpenStreetMap public pharmacy directory is temporarily unreachable.")
        resp_fail = self.client.get(f"{self.api_url}?lat=19.0760&lon=72.8777&radius=10")
        self.assertEqual(resp_fail.status_code, 200)
        data_fail = resp_fail.json()
        self.assertEqual(data_fail['status'], 'warning')
        self.assertTrue(data_fail['osm_unavailable'])
        self.assertIn('temporarily unreachable', data_fail['message'])

        # Scenario 2: Legitimate Empty Results (search worked, but 0 pharmacies exist there)
        mock_osm.return_value = OsmResultList([], osm_error=None)
        resp_ok = self.client.get(f"{self.api_url}?lat=19.0760&lon=72.8777&radius=10")
        self.assertEqual(resp_ok.status_code, 200)
        data_ok = resp_ok.json()
        self.assertEqual(data_ok['status'], 'success')
        self.assertFalse(data_ok['osm_unavailable'])
        self.assertEqual(data_ok['total_count'], 0)

    @patch('patient.views.fetch_osm_pharmacies')
    def test_osm_failure_preserves_medaccess_verified_pharmacies(self, mock_osm):
        """If OpenStreetMap fails, registered MedAccess pharmacies are still returned with a warning."""
        mock_osm.return_value = OsmResultList([], osm_error="OpenStreetMap public pharmacy directory is temporarily unreachable.")

        # Create approved MedAccess pharmacy in range
        user_med = User.objects.create_user(username='pharma_resilient', email='resilient@pharma.com', password='Password123!', role='PHARMACY')
        PharmacyProfile.objects.create(
            user=user_med,
            pharmacy_name='Resilient MedAccess Care',
            owner_name='Owner Resilient',
            pharmacist_name='Pharm Resilient',
            pharmacy_license_number='LIC-RESILIENT',
            gst_number='GST9999999999',
            phone_number='9876540099',
            email='resilient@pharma.com',
            address='Linking Road',
            city='Mumbai',
            state='Maharashtra',
            pincode='400050',
            latitude=Decimal('19.0780000'),
            longitude=Decimal('72.8780000'),
            approval_status='APPROVED'
        )

        self.client.login(username='nearby_patient', password='TestPassword123!')
        response = self.client.get(f"{self.api_url}?lat=19.0760&lon=72.8777&radius=10")
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['total_count'], 1)
        self.assertEqual(data['medaccess_count'], 1)
        self.assertEqual(data['public_count'], 0)
        self.assertTrue(data['osm_unavailable'])
        self.assertIn('temporarily unreachable', data['osm_warning'])
        self.assertEqual(data['pharmacies'][0]['name'], 'Resilient MedAccess Care')


class SavedPharmacyTests(TestCase):
    def setUp(self):
        self.client = Client()

        # 1. Patient User 1
        self.user_p1 = User.objects.create_user(
            username='patient_one',
            email='patient_one@example.com',
            password='Password123!',
            role='PATIENT',
            first_name='Ankit'
        )
        self.profile_p1 = PatientProfile.objects.create(user=self.user_p1)

        # 2. Patient User 2
        self.user_p2 = User.objects.create_user(
            username='patient_two',
            email='patient_two@example.com',
            password='Password123!',
            role='PATIENT',
            first_name='Bhavna'
        )
        self.profile_p2 = PatientProfile.objects.create(user=self.user_p2)

        # 3. Approved Pharmacy 1
        self.pharm_user_1 = User.objects.create_user(
            username='pharma_approved_1',
            email='ap1@medaccess.com',
            password='Password123!',
            role='PHARMACY'
        )
        self.pharmacy_approved_1 = PharmacyProfile.objects.create(
            user=self.pharm_user_1,
            pharmacy_name='Apollo Pharmacy Bandra',
            owner_name='Vikas Khanna',
            pharmacist_name='Sneha Patil',
            pharmacy_license_number='LIC-AP1',
            gst_number='GST1234567890',
            phone_number='9876500001',
            email='ap1@medaccess.com',
            address='Hill Road, Bandra West',
            city='Mumbai',
            state='Maharashtra',
            pincode='400050',
            latitude=Decimal('19.0550000'),
            longitude=Decimal('72.8300000'),
            approval_status='APPROVED'
        )

        # 4. Approved Pharmacy 2
        self.pharm_user_2 = User.objects.create_user(
            username='pharma_approved_2',
            email='ap2@medaccess.com',
            password='Password123!',
            role='PHARMACY'
        )
        self.pharmacy_approved_2 = PharmacyProfile.objects.create(
            user=self.pharm_user_2,
            pharmacy_name='Wellness Forever Chembur',
            owner_name='Ashok Mehta',
            pharmacist_name='Rohan Nair',
            pharmacy_license_number='LIC-AP2',
            gst_number='GST9876543210',
            phone_number='9876500002',
            email='ap2@medaccess.com',
            address='Sion Trombay Road, Chembur',
            city='Mumbai',
            state='Maharashtra',
            pincode='400071',
            latitude=Decimal('19.0600000'),
            longitude=Decimal('72.8900000'),
            approval_status='APPROVED'
        )

        # 5. Pending Pharmacy
        self.pharm_user_pen = User.objects.create_user(
            username='pharma_pen',
            email='pen@medaccess.com',
            password='Password123!',
            role='PHARMACY'
        )
        self.pharmacy_pending = PharmacyProfile.objects.create(
            user=self.pharm_user_pen,
            pharmacy_name='Pending Pharmacy Store',
            owner_name='Pending Owner',
            pharmacist_name='Pending Pharm',
            pharmacy_license_number='LIC-PEN',
            gst_number='GST1111222233',
            phone_number='9876500003',
            email='pen@medaccess.com',
            address='Khar West',
            city='Mumbai',
            state='Maharashtra',
            pincode='400052',
            latitude=Decimal('19.0700000'),
            longitude=Decimal('72.8400000'),
            approval_status='PENDING'
        )

        # 6. Rejected Pharmacy
        self.pharm_user_rej = User.objects.create_user(
            username='pharma_rej',
            email='rej@medaccess.com',
            password='Password123!',
            role='PHARMACY'
        )
        self.pharmacy_rejected = PharmacyProfile.objects.create(
            user=self.pharm_user_rej,
            pharmacy_name='Rejected Pharmacy Store',
            owner_name='Rejected Owner',
            pharmacist_name='Rejected Pharm',
            pharmacy_license_number='LIC-REJ',
            gst_number='GST4444555566',
            phone_number='9876500004',
            email='rej@medaccess.com',
            address='Santacruz West',
            city='Mumbai',
            state='Maharashtra',
            pincode='400054',
            latitude=Decimal('19.0800000'),
            longitude=Decimal('72.8400000'),
            approval_status='REJECTED'
        )

        self.list_url = reverse('patient:saved_pharmacies')
        self.save_url = reverse('patient:save_pharmacy')
        self.dashboard_url = reverse('patient:dashboard')

    def test_anonymous_users_cannot_access_page_or_actions(self):
        """Unauthenticated requests to saved pharmacies views are redirected to login."""
        self.assertEqual(self.client.get(self.list_url).status_code, 302)
        self.assertEqual(self.client.post(self.save_url, {'pharmacy_id': self.pharmacy_approved_1.id}).status_code, 302)
        remove_url = reverse('patient:remove_saved_pharmacy', kwargs={'pk': 1})
        self.assertEqual(self.client.post(remove_url).status_code, 302)

    def test_only_patient_role_can_access_feature(self):
        """Users with PHARMACY or ADMIN roles cannot access saved pharmacies."""
        # 1. Pharmacy user
        self.client.login(username='pharma_approved_1', password='Password123!')
        self.assertEqual(self.client.get(self.list_url).status_code, 302)
        self.assertEqual(self.client.post(self.save_url, {'pharmacy_id': self.pharmacy_approved_1.id}).status_code, 302)

        # 2. Admin user
        admin = User.objects.create_superuser(username='admin_staff', email='admin@test.com', password='Password123!')
        self.client.login(username='admin_staff', password='Password123!')
        self.assertEqual(self.client.get(self.list_url).status_code, 302)
        self.assertEqual(self.client.post(self.save_url, {'pharmacy_id': self.pharmacy_approved_1.id}).status_code, 302)

    def test_patient_can_save_approved_medaccess_pharmacy(self):
        """Patient successfully bookmarks an approved MedAccess pharmacy via form POST."""
        self.client.login(username='patient_one', password='Password123!')
        response = self.client.post(self.save_url, {'pharmacy_id': self.pharmacy_approved_1.id}, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Apollo Pharmacy Bandra')
        self.assertContains(response, 'has been added to your saved pharmacies')

        # Check DB entry
        saved = SavedPharmacy.objects.filter(patient=self.profile_p1, pharmacy=self.pharmacy_approved_1).first()
        self.assertIsNotNone(saved)
        self.assertEqual(saved.patient, self.profile_p1)
        self.assertEqual(saved.pharmacy, self.pharmacy_approved_1)
        self.assertIsNotNone(saved.saved_at)

    def test_patient_can_save_via_ajax_json(self):
        """Patient can bookmark an approved pharmacy via AJAX JSON payload."""
        self.client.login(username='patient_one', password='Password123!')
        response = self.client.post(
            self.save_url,
            data=json.dumps({'pharmacy_id': self.pharmacy_approved_1.id}),
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'success')
        self.assertFalse(data['already_saved'])
        self.assertIn('Apollo Pharmacy Bandra', data['message'])

    def test_duplicate_save_is_prevented(self):
        """Saving an already-saved pharmacy prevents duplication and returns friendly notice."""
        self.client.login(username='patient_one', password='Password123!')

        # First save
        self.client.post(self.save_url, {'pharmacy_id': self.pharmacy_approved_1.id})
        self.assertEqual(SavedPharmacy.objects.filter(patient=self.profile_p1).count(), 1)

        # Duplicate save attempt
        response = self.client.post(self.save_url, {'pharmacy_id': self.pharmacy_approved_1.id}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'is already in your saved list')

        # Count in DB remains 1
        self.assertEqual(SavedPharmacy.objects.filter(patient=self.profile_p1).count(), 1)

        # AJAX duplicate check
        ajax_resp = self.client.post(
            self.save_url,
            data=json.dumps({'pharmacy_id': self.pharmacy_approved_1.id}),
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(ajax_resp.status_code, 200)
        self.assertTrue(ajax_resp.json()['already_saved'])

    def test_patient_can_view_only_their_own_saved_pharmacies(self):
        """Patient 1 sees only their saved pharmacies and cannot see Patient 2's saved list."""
        SavedPharmacy.objects.create(patient=self.profile_p1, pharmacy=self.pharmacy_approved_1)
        SavedPharmacy.objects.create(patient=self.profile_p2, pharmacy=self.pharmacy_approved_2)

        # Patient 1 checks list
        self.client.login(username='patient_one', password='Password123!')
        resp_p1 = self.client.get(self.list_url)
        self.assertEqual(resp_p1.status_code, 200)
        self.assertContains(resp_p1, 'Apollo Pharmacy Bandra')
        self.assertNotContains(resp_p1, 'Wellness Forever Chembur')

        # Patient 2 checks list
        self.client.login(username='patient_two', password='Password123!')
        resp_p2 = self.client.get(self.list_url)
        self.assertEqual(resp_p2.status_code, 200)
        self.assertContains(resp_p2, 'Wellness Forever Chembur')
        self.assertNotContains(resp_p2, 'Apollo Pharmacy Bandra')

    def test_empty_saved_pharmacies_state_displays_cleanly(self):
        """When no pharmacies are saved, clean empty state with nearby pharmacies CTA is shown."""
        self.client.login(username='patient_one', password='Password123!')
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No Saved Pharmacies Yet')
        self.assertContains(response, 'Find Nearby Pharmacies')

    def test_patient_can_remove_only_their_own_saved_pharmacies(self):
        """Patient 1 can remove their own saved pharmacy but cannot remove Patient 2's saved record."""
        saved_p1 = SavedPharmacy.objects.create(patient=self.profile_p1, pharmacy=self.pharmacy_approved_1)
        saved_p2 = SavedPharmacy.objects.create(patient=self.profile_p2, pharmacy=self.pharmacy_approved_2)

        self.client.login(username='patient_one', password='Password123!')

        # 1. Patient 1 tries to remove Patient 2's saved record -> 404 Not Found!
        p2_remove_url = reverse('patient:remove_saved_pharmacy', kwargs={'pk': saved_p2.id})
        resp_tamper = self.client.post(p2_remove_url)
        self.assertEqual(resp_tamper.status_code, 404)
        self.assertTrue(SavedPharmacy.objects.filter(id=saved_p2.id).exists())

        # 2. Patient 1 removes their own saved record -> Success
        p1_remove_url = reverse('patient:remove_saved_pharmacy', kwargs={'pk': saved_p1.id})
        resp_remove = self.client.post(p1_remove_url, follow=True)
        self.assertEqual(resp_remove.status_code, 200)
        self.assertContains(resp_remove, 'has been removed from your saved pharmacies')
        self.assertFalse(SavedPharmacy.objects.filter(id=saved_p1.id).exists())

    def test_pending_and_rejected_pharmacies_cannot_be_saved(self):
        """Attempting to save PENDING or REJECTED pharmacies returns error and creates no record."""
        self.client.login(username='patient_one', password='Password123!')

        # Pending pharmacy
        resp_pen = self.client.post(self.save_url, {'pharmacy_id': self.pharmacy_pending.id})
        self.assertEqual(resp_pen.status_code, 302)
        self.assertFalse(SavedPharmacy.objects.filter(pharmacy=self.pharmacy_pending).exists())

        # Rejected pharmacy
        resp_rej = self.client.post(self.save_url, {'pharmacy_id': self.pharmacy_rejected.id})
        self.assertEqual(resp_rej.status_code, 302)
        self.assertFalse(SavedPharmacy.objects.filter(pharmacy=self.pharmacy_rejected).exists())

    def test_get_requests_cannot_perform_save_or_remove_actions(self):
        """GET requests to save or remove endpoints return HTTP 405 Method Not Allowed."""
        self.client.login(username='patient_one', password='Password123!')
        saved_record = SavedPharmacy.objects.create(patient=self.profile_p1, pharmacy=self.pharmacy_approved_1)
        remove_url = reverse('patient:remove_saved_pharmacy', kwargs={'pk': saved_record.id})

        resp_save = self.client.get(self.save_url)
        self.assertEqual(resp_save.status_code, 405)

        resp_remove = self.client.get(remove_url)
        self.assertEqual(resp_remove.status_code, 405)

    @patch('patient.views.fetch_osm_pharmacies')
    def test_both_medaccess_and_osm_listings_support_saved_status_in_api(self, mock_osm):
        """API returns is_saved flag for both MedAccess and OpenStreetMap listings while keeping sources distinct."""
        mock_osm.return_value = [{
            'id': 'osm_8888',
            'osm_type': 'node',
            'osm_id': 8888,
            'name': 'Public Chemist',
            'source': 'OPENSTREETMAP',
            'is_verified': False,
            'badge_label': 'OpenStreetMap Listing',
            'address': 'Public Lane',
            'phone_number': '',
            'latitude': 19.055,
            'longitude': 72.830,
            'distance_km': 1.0
        }]

        self.client.login(username='patient_one', password='Password123!')
        api_url = reverse('patient:nearby_pharmacies_api')
        response = self.client.get(f"{api_url}?lat=19.055&lon=72.830&radius=10")

        self.assertEqual(response.status_code, 200)
        data = response.json()

        med_item = next(p for p in data['pharmacies'] if p['source'] == 'MEDACCESS')
        osm_item = next(p for p in data['pharmacies'] if p['source'] == 'OPENSTREETMAP')

        self.assertIn('is_saved', med_item)
        self.assertFalse(med_item['is_saved'])
        self.assertIn('is_saved', osm_item)
        self.assertFalse(osm_item['is_saved'])
        self.assertEqual(osm_item['source'], 'OPENSTREETMAP')
        self.assertEqual(med_item['source'], 'MEDACCESS')

    def test_patient_can_save_osm_public_pharmacy(self):
        """Patient can save an OpenStreetMap public pharmacy snapshot."""
        self.client.login(username='patient_one', password='Password123!')
        payload = {
            'source': 'OPENSTREETMAP',
            'osm_type': 'node',
            'osm_id': 123456789,
            'pharmacy_name': 'Bandra Public Chemist',
            'address': 'Linking Road, Bandra West',
            'latitude': '19.0605000',
            'longitude': '72.8355000',
            'phone_number': '022-26401234'
        }
        response = self.client.post(self.save_url, payload, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Bandra Public Chemist')
        self.assertContains(response, 'OpenStreetMap Listing')

        saved = SavedPharmacy.objects.filter(patient=self.profile_p1, osm_id=123456789).first()
        self.assertIsNotNone(saved)
        self.assertIsNone(saved.pharmacy)
        self.assertTrue(saved.is_osm)
        self.assertFalse(saved.is_medaccess)
        self.assertEqual(saved.osm_type, 'node')
        self.assertEqual(saved.display_name, 'Bandra Public Chemist')
        self.assertEqual(saved.display_source, 'OpenStreetMap Listing')
        self.assertEqual(saved.phone_number, '022-26401234')

    def test_patient_can_save_osm_via_ajax_json(self):
        """Patient can save an OpenStreetMap pharmacy via AJAX JSON payload."""
        self.client.login(username='patient_one', password='Password123!')
        payload = {
            'source': 'OPENSTREETMAP',
            'osm_type': 'way',
            'osm_id': 987654321,
            'pharmacy_name': 'Khar Community Pharmacy',
            'address': 'SV Road, Khar West',
            'latitude': 19.0680,
            'longitude': 72.8390,
            'phone_number': ''
        }
        response = self.client.post(
            self.save_url,
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'success')
        self.assertFalse(data['already_saved'])
        self.assertIsNotNone(data['saved_id'])

    def test_duplicate_save_prevented_for_osm_pharmacy(self):
        """A patient cannot save the exact same OSM pharmacy twice."""
        self.client.login(username='patient_one', password='Password123!')
        payload = {
            'source': 'OPENSTREETMAP',
            'osm_type': 'node',
            'osm_id': 555555,
            'pharmacy_name': 'Duplicate Test Store',
            'address': 'Test Road',
            'latitude': 19.070,
            'longitude': 72.840
        }
        # First save
        self.client.post(self.save_url, payload)
        self.assertEqual(SavedPharmacy.objects.filter(patient=self.profile_p1, osm_id=555555).count(), 1)

        # Second save attempt
        resp2 = self.client.post(self.save_url, payload, follow=True)
        self.assertEqual(resp2.status_code, 200)
        self.assertContains(resp2, 'is already in your saved list')
        self.assertEqual(SavedPharmacy.objects.filter(patient=self.profile_p1, osm_id=555555).count(), 1)

    def test_invalid_osm_type_and_out_of_range_coords_rejected(self):
        """Invalid OSM element type and invalid coordinates are rejected with HTTP 400."""
        self.client.login(username='patient_one', password='Password123!')

        # 1. Invalid osm_type
        bad_type_payload = {
            'source': 'OPENSTREETMAP',
            'osm_type': 'invalid_type',
            'osm_id': 1111,
            'latitude': 19.0,
            'longitude': 72.0
        }
        resp1 = self.client.post(self.save_url, bad_type_payload)
        self.assertEqual(resp1.status_code, 400)

        # 2. Out of range latitude
        bad_lat_payload = {
            'source': 'OPENSTREETMAP',
            'osm_type': 'node',
            'osm_id': 1111,
            'latitude': 95.0, # Latitude > 90
            'longitude': 72.0
        }
        resp2 = self.client.post(self.save_url, bad_lat_payload)
        self.assertEqual(resp2.status_code, 400)

    def test_patient_can_unsave_via_unsave_endpoint(self):
        """Patient can toggle unsave via the unsave endpoint for both MedAccess and OSM pharmacies."""
        self.client.login(username='patient_one', password='Password123!')
        unsave_url = reverse('patient:unsave_pharmacy')

        # 1. Unsave MedAccess pharmacy by pharmacy_id
        SavedPharmacy.objects.create(patient=self.profile_p1, pharmacy=self.pharmacy_approved_1)
        resp_med = self.client.post(
            unsave_url,
            data=json.dumps({'pharmacy_id': self.pharmacy_approved_1.id}),
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(resp_med.status_code, 200)
        self.assertFalse(SavedPharmacy.objects.filter(patient=self.profile_p1, pharmacy=self.pharmacy_approved_1).exists())

        # 2. Unsave OSM pharmacy by osm_type and osm_id
        saved_osm = SavedPharmacy.objects.create(
            patient=self.profile_p1,
            osm_type='node',
            osm_id=777777,
            pharmacy_name='OSM Unsave Test Store'
        )
        resp_osm = self.client.post(
            unsave_url,
            data=json.dumps({'osm_type': 'node', 'osm_id': 777777}),
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(resp_osm.status_code, 200)
        self.assertFalse(SavedPharmacy.objects.filter(id=saved_osm.id).exists())

    def test_saved_pharmacies_list_renders_both_medaccess_and_osm(self):
        """Saved pharmacies page displays both MedAccess verified and OSM public listing cards distinctly."""
        SavedPharmacy.objects.create(patient=self.profile_p1, pharmacy=self.pharmacy_approved_1)
        SavedPharmacy.objects.create(
            patient=self.profile_p1,
            osm_type='node',
            osm_id=888888,
            pharmacy_name='City Centre Public Chemist',
            address='Station Road, Bandra',
            latitude=Decimal('19.0550000'),
            longitude=Decimal('72.8300000'),
            phone_number='022-26009999'
        )

        self.client.login(username='patient_one', password='Password123!')
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Apollo Pharmacy Bandra')
        self.assertContains(response, 'City Centre Public Chemist')
        self.assertContains(response, 'MedAccess Verified')
        self.assertContains(response, 'OpenStreetMap Listing')
        self.assertContains(response, '022-26009999')

    def test_dashboard_saved_pharmacies_count_reflects_changes(self):
        """Dashboard dynamic count increments on save and decrements on remove."""
        self.client.login(username='patient_one', password='Password123!')

        # Initial count: 0
        resp_before = self.client.get(self.dashboard_url)
        self.assertEqual(resp_before.context['saved_pharmacies_count'], 0)

        # Save pharmacy (MedAccess)
        saved_med = SavedPharmacy.objects.create(patient=self.profile_p1, pharmacy=self.pharmacy_approved_1)
        resp_after_1 = self.client.get(self.dashboard_url)
        self.assertEqual(resp_after_1.context['saved_pharmacies_count'], 1)

        # Save pharmacy (OSM)
        saved_osm = SavedPharmacy.objects.create(
            patient=self.profile_p1,
            osm_type='node',
            osm_id=99999,
            pharmacy_name='Second Saved Store'
        )
        resp_after_2 = self.client.get(self.dashboard_url)
        self.assertEqual(resp_after_2.context['saved_pharmacies_count'], 2)

        # Remove both
        saved_med.delete()
        saved_osm.delete()
        resp_removed = self.client.get(self.dashboard_url)
        self.assertEqual(resp_removed.context['saved_pharmacies_count'], 0)


class PrescriptionManualMedicineTests(TestCase):
    """
    Tests for manual medicine entry, deletion, ownership protection, and clean UI rendering.
    """
    def setUp(self):
        self.client = Client()
        self.patient_user_1 = User.objects.create_user(
            username='med_patient_1',
            email='med1@example.com',
            password='TestPassword123!',
            role='PATIENT'
        )
        self.patient_profile_1 = PatientProfile.objects.create(
            user=self.patient_user_1,
            blood_group='B+'
        )

        self.patient_user_2 = User.objects.create_user(
            username='med_patient_2',
            email='med2@example.com',
            password='TestPassword123!',
            role='PATIENT'
        )
        self.patient_profile_2 = PatientProfile.objects.create(
            user=self.patient_user_2,
            blood_group='O+'
        )

        self.pharmacy_user = User.objects.create_user(
            username='pharma_user_med',
            email='pharmamed@example.com',
            password='TestPassword123!',
            role='PHARMACY'
        )

        # 1x1 GIF valid image bytes
        self.valid_image_bytes = (
            b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff'
            b'\x00\x00\x00\x21\xf9\x04\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00'
            b'\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'
        )
        self.test_image = SimpleUploadedFile(
            name='test_rx.png',
            content=self.valid_image_bytes,
            content_type='image/png'
        )
        self.prescription = Prescription.objects.create(
            patient=self.patient_profile_1,
            prescription_image=self.test_image,
            status='UPLOADED'
        )

    def test_confirm_medicine_view_creates_record(self):
        """Patient manually enters and saves a medicine for their prescription."""
        confirm_url = reverse('patient:confirm_prescription_medicine', kwargs={'pk': self.prescription.pk})
        detail_url = reverse('patient:prescription_detail', kwargs={'pk': self.prescription.pk})

        self.client.login(username='med_patient_1', password='TestPassword123!')
        payload = {
            'medicine_name': 'Azithromycin',
            'strength': '500mg',
            'dosage': '1 tablet once daily before meal',
            'duration': '3 days',
            'quantity': '3 tablets'
        }
        response = self.client.post(confirm_url, payload)

        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, detail_url)

        # Check medicine record created
        medicine = PrescriptionMedicine.objects.filter(
            prescription=self.prescription,
            medicine_name='Azithromycin'
        ).first()
        self.assertIsNotNone(medicine)
        self.assertEqual(medicine.strength, '500mg')
        self.assertEqual(medicine.dosage, '1 tablet once daily before meal')
        self.assertEqual(medicine.duration, '3 days')
        self.assertEqual(medicine.quantity, '3 tablets')

    def test_confirm_medicine_missing_name_rejected(self):
        """Manual entry form requires a valid medicine name."""
        confirm_url = reverse('patient:confirm_prescription_medicine', kwargs={'pk': self.prescription.pk})
        self.client.login(username='med_patient_1', password='TestPassword123!')

        response = self.client.post(confirm_url, {'medicine_name': ''})
        self.assertEqual(response.status_code, 302)

        # No record created
        self.assertEqual(PrescriptionMedicine.objects.filter(prescription=self.prescription).count(), 0)

    def test_delete_medicine_view_removes_record(self):
        """Patient can delete a manually entered medicine item."""
        medicine = PrescriptionMedicine.objects.create(
            prescription=self.prescription,
            medicine_name='Cough Syrup',
            strength='100ml',
            dosage='5ml twice daily'
        )
        delete_url = reverse(
            'patient:delete_prescription_medicine',
            kwargs={'pk': self.prescription.pk, 'med_id': medicine.pk}
        )
        detail_url = reverse('patient:prescription_detail', kwargs={'pk': self.prescription.pk})

        self.client.login(username='med_patient_1', password='TestPassword123!')
        response = self.client.post(delete_url)

        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, detail_url)
        self.assertFalse(PrescriptionMedicine.objects.filter(pk=medicine.pk).exists())

    def test_cross_patient_cannot_modify_medicines(self):
        """Patient 2 cannot add or delete medicines on Patient 1's prescription."""
        confirm_url = reverse('patient:confirm_prescription_medicine', kwargs={'pk': self.prescription.pk})
        medicine = PrescriptionMedicine.objects.create(
            prescription=self.prescription,
            medicine_name='Confidential Med'
        )
        delete_url = reverse(
            'patient:delete_prescription_medicine',
            kwargs={'pk': self.prescription.pk, 'med_id': medicine.pk}
        )

        self.client.login(username='med_patient_2', password='TestPassword123!')

        resp_confirm = self.client.post(confirm_url, {'medicine_name': 'Hacked Med'})
        self.assertEqual(resp_confirm.status_code, 404)

        resp_delete = self.client.post(delete_url)
        self.assertEqual(resp_delete.status_code, 404)

        # Ensure original medicine was not deleted
        self.assertTrue(PrescriptionMedicine.objects.filter(pk=medicine.pk).exists())

    def test_non_patient_role_denied_access(self):
        """Non-patient roles (e.g. PHARMACY) are blocked by @role_required('PATIENT')."""
        confirm_url = reverse('patient:confirm_prescription_medicine', kwargs={'pk': self.prescription.pk})

        self.client.login(username='pharma_user_med', password='TestPassword123!')

        resp_confirm = self.client.post(confirm_url, {'medicine_name': 'Pharma Injected'})
        self.assertEqual(resp_confirm.status_code, 302)
        self.assertRedirects(resp_confirm, reverse('landing'))

    def test_prescription_detail_renders_clean_view_without_ocr(self):
        """Prescription detail template displays clean view with document, medicines, and no OCR."""
        detail_url = reverse('patient:prescription_detail', kwargs={'pk': self.prescription.pk})
        self.client.login(username='med_patient_1', password='TestPassword123!')

        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, 200)

        # Should contain clean manual elements
        self.assertContains(response, 'Prescription Document')
        self.assertContains(response, 'Prescribed Medicines')
        self.assertContains(response, 'Add Prescribed Medicine')

        # Must NOT contain any OCR elements
        self.assertNotContains(response, 'Retry OCR')
        self.assertNotContains(response, 'Re-run OCR')
        self.assertNotContains(response, 'Raw OCR Extracted Text')
        self.assertNotContains(response, 'Medical Integrity Notice')
        self.assertNotContains(response, 'Text Extraction Incomplete')
        self.assertNotContains(response, 'OCR Processing in Progress')


class PrescriptionFileProcessorTests(TestCase):
    """
    Unit tests for Phase 3, Task 3A: Prescription File Processing Foundation.
    Tests content-based validation, Pillow image optimization, and PyMuPDF PDF rendering.
    """
    def _create_image_bytes(self, size=(400, 300), format_name='JPEG', mode='RGB', exif_orientation=None):
        buf = io.BytesIO()
        img = Image.new(mode, size, color=(200, 220, 240))
        save_kwargs = {}
        if exif_orientation:
            exif = img.getexif()
            exif[0x0112] = exif_orientation
            save_kwargs['exif'] = exif
        img.save(buf, format=format_name, **save_kwargs)
        return buf.getvalue()

    def _create_pdf_bytes(self, page_count=1, encrypted=False, password='password123'):
        doc = pymupdf.open()
        for i in range(page_count):
            page = doc.new_page(width=300, height=400)
            page.insert_text((40, 50), f"Dummy Rx Page {i+1}")
        
        save_kwargs = {}
        if encrypted:
            save_kwargs['encryption'] = pymupdf.PDF_ENCRYPT_AES_256
            save_kwargs['user_pw'] = password
            save_kwargs['owner_pw'] = password

        pdf_bytes = doc.tobytes(**save_kwargs)
        doc.close()
        return pdf_bytes

    def test_valid_jpeg_processing(self):
        """Valid JPEG image is processed into an optimized JPEG page."""
        from patient.services.prescription_processor import process_prescription_file

        data = self._create_image_bytes(size=(600, 800), format_name='JPEG')
        result = process_prescription_file(data)

        self.assertEqual(result.file_type, 'image')
        self.assertEqual(result.original_format, 'JPEG')
        self.assertEqual(result.page_count, 1)
        self.assertEqual(len(result.pages), 1)
        
        page = result.pages[0]
        self.assertEqual(page.page_number, 1)
        self.assertEqual(page.format, 'JPEG')
        self.assertEqual(page.mime_type, 'image/jpeg')
        self.assertEqual((page.width, page.height), (600, 800))
        self.assertGreater(len(page.image_bytes), 0)

    def test_valid_png_processing_with_transparency(self):
        """PNG image with transparency is composited onto white background and output as JPEG."""
        from patient.services.prescription_processor import process_prescription_file

        data = self._create_image_bytes(size=(500, 500), format_name='PNG', mode='RGBA')
        result = process_prescription_file(data)

        self.assertEqual(result.file_type, 'image')
        self.assertEqual(result.original_format, 'PNG')
        self.assertEqual(result.page_count, 1)
        self.assertEqual(result.pages[0].format, 'JPEG')
        self.assertEqual((result.pages[0].width, result.pages[0].height), (500, 500))

    def test_valid_webp_processing(self):
        """Valid WEBP image is recognized and prepared cleanly."""
        from patient.services.prescription_processor import process_prescription_file

        data = self._create_image_bytes(size=(450, 600), format_name='WEBP')
        result = process_prescription_file(data)

        self.assertEqual(result.file_type, 'image')
        self.assertEqual(result.original_format, 'WEBP')
        self.assertEqual(result.page_count, 1)

    def test_exif_orientation_handling(self):
        """EXIF orientation (e.g. 6 = 90 deg CW) is transposed to correct orientation."""
        from patient.services.prescription_processor import process_prescription_file

        # Image (100, 200) with orientation 6 should transpose to (200, 100)
        data = self._create_image_bytes(size=(100, 200), format_name='JPEG', exif_orientation=6)
        result = process_prescription_file(data)

        page = result.pages[0]
        self.assertEqual((page.width, page.height), (200, 100))

    def test_image_larger_than_1600px_is_resized(self):
        """Images larger than 1600px on the longest dimension are scaled down proportionally."""
        from patient.services.prescription_processor import process_prescription_file

        data = self._create_image_bytes(size=(2400, 1200), format_name='JPEG')
        result = process_prescription_file(data)

        page = result.pages[0]
        self.assertTrue(result.metadata.get('resized'))
        self.assertEqual(page.width, 1600)
        self.assertEqual(page.height, 800)

    def test_small_image_not_unnecessarily_resized(self):
        """Images within 1600px bounds retain their exact dimensions."""
        from patient.services.prescription_processor import process_prescription_file

        data = self._create_image_bytes(size=(800, 600), format_name='JPEG')
        result = process_prescription_file(data)

        page = result.pages[0]
        self.assertFalse(result.metadata.get('resized'))
        self.assertEqual((page.width, page.height), (800, 600))

    def test_valid_single_page_pdf(self):
        """Single page PDF is rendered at 2x resolution (144 DPI) into a PNG page."""
        from patient.services.prescription_processor import process_prescription_file

        pdf_bytes = self._create_pdf_bytes(page_count=1)
        result = process_prescription_file(pdf_bytes)

        self.assertEqual(result.file_type, 'pdf')
        self.assertEqual(result.original_format, 'PDF')
        self.assertEqual(result.page_count, 1)

        page = result.pages[0]
        self.assertEqual(page.page_number, 1)
        self.assertEqual(page.format, 'PNG')
        self.assertEqual(page.mime_type, 'image/png')
        # At 300x400 pt and 144 DPI (2x), rendered pixels = 600x800
        self.assertEqual((page.width, page.height), (600, 800))
        self.assertGreater(len(page.image_bytes), 0)

    def test_valid_multi_page_pdf(self):
        """Multi-page PDF renders every page without skipping any page."""
        from patient.services.prescription_processor import process_prescription_file

        pdf_bytes = self._create_pdf_bytes(page_count=4)
        result = process_prescription_file(pdf_bytes)

        self.assertEqual(result.file_type, 'pdf')
        self.assertEqual(result.page_count, 4)
        self.assertEqual(len(result.pages), 4)

    def test_pdf_page_order_preserved(self):
        """PDF pages preserve exact sequential order from 1 to N."""
        from patient.services.prescription_processor import process_prescription_file

        pdf_bytes = self._create_pdf_bytes(page_count=3)
        result = process_prescription_file(pdf_bytes)

        page_numbers = [p.page_number for p in result.pages]
        self.assertEqual(page_numbers, [1, 2, 3])

    def test_corrupt_image_rejected(self):
        """Corrupt or incomplete image data raises PrescriptionValidationError."""
        from patient.services.prescription_processor import process_prescription_file, PrescriptionValidationError

        corrupt_data = b'\xff\xd8\xff\xe0' + b'corrupt random content' * 5
        with self.assertRaises(PrescriptionValidationError) as cm:
            process_prescription_file(corrupt_data)
        self.assertIn("Corrupted or invalid image", str(cm.exception))

    def test_corrupt_pdf_rejected(self):
        """Corrupt PDF data raises PrescriptionValidationError."""
        from patient.services.prescription_processor import process_prescription_file, PrescriptionValidationError

        corrupt_data = b'%PDF-1.4\n' + b'broken stream binary garbage' * 10
        with self.assertRaises(PrescriptionValidationError) as cm:
            process_prescription_file(corrupt_data)
        self.assertIn("Corrupted or invalid PDF", str(cm.exception))

    def test_encrypted_pdf_rejected(self):
        """Encrypted / password-protected PDF is detected and rejected with clear error."""
        from patient.services.prescription_processor import process_prescription_file, PrescriptionValidationError

        enc_bytes = self._create_pdf_bytes(page_count=2, encrypted=True)
        with self.assertRaises(PrescriptionValidationError) as cm:
            process_prescription_file(enc_bytes)
        self.assertIn("password-protected or encrypted", str(cm.exception))

    def test_pdf_exceeding_10_pages_rejected(self):
        """PDF documents exceeding 10 pages are rejected with clear limit message."""
        from patient.services.prescription_processor import process_prescription_file, PrescriptionValidationError

        large_pdf = self._create_pdf_bytes(page_count=11)
        with self.assertRaises(PrescriptionValidationError) as cm:
            process_prescription_file(large_pdf)
        self.assertIn("exceeds the allowed limit of 10 pages", str(cm.exception))

    def test_file_exceeding_10mb_rejected(self):
        """Files larger than 10 MB are rejected before expensive memory loading."""
        from patient.services.prescription_processor import process_prescription_file, PrescriptionValidationError

        # Simulating oversized file
        oversized_data = b'\xff\xd8\xff\xe0' + (b'0' * (10 * 1024 * 1024 + 100))
        with self.assertRaises(PrescriptionValidationError) as cm:
            process_prescription_file(oversized_data)
        self.assertIn("exceeds the maximum limit of 10 MB", str(cm.exception))

    def test_unsupported_file_type_rejected(self):
        """Unsupported file types (e.g. text or binary blobs) are rejected based on content signature."""
        from patient.services.prescription_processor import process_prescription_file, PrescriptionValidationError

        text_data = b"This is a prescription in plain text format."
        with self.assertRaises(PrescriptionValidationError) as cm:
            process_prescription_file(text_data)
        self.assertIn("Unsupported file format", str(cm.exception))

    def test_extension_content_mismatch_rejected(self):
        """Spoofed file extension (e.g. text file named rx.jpg) is detected and rejected by magic bytes."""
        from patient.services.prescription_processor import process_prescription_file, PrescriptionValidationError

        spoofed_upload = SimpleUploadedFile(
            name='spoofed_rx.jpg',
            content=b'Plain text content disguised with a .jpg file extension',
            content_type='image/jpeg'
        )
        with self.assertRaises(PrescriptionValidationError) as cm:
            process_prescription_file(spoofed_upload)
        self.assertIn("Unsupported file format", str(cm.exception))

    def test_empty_file_rejected(self):
        """Empty uploads are rejected immediately."""
        from patient.services.prescription_processor import process_prescription_file, PrescriptionValidationError

        with self.assertRaises(PrescriptionValidationError) as cm:
            process_prescription_file(b'')
        self.assertIn("The uploaded file is empty", str(cm.exception))


# ==============================================================================
# PHASE 3, TASK 3B: GEMINI VISION PRESCRIPTION EXTRACTION SERVICE TESTS
# ==============================================================================
import json
from unittest.mock import MagicMock
from django.test import SimpleTestCase, override_settings
from google.genai.errors import APIError, ClientError, ServerError
from google.genai import types
import httpx

from patient.services.prescription_processor import ProcessedPage, ProcessedPrescription
from patient.services.gemini_vision import (
    GeminiVisionError,
    GeminiConfigurationError,
    GeminiAuthenticationError,
    GeminiQuotaError,
    GeminiNetworkError,
    GeminiModelError,
    GeminiExtractionError,
    GeminiValidationError,
    GeminiExtractionResult,
    extract_prescription_details,
    validate_extracted_prescription_data,
    redact_sensitive_text,
)


class GeminiVisionServiceTests(SimpleTestCase):
    """
    Unit tests for Gemini Vision Prescription Extraction Service (Phase 3, Task 3B).
    All Gemini API calls are strictly mocked with zero network quota consumption.
    """

    def setUp(self):
        self.mock_client = MagicMock()
        self.sample_page_1 = ProcessedPage(
            page_number=1,
            image_bytes=b"\xff\xd8\xff\xe0\x00\x10JFIFfake_page1",
            format="JPEG",
            mime_type="image/jpeg",
            width=1200,
            height=1600,
        )
        self.sample_page_2 = ProcessedPage(
            page_number=2,
            image_bytes=b"\x89PNG\r\n\x1a\nfake_page2",
            format="PNG",
            mime_type="image/png",
            width=1200,
            height=1600,
        )
        self.valid_response_json = {
            "medicines": [
                {
                    "medicine_name": "Amoxicillin",
                    "strength": "500 mg",
                    "dosage": "1 capsule",
                    "frequency": "1-0-1",
                    "duration": "5 days",
                    "instructions": "After meals",
                    "needs_verification": False,
                    "verification_reason": "",
                },
                {
                    "medicine_name": "Paracetamol",
                    "strength": "650 mg",
                    "dosage": "1 tablet",
                    "frequency": "SOS",
                    "duration": "3 days",
                    "instructions": "When required for fever",
                    "needs_verification": True,
                    "verification_reason": "Duration text faintly scribbled",
                },
            ],
            "unclear_details": ["Illegible signature in bottom right corner"],
        }

    @override_settings(GEMINI_API_KEY="test-mock-api-key", GEMINI_MODEL="gemini-3.5-flash-lite")
    def test_successful_extraction_multiple_medicines(self):
        """Valid response with multiple medicines and unclear details parses correctly."""
        mock_response = MagicMock()
        mock_response.text = json.dumps(self.valid_response_json)
        self.mock_client.models.generate_content.return_value = mock_response

        result = extract_prescription_details([self.sample_page_1], client=self.mock_client)

        self.assertIsInstance(result, GeminiExtractionResult)
        self.assertEqual(len(result.medicines), 2)
        self.assertEqual(len(result.unclear_details), 1)
        self.assertEqual(result.medicines[0]["medicine_name"], "Amoxicillin")
        self.assertEqual(result.medicines[0]["strength"], "500 mg")
        self.assertFalse(result.medicines[0]["needs_verification"])
        self.assertEqual(result.medicines[1]["medicine_name"], "Paracetamol")
        self.assertTrue(result.medicines[1]["needs_verification"])
        self.assertEqual(result.medicines[1]["verification_reason"], "Duration text faintly scribbled")
        self.assertEqual(result.unclear_details[0], "Illegible signature in bottom right corner")
        self.assertEqual(result.metadata["model"], "gemini-3.5-flash-lite")
        self.assertEqual(result.metadata["page_count"], 1)
        self.assertIn("duration_seconds", result.metadata)

    @override_settings(GEMINI_API_KEY="test-mock-api-key")
    def test_successful_extraction_empty_medicines(self):
        """Valid document without medicines returns empty list without error."""
        empty_data = {
            "medicines": [],
            "unclear_details": ["Document appears to be a lab test result, not a prescription."],
        }
        mock_response = MagicMock()
        mock_response.text = json.dumps(empty_data)
        self.mock_client.models.generate_content.return_value = mock_response

        result = extract_prescription_details([self.sample_page_1], client=self.mock_client)

        self.assertEqual(len(result.medicines), 0)
        self.assertEqual(len(result.unclear_details), 1)

    @override_settings(GEMINI_API_KEY="test-mock-api-key")
    def test_markdown_code_fences_stripped_cleanly(self):
        """Model output enclosed in markdown ```json ... ``` blocks is safely stripped and parsed."""
        fenced_json = f"```json\n{json.dumps(self.valid_response_json)}\n```"
        mock_response = MagicMock()
        mock_response.text = fenced_json
        self.mock_client.models.generate_content.return_value = mock_response

        result = extract_prescription_details([self.sample_page_1], client=self.mock_client)

        self.assertEqual(len(result.medicines), 2)
        self.assertEqual(result.medicines[0]["medicine_name"], "Amoxicillin")

    @override_settings(GEMINI_API_KEY="")
    def test_missing_api_key_raises_configuration_error(self):
        """Missing or blank GEMINI_API_KEY raises GeminiConfigurationError."""
        with self.assertRaises(GeminiConfigurationError) as cm:
            extract_prescription_details([self.sample_page_1], client=self.mock_client)
        self.assertIn("GEMINI_API_KEY", str(cm.exception))

    @override_settings(GEMINI_API_KEY="test-mock-api-key")
    def test_empty_response_raises_extraction_error(self):
        """Empty or blank response from Gemini raises GeminiExtractionError."""
        mock_response = MagicMock()
        mock_response.text = "   "
        self.mock_client.models.generate_content.return_value = mock_response

        with self.assertRaises(GeminiExtractionError) as cm:
            extract_prescription_details([self.sample_page_1], client=self.mock_client)
        self.assertIn("empty or blank response", str(cm.exception))

    @override_settings(GEMINI_API_KEY="test-mock-api-key")
    def test_malformed_json_raises_extraction_error(self):
        """Unparseable non-JSON text from Gemini raises GeminiExtractionError."""
        mock_response = MagicMock()
        mock_response.text = "I cannot read this prescription properly because the image is blurry."
        self.mock_client.models.generate_content.return_value = mock_response

        with self.assertRaises(GeminiExtractionError) as cm:
            extract_prescription_details([self.sample_page_1], client=self.mock_client)
        self.assertIn("Failed to parse Gemini response as valid JSON", str(cm.exception))

    @override_settings(GEMINI_API_KEY="test-mock-api-key")
    def test_missing_medicine_field_raises_validation_error(self):
        """Response with missing required medicine fields raises GeminiValidationError."""
        invalid_json = {
            "medicines": [
                {
                    "medicine_name": "Azithromycin",
                }
            ],
            "unclear_details": [],
        }
        mock_response = MagicMock()
        mock_response.text = json.dumps(invalid_json)
        self.mock_client.models.generate_content.return_value = mock_response

        with self.assertRaises(GeminiValidationError) as cm:
            extract_prescription_details([self.sample_page_1], client=self.mock_client)
        self.assertIn("missing required fields", str(cm.exception))

    @override_settings(GEMINI_API_KEY="test-mock-api-key")
    def test_invalid_field_type_raises_validation_error(self):
        """Response with incorrect data types (e.g. string for needs_verification) raises GeminiValidationError."""
        invalid_json = {
            "medicines": [
                {
                    "medicine_name": "Azithromycin",
                    "strength": "500 mg",
                    "dosage": "1 tab",
                    "frequency": "OD",
                    "duration": "3 days",
                    "instructions": "Before food",
                    "needs_verification": "yes",  # Must be bool, not str
                    "verification_reason": "",
                }
            ],
            "unclear_details": [],
        }
        mock_response = MagicMock()
        mock_response.text = json.dumps(invalid_json)
        self.mock_client.models.generate_content.return_value = mock_response

        with self.assertRaises(GeminiValidationError) as cm:
            extract_prescription_details([self.sample_page_1], client=self.mock_client)
        self.assertIn("needs_verification must be a boolean", str(cm.exception))

    @override_settings(GEMINI_API_KEY="test-mock-api-key")
    def test_authentication_error_handling(self):
        """HTTP 401 / 403 errors from Gemini raise GeminiAuthenticationError without credential leakage."""
        self.mock_client.models.generate_content.side_effect = ClientError(
            403, {"error": {"message": "API key test-mock-api-key not authorized."}}
        )

        with self.assertRaises(GeminiAuthenticationError) as cm:
            extract_prescription_details([self.sample_page_1], client=self.mock_client)
        self.assertIn("authentication failed", str(cm.exception))
        self.assertNotIn("test-mock-api-key", str(cm.exception))

    @override_settings(GEMINI_API_KEY="test-mock-api-key")
    def test_quota_exceeded_error_handling(self):
        """HTTP 429 quota/rate limit error raises GeminiQuotaError."""
        self.mock_client.models.generate_content.side_effect = ClientError(
            429, {"error": {"message": "Resource has been exhausted."}}
        )

        with self.assertRaises(GeminiQuotaError) as cm:
            extract_prescription_details([self.sample_page_1], client=self.mock_client)
        self.assertIn("quota exceeded", str(cm.exception))

    @override_settings(GEMINI_API_KEY="test-mock-api-key")
    def test_model_not_found_error_handling(self):
        """HTTP 404 model not found raises GeminiModelError."""
        self.mock_client.models.generate_content.side_effect = ClientError(
            404, {"error": {"message": "Model not found."}}
        )

        with self.assertRaises(GeminiModelError) as cm:
            extract_prescription_details([self.sample_page_1], client=self.mock_client)
        self.assertIn("was not found", str(cm.exception))

    @override_settings(GEMINI_API_KEY="test-mock-api-key")
    def test_server_error_handling(self):
        """HTTP 5xx server error raises GeminiModelError."""
        self.mock_client.models.generate_content.side_effect = ServerError(
            503, {"error": {"message": "Service unavailable."}}
        )

        with self.assertRaises(GeminiModelError) as cm:
            extract_prescription_details([self.sample_page_1], client=self.mock_client)
        self.assertIn("Gemini server error", str(cm.exception))

    @override_settings(GEMINI_API_KEY="test-mock-api-key")
    def test_network_timeout_error_handling(self):
        """Network timeout raises GeminiNetworkError."""
        self.mock_client.models.generate_content.side_effect = httpx.TimeoutException("Read timed out")

        with self.assertRaises(GeminiNetworkError) as cm:
            extract_prescription_details([self.sample_page_1], client=self.mock_client)
        self.assertIn("timed out", str(cm.exception))

    @override_settings(GEMINI_API_KEY="test-mock-api-key")
    def test_network_connection_error_handling(self):
        """Network connection failure raises GeminiNetworkError."""
        self.mock_client.models.generate_content.side_effect = httpx.ConnectError("Failed to resolve host")

        with self.assertRaises(GeminiNetworkError) as cm:
            extract_prescription_details([self.sample_page_1], client=self.mock_client)
        self.assertIn("Network connectivity error", str(cm.exception))

    @override_settings(GEMINI_API_KEY="test-mock-api-key")
    def test_multi_page_input_order_preservation(self):
        """Multi-page inputs are sorted by page_number and passed to Gemini in exact sequential order."""
        mock_response = MagicMock()
        mock_response.text = json.dumps(self.valid_response_json)
        self.mock_client.models.generate_content.return_value = mock_response

        # Pass out of order: [page 2, page 1]
        extract_prescription_details([self.sample_page_2, self.sample_page_1], client=self.mock_client)

        call_args = self.mock_client.models.generate_content.call_args
        contents = call_args.kwargs["contents"]

        self.assertEqual(len(contents), 3)
        self.assertIsInstance(contents[0], types.Part)
        self.assertIsInstance(contents[1], types.Part)
        self.assertEqual(contents[0].inline_data.mime_type, "image/jpeg")
        self.assertEqual(contents[1].inline_data.mime_type, "image/png")
        self.assertIn("Please read the attached prescription", contents[2])

    @override_settings(GEMINI_API_KEY="test-mock-api-key", GEMINI_MODEL="gemini-3.5-flash-lite")
    def test_configured_model_name_passed_without_fallback(self):
        """The exact configured GEMINI_MODEL is passed to the SDK call without modification or fallback."""
        mock_response = MagicMock()
        mock_response.text = json.dumps(self.valid_response_json)
        self.mock_client.models.generate_content.return_value = mock_response

        extract_prescription_details([self.sample_page_1], client=self.mock_client)

        call_args = self.mock_client.models.generate_content.call_args
        self.assertEqual(call_args.kwargs["model"], "gemini-3.5-flash-lite")

    @override_settings(GEMINI_API_KEY="super-secret-production-key-12345")
    def test_credential_redaction(self):
        """Sensitive API keys and patterns are cleanly redacted by redact_sensitive_text."""
        raw_error = "Failed to connect with key super-secret-production-key-12345 at https://api.google.com?key=AIzaSyD1234567890abcdef1234567890abcdef"
        sanitized = redact_sensitive_text(raw_error)

        self.assertNotIn("super-secret-production-key-12345", sanitized)
        self.assertNotIn("AIzaSyD1234567890abcdef1234567890abcdef", sanitized)
        self.assertIn("[REDACTED_API_KEY]", sanitized)

    @override_settings(GEMINI_API_KEY="test-mock-api-key")
    def test_supports_processed_prescription_instance(self):
        """extract_prescription_details seamlessly handles a ProcessedPrescription object."""
        processed_rx = ProcessedPrescription(
            file_type="pdf",
            original_format="PDF",
            page_count=2,
            pages=[self.sample_page_1, self.sample_page_2],
            metadata={"render_dpi": 144},
        )
        mock_response = MagicMock()
        mock_response.text = json.dumps(self.valid_response_json)
        self.mock_client.models.generate_content.return_value = mock_response

        result = extract_prescription_details(processed_rx, client=self.mock_client)

        self.assertEqual(result.metadata["page_count"], 2)
        self.assertEqual(len(result.medicines), 2)


class PrescriptionMedicineSchemaEnhancementTests(TestCase):
    """
    Tests for Phase 4, Task 4C-2: PrescriptionMedicine Schema Enhancement.
    Verifies:
    1. New fields exist and accept valid values.
    2. New manual entries default to source='MANUAL'.
    3. Manual entries have extracted_index=NULL.
    4. Multiple manual entries for one prescription are allowed.
    5. Two different extracted indices for one prescription are allowed.
    6. Duplicate non-null extracted indices for the same prescription are blocked.
    7. The same extracted index can exist for two different prescriptions.
    8. Existing manual records remain readable and unchanged.
    """
    def setUp(self):
        self.patient_user = User.objects.create_user(
            username='schema_test_patient',
            email='schematest@example.com',
            password='TestPassword123!',
            role='PATIENT'
        )
        self.patient_profile = PatientProfile.objects.create(
            user=self.patient_user,
            blood_group='B+'
        )
        self.rx1 = Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image='patient/prescriptions/rx1.jpg',
            status='COMPLETED',
            extracted_data={'medicines': [{'medicine_name': 'Amoxicillin'}], 'unclear_details': []}
        )
        self.rx2 = Prescription.objects.create(
            patient=self.patient_profile,
            prescription_image='patient/prescriptions/rx2.jpg',
            status='COMPLETED',
            extracted_data={'medicines': [{'medicine_name': 'Paracetamol'}], 'unclear_details': []}
        )

    def test_new_fields_exist_and_accept_valid_values(self):
        """New fields frequency, instructions, source, extracted_index, and created_at accept valid values."""
        med = PrescriptionMedicine.objects.create(
            prescription=self.rx1,
            medicine_name='Metformin',
            strength='500 mg',
            dosage='1 tablet',
            duration='30 days',
            quantity='30 tabs',
            frequency='1-0-1',
            instructions='After food with a glass of water',
            source='AI_CONFIRMED',
            extracted_index=0
        )
        med.refresh_from_db()
        self.assertEqual(med.medicine_name, 'Metformin')
        self.assertEqual(med.frequency, '1-0-1')
        self.assertEqual(med.instructions, 'After food with a glass of water')
        self.assertEqual(med.source, 'AI_CONFIRMED')
        self.assertEqual(med.extracted_index, 0)
        self.assertIsNotNone(med.created_at)

    def test_manual_entry_defaults_to_manual_source(self):
        """New manual entries default to source='MANUAL' without explicit specification."""
        med = PrescriptionMedicine.objects.create(
            prescription=self.rx1,
            medicine_name='Aspirin',
            strength='75 mg'
        )
        med.refresh_from_db()
        self.assertEqual(med.source, 'MANUAL')
        self.assertIsNone(med.extracted_index)

    def test_manual_entries_allow_null_extracted_index(self):
        """Manual entries keep extracted_index as None/NULL and can be queried properly."""
        med = PrescriptionMedicine.objects.create(
            prescription=self.rx1,
            medicine_name='Cetirizine',
            source='MANUAL',
            extracted_index=None
        )
        med.refresh_from_db()
        self.assertIsNone(med.extracted_index)
        self.assertTrue(
            PrescriptionMedicine.objects.filter(prescription=self.rx1, extracted_index__isnull=True).exists()
        )

    def test_multiple_manual_entries_allowed_per_prescription(self):
        """Multiple manual entries for the same prescription with NULL extracted_index are allowed."""
        med1 = PrescriptionMedicine.objects.create(
            prescription=self.rx1,
            medicine_name='Paracetamol',
            source='MANUAL',
            extracted_index=None
        )
        med2 = PrescriptionMedicine.objects.create(
            prescription=self.rx1,
            medicine_name='Ibuprofen',
            source='MANUAL',
            extracted_index=None
        )
        med3 = PrescriptionMedicine.objects.create(
            prescription=self.rx1,
            medicine_name='Pantoprazole',
            source='MANUAL',
            extracted_index=None
        )
        self.assertEqual(self.rx1.medicines.filter(source='MANUAL').count(), 3)

    def test_two_different_extracted_indices_allowed_on_same_prescription(self):
        """Two different extracted indices (e.g. 0 and 1) are successfully saved for the same prescription."""
        med0 = PrescriptionMedicine.objects.create(
            prescription=self.rx1,
            medicine_name='Amoxicillin',
            source='AI_CONFIRMED',
            extracted_index=0
        )
        med1 = PrescriptionMedicine.objects.create(
            prescription=self.rx1,
            medicine_name='Clavulanate',
            source='AI_CONFIRMED',
            extracted_index=1
        )
        self.assertEqual(self.rx1.medicines.filter(source='AI_CONFIRMED').count(), 2)

    def test_duplicate_non_null_extracted_index_blocked_on_same_prescription(self):
        """Attempting to save duplicate non-null extracted_index on the same prescription raises IntegrityError."""
        PrescriptionMedicine.objects.create(
            prescription=self.rx1,
            medicine_name='Amoxicillin',
            source='AI_CONFIRMED',
            extracted_index=0
        )
        with self.assertRaises(IntegrityError):
            PrescriptionMedicine.objects.create(
                prescription=self.rx1,
                medicine_name='Amoxicillin Duplicate Attempt',
                source='AI_CONFIRMED',
                extracted_index=0
            )

    def test_same_extracted_index_allowed_across_different_prescriptions(self):
        """The same extracted index (e.g. 0) can exist on two distinct prescriptions without conflict."""
        med_rx1 = PrescriptionMedicine.objects.create(
            prescription=self.rx1,
            medicine_name='Drug For Rx 1',
            source='AI_CONFIRMED',
            extracted_index=0
        )
        med_rx2 = PrescriptionMedicine.objects.create(
            prescription=self.rx2,
            medicine_name='Drug For Rx 2',
            source='AI_CONFIRMED',
            extracted_index=0
        )
        self.assertEqual(med_rx1.extracted_index, 0)
        self.assertEqual(med_rx2.extracted_index, 0)
        self.assertNotEqual(med_rx1.prescription_id, med_rx2.prescription_id)

    def test_legacy_manual_records_remain_readable(self):
        """Standard manual entries without optional fields remain readable and representable."""
        med = PrescriptionMedicine.objects.create(
            prescription=self.rx1,
            medicine_name='Legacy Drug',
            strength='10mg',
            dosage='1 daily'
        )
        self.assertEqual(str(med), 'Legacy Drug')
        self.assertEqual(med.source, 'MANUAL')
        self.assertIsNone(med.frequency)
        self.assertIsNone(med.instructions)
        self.assertIsNone(med.extracted_index)


class PrescriptionMedicineConfirmationWorkflowTests(TestCase):
    """
    Comprehensive integration and workflow tests for Phase 4, Task 4C:
    Patient review, editing, confirmation, categorized display, and duplicate prevention.
    """
    def setUp(self):
        self.client = Client()
        self.patient_user_1 = User.objects.create_user(
            username='rx_confirm_patient_1',
            email='patient1@medaccess.test',
            password='TestPassword123!',
            role='PATIENT'
        )
        self.profile_1 = PatientProfile.objects.create(
            user=self.patient_user_1,
            blood_group='O+'
        )

        self.patient_user_2 = User.objects.create_user(
            username='rx_confirm_patient_2',
            email='patient2@medaccess.test',
            password='TestPassword123!',
            role='PATIENT'
        )
        self.profile_2 = PatientProfile.objects.create(
            user=self.patient_user_2,
            blood_group='A+'
        )

        self.sample_extracted_data = {
            'medicines': [
                {
                    'medicine_name': 'Amoxicillin',
                    'strength': '500 mg',
                    'dosage': '1 capsule',
                    'frequency': '1-0-1',
                    'duration': '7 days',
                    'instructions': 'After meals',
                    'needs_verification': False,
                    'verification_reason': '',
                },
                {
                    'medicine_name': 'Cough Syrup',
                    'strength': '100 ml',
                    'dosage': '5 ml',
                    'frequency': 'TDS',
                    'duration': '5 days',
                    'instructions': 'Before bed',
                    'needs_verification': True,
                    'verification_reason': 'Dosage writing is faint',
                },
                {
                    'medicine_name': 'Amoxicillin',
                    'strength': '250 mg',
                    'dosage': '1 tablet',
                    'frequency': 'Once daily',
                    'duration': '3 days',
                    'instructions': 'With water',
                    'needs_verification': False,
                    'verification_reason': '',
                }
            ],
            'unclear_details': ['Doctor stamp in lower margin is partly cut off']
        }

        self.prescription_1 = Prescription.objects.create(
            patient=self.profile_1,
            prescription_image='patient/prescriptions/confirm_test.jpg',
            status='COMPLETED',
            extracted_data=self.sample_extracted_data
        )

    def test_confirm_extracted_medicine_success(self):
        """Patient successfully confirms an extracted medicine without edits."""
        self.client.login(username='rx_confirm_patient_1', password='TestPassword123!')
        url = reverse('patient:confirm_extracted_medicine', kwargs={'pk': self.prescription_1.pk})

        post_data = {
            'extracted_index': 0,
            'medicine_name': 'Amoxicillin',
            'strength': '500 mg',
            'dosage': '1 capsule',
            'frequency': '1-0-1',
            'duration': '7 days',
            'instructions': 'After meals',
        }
        response = self.client.post(url, post_data)
        self.assertEqual(response.status_code, 302)

        # Verify DB record
        med = PrescriptionMedicine.objects.filter(prescription=self.prescription_1, extracted_index=0).first()
        self.assertIsNotNone(med)
        self.assertEqual(med.medicine_name, 'Amoxicillin')
        self.assertEqual(med.strength, '500 mg')
        self.assertEqual(med.frequency, '1-0-1')
        self.assertEqual(med.source, 'AI_CONFIRMED')
        self.assertEqual(med.extracted_index, 0)
        self.assertIsNotNone(med.created_at)

        # Verify original JSON was completely preserved
        self.prescription_1.refresh_from_db()
        self.assertEqual(len(self.prescription_1.extracted_data['medicines']), 3)
        self.assertEqual(self.prescription_1.extracted_data['medicines'][0]['medicine_name'], 'Amoxicillin')

    def test_confirm_extracted_medicine_with_edited_fields(self):
        """Patient edits medicine name, strength, and schedule before confirming."""
        self.client.login(username='rx_confirm_patient_1', password='TestPassword123!')
        url = reverse('patient:confirm_extracted_medicine', kwargs={'pk': self.prescription_1.pk})

        post_data = {
            'extracted_index': 0,
            'medicine_name': 'Amoxicillin Clavulanate (Augmentin)',
            'strength': '625 mg',
            'dosage': '1 tablet',
            'frequency': 'Twice daily',
            'duration': '10 days',
            'instructions': 'Take after food with full glass of water',
        }
        response = self.client.post(url, post_data)
        self.assertEqual(response.status_code, 302)

        med = PrescriptionMedicine.objects.filter(prescription=self.prescription_1, extracted_index=0).first()
        self.assertIsNotNone(med)
        self.assertEqual(med.medicine_name, 'Amoxicillin Clavulanate (Augmentin)')
        self.assertEqual(med.strength, '625 mg')
        self.assertEqual(med.frequency, 'Twice daily')

        # Verify raw JSON was NOT overwritten by edits
        self.prescription_1.refresh_from_db()
        self.assertEqual(self.prescription_1.extracted_data['medicines'][0]['medicine_name'], 'Amoxicillin')

    def test_confirm_uncertain_medicine_with_acknowledgement(self):
        """Uncertain medicine (needs_verification=True) is confirmed when explicit acknowledgment is provided."""
        self.client.login(username='rx_confirm_patient_1', password='TestPassword123!')
        url = reverse('patient:confirm_extracted_medicine', kwargs={'pk': self.prescription_1.pk})

        post_data = {
            'extracted_index': 1,  # Item 1 has needs_verification=True
            'medicine_name': 'Cough Syrup',
            'strength': '100 ml',
            'dosage': '10 ml',
            'frequency': 'TDS',
            'duration': '5 days',
            'instructions': 'Before bed',
            'uncertainty_acknowledged': '1',
        }
        response = self.client.post(url, post_data)
        self.assertEqual(response.status_code, 302)

        med = PrescriptionMedicine.objects.filter(prescription=self.prescription_1, extracted_index=1).first()
        self.assertIsNotNone(med)
        self.assertEqual(med.medicine_name, 'Cough Syrup')
        self.assertEqual(med.dosage, '10 ml')

    def test_confirm_uncertain_medicine_without_acknowledgement_fails(self):
        """Uncertain medicine without explicit acknowledgment is rejected."""
        self.client.login(username='rx_confirm_patient_1', password='TestPassword123!')
        url = reverse('patient:confirm_extracted_medicine', kwargs={'pk': self.prescription_1.pk})

        post_data = {
            'extracted_index': 1,
            'medicine_name': 'Cough Syrup',
            'strength': '100 ml',
            # Omitting uncertainty_acknowledged
        }
        response = self.client.post(url, post_data)
        self.assertEqual(response.status_code, 302)

        # No record created
        self.assertFalse(PrescriptionMedicine.objects.filter(prescription=self.prescription_1, extracted_index=1).exists())

    def test_confirm_extracted_medicine_invalid_index(self):
        """Out of bounds or malformed extracted_index is rejected."""
        self.client.login(username='rx_confirm_patient_1', password='TestPassword123!')
        url = reverse('patient:confirm_extracted_medicine', kwargs={'pk': self.prescription_1.pk})

        # Non-existent index 99
        response = self.client.post(url, {'extracted_index': 99, 'medicine_name': 'Fake Med'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PrescriptionMedicine.objects.filter(prescription=self.prescription_1, extracted_index=99).exists())

        # Negative index
        response_neg = self.client.post(url, {'extracted_index': -1, 'medicine_name': 'Fake Med'})
        self.assertEqual(response_neg.status_code, 302)

    def test_confirm_extracted_medicine_cross_patient_denied(self):
        """Patient 2 cannot confirm a medicine on Patient 1's prescription."""
        self.client.login(username='rx_confirm_patient_2', password='TestPassword123!')
        url = reverse('patient:confirm_extracted_medicine', kwargs={'pk': self.prescription_1.pk})

        response = self.client.post(url, {'extracted_index': 0, 'medicine_name': 'Amoxicillin'})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.prescription_1.medicines.count(), 0)

    def test_confirm_extracted_medicine_duplicate_prevented(self):
        """Attempting to confirm the same extracted index twice is blocked."""
        self.client.login(username='rx_confirm_patient_1', password='TestPassword123!')
        url = reverse('patient:confirm_extracted_medicine', kwargs={'pk': self.prescription_1.pk})

        post_data = {'extracted_index': 0, 'medicine_name': 'Amoxicillin'}
        # First confirmation
        self.client.post(url, post_data)
        self.assertEqual(self.prescription_1.medicines.filter(extracted_index=0).count(), 1)

        # Second confirmation of same index
        self.client.post(url, post_data)
        self.assertEqual(self.prescription_1.medicines.filter(extracted_index=0).count(), 1)

    def test_allow_identical_medicine_name_at_different_indices(self):
        """Prescription with Amoxicillin at index 0 and index 2 can confirm both independently."""
        self.client.login(username='rx_confirm_patient_1', password='TestPassword123!')
        url = reverse('patient:confirm_extracted_medicine', kwargs={'pk': self.prescription_1.pk})

        self.client.post(url, {'extracted_index': 0, 'medicine_name': 'Amoxicillin', 'strength': '500 mg'})
        self.client.post(url, {'extracted_index': 2, 'medicine_name': 'Amoxicillin', 'strength': '250 mg'})

        self.assertEqual(self.prescription_1.medicines.filter(medicine_name='Amoxicillin').count(), 2)

    def test_confirm_extracted_medicine_ajax_response(self):
        """AJAX request returns structured JSON with success message and updated summaries."""
        self.client.login(username='rx_confirm_patient_1', password='TestPassword123!')
        url = reverse('patient:confirm_extracted_medicine', kwargs={'pk': self.prescription_1.pk})

        post_data = {
            'extracted_index': 0,
            'medicine_name': 'Amoxicillin',
            'strength': '500 mg',
            'frequency': '1-0-1',
        }
        response = self.client.post(url, post_data, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)

        data = response.json()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['medicine']['medicine_name'], 'Amoxicillin')
        self.assertEqual(data['summary']['confirmed_ai_count'], 1)
        self.assertEqual(data['summary']['total_ai_count'], 3)
        self.assertEqual(data['summary']['confirmation_summary'], '1 of 3 medicines confirmed')

    def test_manual_medicine_creation_and_defaults(self):
        """Manual medicine creation continues to work and sets source='MANUAL', extracted_index=None."""
        self.client.login(username='rx_confirm_patient_1', password='TestPassword123!')
        manual_url = reverse('patient:confirm_prescription_medicine', kwargs={'pk': self.prescription_1.pk})

        post_data = {
            'medicine_name': 'Paracetamol Extra',
            'strength': '650 mg',
            'dosage': '1 tablet',
            'frequency': 'SOS',
            'duration': '3 days',
            'quantity': '10 tablets',
        }
        response = self.client.post(manual_url, post_data)
        self.assertEqual(response.status_code, 302)

        manual_med = self.prescription_1.medicines.filter(medicine_name='Paracetamol Extra').first()
        self.assertIsNotNone(manual_med)
        self.assertEqual(manual_med.source, 'MANUAL')
        self.assertIsNone(manual_med.extracted_index)

    def test_detail_view_categorizes_medicines_properly(self):
        """Prescription detail view displays unconfirmed candidates, confirmed medicines, and manual entries."""
        # Confirm index 0
        PrescriptionMedicine.objects.create(
            prescription=self.prescription_1,
            medicine_name='Amoxicillin',
            source='AI_CONFIRMED',
            extracted_index=0
        )
        # Add manual medicine
        PrescriptionMedicine.objects.create(
            prescription=self.prescription_1,
            medicine_name='Vitamin C',
            source='MANUAL',
            extracted_index=None
        )

        self.client.login(username='rx_confirm_patient_1', password='TestPassword123!')
        detail_url = reverse('patient:prescription_detail', kwargs={'pk': self.prescription_1.pk})
        response = self.client.get(detail_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'AI Extracted — Not Confirmed')
        self.assertContains(response, 'Patient Confirmed')
        self.assertContains(response, 'Manual Entry')
        self.assertContains(response, '1 of 3 medicines confirmed')
        self.assertContains(response, 'Cough Syrup')
        self.assertContains(response, 'Vitamin C')

    def test_cards_and_modal_display_confirmation_summary(self):
        """Prescriptions list cards display confirmation progress summary badges."""
        PrescriptionMedicine.objects.create(
            prescription=self.prescription_1,
            medicine_name='Amoxicillin',
            source='AI_CONFIRMED',
            extracted_index=0
        )
        self.client.login(username='rx_confirm_patient_1', password='TestPassword123!')
        list_url = reverse('patient:prescriptions_list')
        response = self.client.get(list_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '1 of 3 medicines confirmed')

    def test_delete_confirmed_medicine_allows_reconfirmation(self):
        """Deleting a confirmed medicine removes it and allows it to be confirmed again."""
        self.client.login(username='rx_confirm_patient_1', password='TestPassword123!')
        med = PrescriptionMedicine.objects.create(
            prescription=self.prescription_1,
            medicine_name='Amoxicillin',
            source='AI_CONFIRMED',
            extracted_index=0
        )
        delete_url = reverse('patient:delete_prescription_medicine', kwargs={'pk': self.prescription_1.pk, 'med_id': med.id})
        self.client.post(delete_url)

        self.assertFalse(PrescriptionMedicine.objects.filter(pk=med.id).exists())

        # Re-confirm index 0
        confirm_url = reverse('patient:confirm_extracted_medicine', kwargs={'pk': self.prescription_1.pk})
        res = self.client.post(confirm_url, {'extracted_index': 0, 'medicine_name': 'Amoxicillin'})
        self.assertEqual(res.status_code, 302)
        self.assertTrue(PrescriptionMedicine.objects.filter(prescription=self.prescription_1, extracted_index=0).exists())

    def test_unauthenticated_user_denied_confirmation(self):
        """Unauthenticated user is redirected to login when trying to confirm a medicine."""
        confirm_url = reverse('patient:confirm_extracted_medicine', kwargs={'pk': self.prescription_1.pk})
        response = self.client.post(confirm_url, {'extracted_index': 0, 'medicine_name': 'Amoxicillin'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response.url)


class PatientFeedbackTests(TestCase):
    """
    Test suite for Patient Portal Feedback & Ratings functionality.
    Covers access control, approved pharmacy search, rating/comment validation,
    create vs update behaviour, cross-patient isolation, and CSRF protection.
    """
    def setUp(self):
        self.client = Client()
        self.feedback_url = reverse('patient:feedback')

        # Patients
        self.patient_user_1 = User.objects.create_user(
            username='feedback_patient_1',
            email='patient1@example.com',
            password='TestPassword123!',
            role='PATIENT',
            first_name='Ananya',
            last_name='Sharma'
        )
        self.patient_profile_1 = PatientProfile.objects.create(
            user=self.patient_user_1,
            blood_group='B+'
        )

        self.patient_user_2 = User.objects.create_user(
            username='feedback_patient_2',
            email='patient2@example.com',
            password='TestPassword123!',
            role='PATIENT',
            first_name='Vikram',
            last_name='Singh'
        )
        self.patient_profile_2 = PatientProfile.objects.create(
            user=self.patient_user_2,
            blood_group='O+'
        )

        # Approved Pharmacies
        self.pharmacy_user_approved_1 = User.objects.create_user(
            username='apollo_pharma_user',
            email='apollo@example.com',
            password='TestPassword123!',
            role='PHARMACY',
            is_active=True,
            is_approved=True
        )
        self.pharmacy_approved_1 = PharmacyProfile.objects.create(
            user=self.pharmacy_user_approved_1,
            pharmacy_name='Apollo Pharmacy',
            owner_name='Apollo Health',
            pharmacist_name='Rajesh Kumar',
            pharmacy_license_number='DL-APOLLO-001',
            gst_number='27AABCA1234B1Z1',
            phone_number='9876543210',
            email='contact@apollopharmacy.com',
            address='123 Marine Drive',
            city='Mumbai',
            state='Maharashtra',
            pincode='400020',
            approval_status='APPROVED'
        )

        self.pharmacy_user_approved_2 = User.objects.create_user(
            username='wellness_pharma_user',
            email='wellness@example.com',
            password='TestPassword123!',
            role='PHARMACY',
            is_active=True,
            is_approved=True
        )
        self.pharmacy_approved_2 = PharmacyProfile.objects.create(
            user=self.pharmacy_user_approved_2,
            pharmacy_name='Wellness Chemist',
            owner_name='Wellness Corp',
            pharmacist_name='Sneha Patel',
            pharmacy_license_number='DL-WELLNESS-002',
            gst_number='27AABCW5678B1Z2',
            phone_number='9811223344',
            email='info@wellnesschemist.com',
            address='45 FC Road, Deccan',
            city='Pune',
            state='Maharashtra',
            pincode='411004',
            approval_status='APPROVED'
        )

        # Pending Pharmacy
        self.pharmacy_user_pending = User.objects.create_user(
            username='pending_pharma_user',
            email='pending@example.com',
            password='TestPassword123!',
            role='PHARMACY',
            is_active=True,
            is_approved=False
        )
        self.pharmacy_pending = PharmacyProfile.objects.create(
            user=self.pharmacy_user_pending,
            pharmacy_name='Pending Meds',
            owner_name='Pending Owner',
            pharmacist_name='Pending Pharmacist',
            pharmacy_license_number='DL-PENDING-003',
            gst_number='27AABCP9999B1Z3',
            phone_number='9877777777',
            email='pending@example.com',
            address='12 Pending Street',
            city='Mumbai',
            state='Maharashtra',
            pincode='400001',
            approval_status='PENDING'
        )

        # Rejected Pharmacy
        self.pharmacy_user_rejected = User.objects.create_user(
            username='rejected_pharma_user',
            email='rejected@example.com',
            password='TestPassword123!',
            role='PHARMACY',
            is_active=True,
            is_approved=False
        )
        self.pharmacy_rejected = PharmacyProfile.objects.create(
            user=self.pharmacy_user_rejected,
            pharmacy_name='Rejected Pharma',
            owner_name='Rejected Owner',
            pharmacist_name='Rejected Pharmacist',
            pharmacy_license_number='DL-REJECTED-004',
            gst_number='27AABCR8888B1Z4',
            phone_number='9866666666',
            email='rejected@example.com',
            address='99 Rejected Road',
            city='Pune',
            state='Maharashtra',
            pincode='411001',
            approval_status='REJECTED'
        )

    # 1. Patient can access feedback page (200 OK)
    def test_patient_can_access_feedback_page(self):
        """Logged-in patient can access the feedback and ratings page."""
        self.client.login(username='feedback_patient_1', password='TestPassword123!')
        response = self.client.get(self.feedback_url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'patient/feedback.html')
        self.assertContains(response, 'Feedback & Ratings')
        self.assertIn('pharmacies', response.context)
        self.assertIn('submitted_feedbacks', response.context)

    # 2. Non-patient users cannot access it (302/403)
    def test_non_patient_users_denied_access(self):
        """Unauthenticated, pharmacy, and admin users are denied access to patient feedback page."""
        # Unauthenticated -> redirect to login
        response = self.client.get(self.feedback_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('accounts:login'), response.url)

        # Pharmacy user -> redirected
        self.client.login(username='apollo_pharma_user', password='TestPassword123!')
        pharma_response = self.client.get(self.feedback_url)
        self.assertEqual(pharma_response.status_code, 302)

        # Admin user -> redirected
        admin_user = User.objects.create_superuser(
            username='feedback_admin',
            email='admin@example.com',
            password='AdminPassword123!'
        )
        self.client.login(username='feedback_admin', password='AdminPassword123!')
        admin_response = self.client.get(self.feedback_url)
        self.assertEqual(admin_response.status_code, 302)

    # 3. Only approved pharmacies are displayed / searchable
    def test_only_approved_pharmacies_displayed(self):
        """Only pharmacies with approval_status='APPROVED' and active/approved users appear."""
        self.client.login(username='feedback_patient_1', password='TestPassword123!')
        response = self.client.get(self.feedback_url)
        self.assertEqual(response.status_code, 200)

        pharmacies = list(response.context['pharmacies'])
        self.assertIn(self.pharmacy_approved_1, pharmacies)
        self.assertIn(self.pharmacy_approved_2, pharmacies)
        self.assertNotIn(self.pharmacy_pending, pharmacies)
        self.assertNotIn(self.pharmacy_rejected, pharmacies)

        self.assertContains(response, 'Apollo Pharmacy')
        self.assertContains(response, 'Wellness Chemist')
        self.assertNotContains(response, 'Pending Meds')
        self.assertNotContains(response, 'Rejected Pharma')

    # 4. Pharmacy search works (by name, city, address)
    def test_pharmacy_search_by_name_city_address(self):
        """Pharmacy search filters approved pharmacies by name, city, and address."""
        self.client.login(username='feedback_patient_1', password='TestPassword123!')

        # Search by Name
        resp_name = self.client.get(self.feedback_url, {'q': 'Apollo'})
        name_pharmacies = list(resp_name.context['pharmacies'])
        self.assertIn(self.pharmacy_approved_1, name_pharmacies)
        self.assertNotIn(self.pharmacy_approved_2, name_pharmacies)

        # Search by City
        resp_city = self.client.get(self.feedback_url, {'q': 'Pune'})
        city_pharmacies = list(resp_city.context['pharmacies'])
        self.assertNotIn(self.pharmacy_approved_1, city_pharmacies)
        self.assertIn(self.pharmacy_approved_2, city_pharmacies)

        # Search by Address
        resp_addr = self.client.get(self.feedback_url, {'q': 'Marine Drive'})
        addr_pharmacies = list(resp_addr.context['pharmacies'])
        self.assertIn(self.pharmacy_approved_1, addr_pharmacies)
        self.assertNotIn(self.pharmacy_approved_2, addr_pharmacies)

        # Non-matching query
        resp_none = self.client.get(self.feedback_url, {'q': 'NonExistentCityOrStore'})
        self.assertEqual(len(resp_none.context['pharmacies']), 0)
        self.assertContains(resp_none, 'No approved pharmacies found')

    # 5. Patient can submit valid rating and comment
    def test_patient_can_submit_valid_rating_and_comment(self):
        """Patient can submit a 1-5 rating with non-empty comment for an approved pharmacy."""
        self.client.login(username='feedback_patient_1', password='TestPassword123!')
        payload = {
            'pharmacy_id': self.pharmacy_approved_1.id,
            'rating': 5,
            'comment': 'Exceptional service, medicines were neatly packed and dispensed quickly.'
        }
        response = self.client.post(self.feedback_url, payload)
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, self.feedback_url)

        feedback = Feedback.objects.filter(
            patient=self.patient_profile_1,
            pharmacy=self.pharmacy_approved_1
        ).first()
        self.assertIsNotNone(feedback)
        self.assertEqual(feedback.rating, 5)
        self.assertEqual(feedback.comment, payload['comment'])

    # 6. Invalid ratings (<1 or >5 or non-integer) are rejected
    def test_invalid_ratings_rejected(self):
        """Ratings less than 1, greater than 5, non-integers, or missing are rejected."""
        self.client.login(username='feedback_patient_1', password='TestPassword123!')

        # Rating < 1
        res_zero = self.client.post(self.feedback_url, {
            'pharmacy_id': self.pharmacy_approved_1.id,
            'rating': 0,
            'comment': 'Too low rating'
        })
        self.assertEqual(res_zero.status_code, 400)
        self.assertFalse(Feedback.objects.filter(patient=self.patient_profile_1).exists())

        # Rating > 5
        res_six = self.client.post(self.feedback_url, {
            'pharmacy_id': self.pharmacy_approved_1.id,
            'rating': 6,
            'comment': 'Too high rating'
        })
        self.assertEqual(res_six.status_code, 400)
        self.assertFalse(Feedback.objects.filter(patient=self.patient_profile_1).exists())

        # Non-integer rating
        res_str = self.client.post(self.feedback_url, {
            'pharmacy_id': self.pharmacy_approved_1.id,
            'rating': 'excellent',
            'comment': 'Not an integer'
        })
        self.assertEqual(res_str.status_code, 400)
        self.assertFalse(Feedback.objects.filter(patient=self.patient_profile_1).exists())

        # Missing rating
        res_missing = self.client.post(self.feedback_url, {
            'pharmacy_id': self.pharmacy_approved_1.id,
            'comment': 'Missing rating value'
        })
        self.assertEqual(res_missing.status_code, 400)
        self.assertFalse(Feedback.objects.filter(patient=self.patient_profile_1).exists())

    # 7. Empty / whitespace-only comments are rejected
    def test_empty_and_whitespace_comments_rejected(self):
        """Comments that are empty or contain only whitespace are rejected."""
        self.client.login(username='feedback_patient_1', password='TestPassword123!')

        # Empty comment
        res_empty = self.client.post(self.feedback_url, {
            'pharmacy_id': self.pharmacy_approved_1.id,
            'rating': 4,
            'comment': ''
        })
        self.assertEqual(res_empty.status_code, 400)
        self.assertFalse(Feedback.objects.filter(patient=self.patient_profile_1).exists())

        # Whitespace-only comment
        res_ws = self.client.post(self.feedback_url, {
            'pharmacy_id': self.pharmacy_approved_1.id,
            'rating': 4,
            'comment': '     \n\t   '
        })
        self.assertEqual(res_ws.status_code, 400)
        self.assertFalse(Feedback.objects.filter(patient=self.patient_profile_1).exists())

    # 8. Existing feedback is updated instead of duplicated
    def test_existing_feedback_updated_instead_of_duplicated(self):
        """Submitting feedback for an already-reviewed pharmacy updates the existing record."""
        self.client.login(username='feedback_patient_1', password='TestPassword123!')

        # Create initial review
        initial_feedback = Feedback.objects.create(
            patient=self.patient_profile_1,
            pharmacy=self.pharmacy_approved_1,
            rating=3,
            comment='Initial average experience.'
        )

        # Update review
        update_payload = {
            'pharmacy_id': self.pharmacy_approved_1.id,
            'rating': 4,
            'comment': 'Service improved significantly on my second visit!'
        }
        response = self.client.post(self.feedback_url, update_payload)
        self.assertEqual(response.status_code, 302)

        # Verify only 1 record exists and fields were updated
        self.assertEqual(
            Feedback.objects.filter(patient=self.patient_profile_1, pharmacy=self.pharmacy_approved_1).count(),
            1
        )
        initial_feedback.refresh_from_db()
        self.assertEqual(initial_feedback.rating, 4)
        self.assertEqual(initial_feedback.comment, update_payload['comment'])

    # 9. Patient cannot edit or overwrite another patient's feedback
    def test_patient_cannot_view_or_overwrite_another_patient_feedback(self):
        """Patients have strict tenant isolation and cannot overwrite or view another patient's reviews."""
        # Patient 2 submits feedback for Apollo Pharmacy
        p2_feedback = Feedback.objects.create(
            patient=self.patient_profile_2,
            pharmacy=self.pharmacy_approved_1,
            rating=2,
            comment='Patient 2 had long waiting time.'
        )

        # Patient 1 logs in and submits their own feedback for Apollo Pharmacy
        self.client.login(username='feedback_patient_1', password='TestPassword123!')
        p1_payload = {
            'pharmacy_id': self.pharmacy_approved_1.id,
            'rating': 5,
            'comment': 'Patient 1 had a great experience!'
        }
        self.client.post(self.feedback_url, p1_payload)

        # Patient 2's review is completely untouched
        p2_feedback.refresh_from_db()
        self.assertEqual(p2_feedback.rating, 2)
        self.assertEqual(p2_feedback.comment, 'Patient 2 had long waiting time.')

        # Both feedbacks exist independently
        self.assertEqual(Feedback.objects.filter(pharmacy=self.pharmacy_approved_1).count(), 2)

        # Patient 1 GET feedback page -> submitted_feedbacks only contains Patient 1's feedback
        get_res = self.client.get(self.feedback_url)
        submitted = list(get_res.context['submitted_feedbacks'])
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0].patient, self.patient_profile_1)
        self.assertEqual(submitted[0].comment, 'Patient 1 had a great experience!')
        self.assertNotContains(get_res, 'Patient 2 had long waiting time.')

    # 10. Pending / rejected pharmacies cannot receive feedback
    def test_pending_or_rejected_pharmacies_cannot_receive_feedback(self):
        """Feedback submissions for pending or rejected pharmacies are rejected."""
        self.client.login(username='feedback_patient_1', password='TestPassword123!')

        # Pending pharmacy submission attempt
        res_pending = self.client.post(self.feedback_url, {
            'pharmacy_id': self.pharmacy_pending.id,
            'rating': 4,
            'comment': 'Attempt to review pending pharmacy.'
        })
        self.assertEqual(res_pending.status_code, 400)
        self.assertFalse(Feedback.objects.filter(pharmacy=self.pharmacy_pending).exists())

        # Rejected pharmacy submission attempt
        res_rejected = self.client.post(self.feedback_url, {
            'pharmacy_id': self.pharmacy_rejected.id,
            'rating': 4,
            'comment': 'Attempt to review rejected pharmacy.'
        })
        self.assertEqual(res_rejected.status_code, 400)
        self.assertFalse(Feedback.objects.filter(pharmacy=self.pharmacy_rejected).exists())

    # 11. CSRF protection and HTTP POST validation enforced
    def test_csrf_and_post_validation_enforced(self):
        """CSRF protection is enforced on POST, and missing/invalid pharmacy IDs are rejected."""
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.login(username='feedback_patient_1', password='TestPassword123!')

        # POST without CSRF token must fail with 403 Forbidden
        response = csrf_client.post(self.feedback_url, {
            'pharmacy_id': self.pharmacy_approved_1.id,
            'rating': 5,
            'comment': 'No CSRF token included.'
        })
        self.assertEqual(response.status_code, 403)

        # Normal client POST with missing pharmacy_id
        self.client.login(username='feedback_patient_1', password='TestPassword123!')
        res_no_pharmacy = self.client.post(self.feedback_url, {
            'rating': 5,
            'comment': 'Missing pharmacy ID.'
        })
        self.assertEqual(res_no_pharmacy.status_code, 400)

        # Normal client POST with non-existent pharmacy_id
        res_invalid_id = self.client.post(self.feedback_url, {
            'pharmacy_id': 999999,
            'rating': 5,
            'comment': 'Non-existent pharmacy ID.'
        })
        self.assertEqual(res_invalid_id.status_code, 400)


class MedicineUsesSideEffectsCheckerTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.patient_user = User.objects.create_user(
            username='checker_patient_tester',
            password='TestPassword123!',
            role='PATIENT'
        )
        self.patient_profile = PatientProfile.objects.create(
            user=self.patient_user
        )
        self.pharmacy_user = User.objects.create_user(
            username='checker_pharmacy_tester',
            password='TestPassword123!',
            role='PHARMACY'
        )
        self.checker_url = reverse('patient:medicine_checker')
        self.checker_api_url = reverse('patient:medicine_checker_api')
        self.checker_autocomplete_url = reverse('patient:medicine_checker_autocomplete_api')

    def test_checker_page_requires_patient_authentication(self):
        """Unauthenticated requests must redirect to login."""
        res = self.client.get(self.checker_url)
        self.assertEqual(res.status_code, 302)

    def test_checker_page_patient_access(self):
        """Authenticated patient can access the medicine checker page."""
        self.client.force_login(self.patient_user)
        res = self.client.get(self.checker_url)
        self.assertEqual(res.status_code, 200)
        self.assertTemplateUsed(res, 'patient/medicine_checker.html')
        self.assertContains(res, 'Medicine Uses & Side Effects')

    def test_checker_api_high_confidence_augmentin(self):
        """Searching 'Augmentin 625' yields HIGH confidence match with uses & side effects."""
        self.client.force_login(self.patient_user)
        res = self.client.get(self.checker_api_url, {'q': 'Augmentin 625'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['match_status'], 'HIGH')
        self.assertGreaterEqual(data['similarity_score'], 0.75)
        self.assertIsNotNone(data['best_match'])
        self.assertEqual(data['best_match']['medicine_name'], 'Augmentin 625 Duo Tablet')
        self.assertIn('Amoxycillin', data['best_match']['composition'])
        self.assertTrue(len(data['best_match']['uses_list']) > 0)
        self.assertTrue(len(data['best_match']['side_effects_merged']) > 0)

    def test_checker_api_high_confidence_azithral(self):
        """Searching 'Azithral 500 tab' yields HIGH confidence match with Azithral 500 Tablet."""
        self.client.force_login(self.patient_user)
        res = self.client.get(self.checker_api_url, {'q': 'Azithral 500 tab'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['match_status'], 'HIGH')
        self.assertEqual(data['best_match']['medicine_name'], 'Azithral 500 Tablet')

    def test_checker_api_medium_confidence_confirmation_prompt(self):
        """Searching 'Dolo 650' or 'Augmetin' yields MEDIUM confidence requiring confirmation."""
        self.client.force_login(self.patient_user)
        res = self.client.get(self.checker_api_url, {'q': 'Augmetin'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['match_status'], 'MEDIUM')
        # Crucial safety rule: best_match must be None until patient confirms
        self.assertIsNone(data['best_match'])
        self.assertIsNotNone(data['suggested_confirmation'])
        self.assertGreaterEqual(data['similarity_score'], 0.50)
        self.assertLess(data['similarity_score'], 0.75)

    def test_checker_api_confirm_medicine_returns_details(self):
        """Confirming suggested medicine returns complete verified details."""
        self.client.force_login(self.patient_user)
        res = self.client.get(self.checker_api_url, {'confirmed_name': 'Augmentin 375 Tablet'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['match_status'], 'HIGH')
        self.assertIsNotNone(data['best_match'])
        self.assertEqual(data['best_match']['medicine_name'], 'Augmentin 375 Tablet')

    def test_checker_api_no_match_unknown_medicine(self):
        """Unknown synthetic query yields NO_MATCH and best_match is None."""
        self.client.force_login(self.patient_user)
        res = self.client.get(self.checker_api_url, {'q': 'XyzNonExistentFakeMed999'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['match_status'], 'NO_MATCH')
        self.assertIsNone(data['best_match'])
        self.assertLess(data['similarity_score'], 0.50)

    def test_checker_api_empty_input(self):
        """Empty input or input < 2 characters returns error/empty status."""
        self.client.force_login(self.patient_user)
        res = self.client.get(self.checker_api_url, {'q': ' '}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertFalse(data['success'])
        self.assertEqual(data['match_status'], 'EMPTY_OR_TOO_SHORT')

    def test_checker_api_non_patient_forbidden(self):
        """Pharmacy role cannot access patient medicine checker."""
        self.client.force_login(self.pharmacy_user)
        res = self.client.get(self.checker_api_url, {'q': 'Dolo'})
        self.assertIn(res.status_code, [302, 403])

    def test_navigation_link_in_base_patient_template(self):
        """Base patient template includes navigation link to medicine checker."""
        self.client.force_login(self.patient_user)
        res = self.client.get(reverse('patient:dashboard'))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'medicine-checker')

    def test_autocomplete_api_augmentin(self):
        """Searching 'Augmentin' and 'Augmentin 6' returns relevant Augmentin suggestions."""
        self.client.force_login(self.patient_user)
        res = self.client.get(self.checker_autocomplete_url, {'q': 'Augmentin'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['status'], 'success')
        self.assertGreater(data['count'], 0)
        names = [s['medicine_name'] for s in data['suggestions']]
        self.assertIn('Augmentin 625 Duo Tablet', names)

        # Multi-word/prefix 'Augmentin 6'
        res2 = self.client.get(self.checker_autocomplete_url, {'q': 'Augmentin 6'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res2.status_code, 200)
        data2 = res2.json()
        self.assertEqual(data2['status'], 'success')
        names2 = [s['medicine_name'] for s in data2['suggestions']]
        self.assertIn('Augmentin 625 Duo Tablet', names2)

    def test_autocomplete_api_azithral_and_dolo(self):
        """Searching 'Azithral' and 'Dolo' returns matching formulations."""
        self.client.force_login(self.patient_user)
        res = self.client.get(self.checker_autocomplete_url, {'q': 'Azithral'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        names = [s['medicine_name'] for s in data['suggestions']]
        self.assertIn('Azithral 500 Tablet', names)

        res_dolo = self.client.get(self.checker_autocomplete_url, {'q': 'Dolo'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_dolo.status_code, 200)
        data_dolo = res_dolo.json()
        names_dolo = [s['medicine_name'] for s in data_dolo['suggestions']]
        self.assertIn('Dolo Drops', names_dolo)

    def test_autocomplete_api_max_limit_7(self):
        """Autocomplete limits suggestions to maximum 7."""
        self.client.force_login(self.patient_user)
        res = self.client.get(self.checker_autocomplete_url, {'q': 'Table'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertLessEqual(len(data['suggestions']), 7)
        self.assertLessEqual(data['count'], 7)

    def test_autocomplete_api_empty_and_short(self):
        """Queries with length < 2 return 0 suggestions safely."""
        self.client.force_login(self.patient_user)
        res = self.client.get(self.checker_autocomplete_url, {'q': 'A'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['count'], 0)
        self.assertEqual(len(data['suggestions']), 0)

        res_empty = self.client.get(self.checker_autocomplete_url, {'q': ''}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_empty.status_code, 200)
        data_empty = res_empty.json()
        self.assertEqual(data_empty['count'], 0)

    def test_autocomplete_api_unknown_medicine(self):
        """Unknown synthetic query returns 0 suggestions."""
        self.client.force_login(self.patient_user)
        res = self.client.get(self.checker_autocomplete_url, {'q': 'XyzNonExistentFake99'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['count'], 0)
        self.assertEqual(len(data['suggestions']), 0)

    def test_autocomplete_api_no_uses_or_side_effects_leaked(self):
        """Autocomplete suggestions must never contain uses or side effects."""
        self.client.force_login(self.patient_user)
        res = self.client.get(self.checker_autocomplete_url, {'q': 'Augmentin'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        for item in data['suggestions']:
            self.assertIn('medicine_name', item)
            self.assertNotIn('uses', item)
            self.assertNotIn('uses_list', item)
            self.assertNotIn('uses_cleaned', item)
            self.assertNotIn('side_effects', item)
            self.assertNotIn('side_effects_merged', item)

    def test_autocomplete_api_authentication_required(self):
        """Unauthenticated requests must redirect to login."""
        res = self.client.get(self.checker_autocomplete_url, {'q': 'Augmentin'})
        self.assertEqual(res.status_code, 302)

    def test_autocomplete_dropdown_elements_in_template(self):
        """Page template includes the autocomplete dropdown and mini spinner markup."""
        self.client.force_login(self.patient_user)
        res = self.client.get(self.checker_url)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'checker-autocomplete-dropdown')
        self.assertContains(res, 'checker-autocomplete-spinner')








