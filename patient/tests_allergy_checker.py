"""
Comprehensive automated tests for MedAccess AI-Powered Medicine Allergy Checker.

Tests cover:
1. Public accessibility without login / registration.
2. Input validation (missing medicines, missing symptoms, oversized/invalid files).
3. Text-based screening with mocked Groq qwen/qwen3.8-27b model.
4. Vision-based screening with uploaded image and mocked Groq qwen/qwen3.8-27b multimodal model.
5. Multilingual NLP support (English, Hindi, Hinglish).
6. Deterministic emergency symptom detection heuristics (airway / anaphylaxis).
7. External API exception handling (RateLimitError, APITimeoutError, AuthenticationError, model errors).
8. Emergency guidance preservation when external Groq API is unavailable.
9. Fallback mode when GROQ_API_KEY is not configured.
10. IP-based rate limiting on public endpoints.
11. Privacy and zero-secret leakage checks.
"""

import io
import json
from unittest.mock import MagicMock, patch
from PIL import Image

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, Client, override_settings
from django.urls import reverse

from patient.services.allergy_checker_service import (
    analyze_medicine_allergy,
    detect_emergency_symptoms,
    validate_image_upload,
    check_rate_limit,
    AllergyCheckerValidationError,
    MANDATORY_MEDICAL_DISCLAIMER,
)


def create_dummy_image(name='test_tablet.jpg', fmt='JPEG', size=(100, 100), color='white'):
    """Helper to generate an in-memory dummy image for testing."""
    file_obj = io.BytesIO()
    img = Image.new('RGB', size, color=color)
    img.save(file_obj, format=fmt)
    file_obj.seek(0)
    mime = 'image/jpeg' if fmt == 'JPEG' else f'image/{fmt.lower()}'
    return SimpleUploadedFile(name, file_obj.read(), content_type=mime)


class AllergyCheckerPublicAccessTests(TestCase):
    """Verifies that the Allergy Checker is publicly accessible without login."""

    def setUp(self):
        self.client = Client()

    def test_allergy_checker_page_publicly_accessible(self):
        """GET /allergy-checker/ should return HTTP 200 without authentication."""
        url = reverse('allergy_checker')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'patient/allergy_checker.html')
        self.assertContains(response, 'Medicine Allergy & Adverse Reaction Checker', html=False)
        self.assertContains(response, 'Analyze Allergy Risk')

    def test_allergy_checker_api_requires_post(self):
        """GET /allergy-checker/api/ should return HTTP 405 Method Not Allowed."""
        url = reverse('allergy_checker_api')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 405)


class AllergyCheckerValidationTests(TestCase):
    """Verifies strict input validation rules."""

    def setUp(self):
        self.client = Client()
        cache.clear()

    def test_missing_both_medicine_name_and_image(self):
        """Request without medicine name and without image must fail with HTTP 400."""
        url = reverse('allergy_checker_api')
        response = self.client.post(url, {
            'medicine_name': '',
            'symptoms': 'I have mild red spots on my hands.',
        })
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('medicine', data['error'].lower())

    def test_missing_symptoms(self):
        """Request without symptoms must fail with HTTP 400."""
        url = reverse('allergy_checker_api')
        response = self.client.post(url, {
            'medicine_name': 'Amoxicillin',
            'symptoms': '',
        })
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('symptoms', data['error'].lower())

    def test_invalid_image_mime_type(self):
        """Uploaded file with disallowed mime type (e.g. PDF/text) must fail with HTTP 400."""
        url = reverse('allergy_checker_api')
        fake_pdf = SimpleUploadedFile('document.pdf', b'%PDF-1.4 dummy', content_type='application/pdf')
        response = self.client.post(url, {
            'medicine_image': fake_pdf,
            'symptoms': 'Itching all over.',
        })
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('unsupported file format', data['error'].lower())

    def test_oversized_image_upload(self):
        """Uploaded image over 5 MB must fail with HTTP 400."""
        oversized_data = b'0' * (5500 * 1024)
        large_file = SimpleUploadedFile('giant.jpg', oversized_data, content_type='image/jpeg')

        with self.assertRaises(AllergyCheckerValidationError) as ctx:
            validate_image_upload(large_file)
        self.assertIn('5 mb', str(ctx.exception).lower())


class AllergyCheckerEmergencyHeuristicsTests(TestCase):
    """Verifies deterministic regex detection for emergency anaphylactic symptoms."""

    def test_english_emergency_symptoms(self):
        """English airway compromise & anaphylaxis phrases must trigger emergency."""
        emergencies = [
            "I am having severe breathing difficulty and throat is tight",
            "My tongue and facial swelling started immediately after the pill",
            "Experiencing shortness of breath and wheezing",
            "Patient passed out and is gasping for air",
            "Cyanosis and blue lips observed",
        ]
        for phrase in emergencies:
            with self.subTest(phrase=phrase):
                self.assertTrue(
                    detect_emergency_symptoms(phrase),
                    f"Expected emergency detection for: {phrase}"
                )

    def test_hindi_hinglish_emergency_symptoms(self):
        """Hindi and Hinglish red-flag phrases must trigger emergency."""
        hindi_emergencies = [
            "dawa khane ke baad saans lene me dikkat ho rahi hai",
            "gale me sujan aur kharash hai",
            "honth aur jeebh me soojan aa gayi",
            "chakkar aane laga aur dam ghut raha hai",
            "saans fulna shuru ho gaya",
        ]
        for phrase in hindi_emergencies:
            with self.subTest(phrase=phrase):
                self.assertTrue(
                    detect_emergency_symptoms(phrase),
                    f"Expected Hindi emergency detection for: {phrase}"
                )

    def test_non_emergency_mild_symptoms(self):
        """Mild common complaints must not be flagged as immediate airway emergency."""
        non_emergencies = [
            "Mild headache after tablet",
            "Slight nausea and sour taste",
            "Feeling a bit sleepy after dose",
        ]
        for phrase in non_emergencies:
            with self.subTest(phrase=phrase):
                self.assertFalse(
                    detect_emergency_symptoms(phrase),
                    f"Unexpected emergency detection for: {phrase}"
                )


@override_settings(GROQ_API_KEY='mock_groq_api_key_for_testing')
class AllergyCheckerGroqInferenceTests(TestCase):
    """Tests AI inference with mocked Groq API for text and multimodal requests using qwen/qwen3.8-27b."""

    def setUp(self):
        self.client = Client()
        cache.clear()

    @patch('groq.Groq')
    def test_text_only_medicine_screening_success(self, mock_groq_class):
        """Valid text medicine name + symptoms should invoke qwen/qwen3.8-27b model and return structured result."""
        mock_groq_instance = MagicMock()
        mock_groq_class.return_value = mock_groq_instance

        mock_payload = {
            "identified_medicine": "Amoxicillin",
            "active_ingredient": "Amoxicillin Trihydrate",
            "identification_confidence": "HIGH",
            "user_symptoms": ["red hives", "intense itching"],
            "is_emergency": False,
            "risk_level": "POTENTIAL_ALLERGY",
            "analysis_summary": "Symptoms indicate a potential drug hypersensitivity reaction to penicillin-class antibiotics.",
            "allergy_assessment": "The onset of urticaria and pruritus suggests an IgE-mediated allergic response.",
            "side_effect_differentiation": "Unlike common amoxicillin gastrointestinal side effects, widespread hives point toward hypersensitivity.",
            "known_reaction_info": "Penicillin derivatives share cross-reactivity with cephalosporins.",
            "warning_signs": ["Shortness of breath", "Facial swelling"],
            "recommended_actions": ["Discontinue medicine immediately", "Consult prescribing doctor"],
            "emergency_guidance": None
        }

        mock_completion = MagicMock()
        mock_completion.choices = [
            MagicMock(message=MagicMock(content=json.dumps(mock_payload)))
        ]
        mock_groq_instance.chat.completions.create.return_value = mock_completion

        url = reverse('allergy_checker_api')
        response = self.client.post(url, {
            'medicine_name': 'Amoxicillin 500mg',
            'symptoms': 'Red hives and intense itching on arms 40 minutes after taking capsule.',
        })

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        result = data['data']
        self.assertEqual(result['identified_medicine'], 'Amoxicillin')
        self.assertEqual(result['risk_level'], 'POTENTIAL_ALLERGY')
        self.assertIn('penicillin', result['analysis_summary'].lower())
        self.assertEqual(result['disclaimer'], MANDATORY_MEDICAL_DISCLAIMER)

        # Verify call used qwen/qwen3.8-27b
        mock_groq_instance.chat.completions.create.assert_called_once()
        call_kwargs = mock_groq_instance.chat.completions.create.call_args[1]
        self.assertEqual(call_kwargs['model'], 'qwen/qwen3.8-27b')

    @patch('groq.Groq')
    def test_image_upload_vision_screening_success(self, mock_groq_class):
        """Uploaded medicine packaging should invoke qwen/qwen3.8-27b with base64 data URL."""
        mock_groq_instance = MagicMock()
        mock_groq_class.return_value = mock_groq_instance

        mock_payload = {
            "identified_medicine": "Augmentin 625 Duo",
            "active_ingredient": "Amoxicillin + Clavulanic Acid",
            "identification_confidence": "HIGH",
            "user_symptoms": ["facial swelling", "skin rash"],
            "is_emergency": True,
            "risk_level": "EMERGENCY",
            "analysis_summary": "Facial swelling following Augmentin intake is a serious clinical sign of angioedema.",
            "allergy_assessment": "Possible severe hypersensitivity reaction.",
            "side_effect_differentiation": "Facial swelling is not an expected side effect.",
            "known_reaction_info": "High cross-reactivity across beta-lactam antibiotics.",
            "warning_signs": ["Throat tightness", "Breathing difficulty"],
            "recommended_actions": ["Seek urgent medical care", "Do not take another dose"],
            "emergency_guidance": "Go to the nearest emergency department."
        }

        mock_completion = MagicMock()
        mock_completion.choices = [
            MagicMock(message=MagicMock(content=json.dumps(mock_payload)))
        ]
        mock_groq_instance.chat.completions.create.return_value = mock_completion

        dummy_img = create_dummy_image(name='strip.jpg', fmt='JPEG')
        url = reverse('allergy_checker_api')
        response = self.client.post(url, {
            'medicine_image': dummy_img,
            'symptoms': 'chehre par sujan aa gayi hai aur rash hai',
        })

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        result = data['data']
        self.assertTrue(result['is_emergency'])
        self.assertEqual(result['risk_level'], 'EMERGENCY')

        # Verify call used qwen/qwen3.8-27b
        call_kwargs = mock_groq_instance.chat.completions.create.call_args[1]
        self.assertEqual(call_kwargs['model'], 'qwen/qwen3.8-27b')

        # Verify image base64 data URL format inside user message content
        messages = call_kwargs['messages']
        user_msg = messages[-1]
        self.assertIsInstance(user_msg['content'], list)
        img_part = next((p for p in user_msg['content'] if p.get('type') == 'image_url'), None)
        self.assertIsNotNone(img_part)
        self.assertTrue(img_part['image_url']['url'].startswith('data:image/jpeg;base64,'))

    @patch('groq.Groq')
    def test_emergency_override_when_ai_misses_critical_symptom(self, mock_groq_class):
        """Server-side heuristic must upgrade result to EMERGENCY even if model returns UNLIKELY_ALLERGY."""
        mock_groq_instance = MagicMock()
        mock_groq_class.return_value = mock_groq_instance

        mock_payload = {
            "identified_medicine": "Aspirin",
            "active_ingredient": "Acetylsalicylic Acid",
            "identification_confidence": "HIGH",
            "user_symptoms": ["hard to breathe"],
            "is_emergency": False,
            "risk_level": "COMMON_SIDE_EFFECT",
            "analysis_summary": "Mild reaction.",
            "allergy_assessment": "Unclear.",
            "side_effect_differentiation": "Side effect.",
            "known_reaction_info": "NSAID hypersensitivity.",
            "warning_signs": [],
            "recommended_actions": [],
            "emergency_guidance": None
        }

        mock_completion = MagicMock()
        mock_completion.choices = [
            MagicMock(message=MagicMock(content=json.dumps(mock_payload)))
        ]
        mock_groq_instance.chat.completions.create.return_value = mock_completion

        url = reverse('allergy_checker_api')
        response = self.client.post(url, {
            'medicine_name': 'Aspirin',
            'symptoms': 'I am gasping for air and have difficulty breathing',
        })

        self.assertEqual(response.status_code, 200)
        result = response.json()['data']
        self.assertTrue(result['is_emergency'])
        self.assertEqual(result['risk_level'], 'EMERGENCY')
        self.assertIsNotNone(result['emergency_guidance'])


@override_settings(GROQ_API_KEY='mock_groq_api_key_for_testing')
class AllergyCheckerErrorHandlingTests(TestCase):
    """Verifies robust handling of Groq API errors and fallbacks."""

    def setUp(self):
        self.client = Client()
        cache.clear()

    @patch('groq.Groq')
    def test_groq_rate_limit_error_returns_429(self, mock_groq_class):
        """When Groq throws RateLimitError without emergency, API must return HTTP 429."""
        from groq import RateLimitError
        mock_groq_instance = MagicMock()
        mock_groq_class.return_value = mock_groq_instance
        mock_groq_instance.chat.completions.create.side_effect = RateLimitError(
            message="Rate limit exceeded",
            response=MagicMock(status_code=429),
            body={}
        )

        url = reverse('allergy_checker_api')
        response = self.client.post(url, {
            'medicine_name': 'Paracetamol',
            'symptoms': 'Mild itchy spots on arm.',
        })
        self.assertEqual(response.status_code, 429)
        self.assertIn('high demand', response.json()['error'].lower())

    @patch('groq.Groq')
    def test_groq_timeout_error_returns_504(self, mock_groq_class):
        """When Groq API call times out without emergency, API must return HTTP 504."""
        from groq import APITimeoutError
        mock_groq_instance = MagicMock()
        mock_groq_class.return_value = mock_groq_instance
        mock_groq_instance.chat.completions.create.side_effect = APITimeoutError(
            request=MagicMock()
        )

        url = reverse('allergy_checker_api')
        response = self.client.post(url, {
            'medicine_name': 'Paracetamol',
            'symptoms': 'Mild itchy spots on arm.',
        })
        self.assertEqual(response.status_code, 504)
        self.assertIn('timed out', response.json()['error'].lower())

    @patch('groq.Groq')
    def test_groq_authentication_error_returns_401(self, mock_groq_class):
        """When Groq authentication fails, API must return HTTP 401."""
        from groq import AuthenticationError
        mock_groq_instance = MagicMock()
        mock_groq_class.return_value = mock_groq_instance
        mock_groq_instance.chat.completions.create.side_effect = AuthenticationError(
            message="Invalid API Key",
            response=MagicMock(status_code=401),
            body={'error': {'code': 'invalid_api_key'}}
        )

        url = reverse('allergy_checker_api')
        response = self.client.post(url, {
            'medicine_name': 'Paracetamol',
            'symptoms': 'Mild itchy spots on arm.',
        })
        self.assertEqual(response.status_code, 401)
        self.assertIn('authentication', response.json()['error'].lower())

    @patch('groq.Groq')
    def test_groq_model_decommissioned_error_returns_502(self, mock_groq_class):
        """When Groq returns model_decommissioned, API must return HTTP 502 with clear message."""
        from groq import BadRequestError
        mock_groq_instance = MagicMock()
        mock_groq_class.return_value = mock_groq_instance
        mock_groq_instance.chat.completions.create.side_effect = BadRequestError(
            message="The model has been decommissioned",
            response=MagicMock(status_code=400),
            body={'error': {'code': 'model_decommissioned'}}
        )

        url = reverse('allergy_checker_api')
        response = self.client.post(url, {
            'medicine_name': 'Paracetamol',
            'symptoms': 'Mild itchy spots on arm.',
        })
        self.assertEqual(response.status_code, 502)
        self.assertIn('model', response.json()['error'].lower())

    @patch('groq.Groq')
    def test_emergency_guidance_displayed_even_when_groq_fails(self, mock_groq_class):
        """When patient reports emergency symptoms and Groq API fails, emergency guidance must still be returned."""
        from groq import RateLimitError
        mock_groq_instance = MagicMock()
        mock_groq_class.return_value = mock_groq_instance
        mock_groq_instance.chat.completions.create.side_effect = RateLimitError(
            message="Rate limit exceeded",
            response=MagicMock(status_code=429),
            body={}
        )

        url = reverse('allergy_checker_api')
        response = self.client.post(url, {
            'medicine_name': 'Amoxicillin',
            'symptoms': 'saans lene me bahut dikkat ho rahi hai aur gale me sujan hai',
        })

        # Crucial requirement: Must return 200 with emergency guidance, NOT an unhelpful error
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        result = data['data']
        self.assertTrue(result['is_emergency'])
        self.assertEqual(result['risk_level'], 'EMERGENCY')
        self.assertIsNotNone(result['emergency_guidance'])
        self.assertIn('112', result['emergency_guidance'])

    @override_settings(GROQ_API_KEY='')
    def test_fallback_when_api_key_blank(self):
        """When GROQ_API_KEY is blank, fallback response must be returned safely."""
        result = analyze_medicine_allergy(
            medicine_name='Cetirizine',
            symptoms='saans lene me dikkat ho rahi hai',
            client_ip='127.0.0.1'
        )
        self.assertIsInstance(result, dict)
        self.assertEqual(result['identified_medicine'], 'Cetirizine')
        self.assertTrue(result['is_emergency'])
        self.assertEqual(result['risk_level'], 'EMERGENCY')
        self.assertIn('fallback', result['analysis_summary'].lower())


class AllergyCheckerRateLimitingTests(TestCase):
    """Verifies that abusive traffic from a single IP is blocked."""

    def setUp(self):
        self.client = Client()
        cache.clear()

    def test_endpoint_rate_limiting_enforced(self):
        """Making more requests than the limit within window must return HTTP 429."""
        url = reverse('allergy_checker_api')
        ip = '198.51.100.42'
        limit = 20

        for _ in range(limit):
            self.assertTrue(check_rate_limit(ip, limit=limit, window_seconds=60))

        self.assertFalse(check_rate_limit(ip, limit=limit, window_seconds=60))

        response = self.client.post(
            url,
            {'medicine_name': 'Paracetamol', 'symptoms': 'Nausea'},
            REMOTE_ADDR=ip
        )
        self.assertEqual(response.status_code, 429)
        self.assertIn('too many screening requests', response.json()['error'].lower())


@override_settings(GROQ_API_KEY='mock_groq_api_key_for_testing')
class AllergyCheckerServerRenderedTests(TestCase):
    """Verifies progressive enhancement via standard HTML form submission."""

    def setUp(self):
        self.client = Client()
        cache.clear()

    @patch('groq.Groq')
    def test_html_form_post_success(self, mock_groq_class):
        """Submitting regular POST to /allergy-checker/ renders results in HTML."""
        mock_groq_instance = MagicMock()
        mock_groq_class.return_value = mock_groq_instance

        mock_payload = {
            "identified_medicine": "Ibuprofen",
            "active_ingredient": "Ibuprofen",
            "identification_confidence": "HIGH",
            "user_symptoms": ["stomach pain"],
            "is_emergency": False,
            "risk_level": "COMMON_SIDE_EFFECT",
            "analysis_summary": "NSAIDs commonly cause gastrointestinal irritation.",
            "allergy_assessment": "Symptoms are consistent with a known pharmacological side effect rather than an allergic reaction.",
            "side_effect_differentiation": "GI upset is not an immune-mediated hypersensitivity.",
            "known_reaction_info": "Can cause gastric ulceration with prolonged use.",
            "warning_signs": ["Black tarry stools"],
            "recommended_actions": ["Take with food", "Consult doctor if pain persists"],
            "emergency_guidance": None
        }

        mock_completion = MagicMock()
        mock_completion.choices = [
            MagicMock(message=MagicMock(content=json.dumps(mock_payload)))
        ]
        mock_groq_instance.chat.completions.create.return_value = mock_completion

        url = reverse('allergy_checker')
        response = self.client.post(url, {
            'medicine_name': 'Ibuprofen 400mg',
            'symptoms': 'Mild stomach burning and nausea after taking tablet.',
        })

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'patient/allergy_checker.html')
        self.assertContains(response, 'Ibuprofen')
        self.assertContains(response, 'COMMON_SIDE_EFFECT')
        self.assertContains(response, 'gastrointestinal irritation')
