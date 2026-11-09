from django import forms
from accounts.models import User
from accounts.decorators import get_predefined_admin_emails
from patient.models import PatientProfile, Prescription, PrescriptionMedicine
from pharmacy.models import Feedback


class PatientUserForm(forms.ModelForm):
    """
    Form for updating User fields (name, email, phone) on the patient profile.
    """
    first_name = forms.CharField(
        max_length=150,
        required=True,
        widget=forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'First Name'})
    )
    last_name = forms.CharField(
        max_length=150,
        required=True,
        widget=forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Last Name'})
    )
    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={'class': 'form-input', 'placeholder': 'Email Address'})
    )
    phone_number = forms.CharField(
        max_length=15,
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-input', 'placeholder': '10-digit Phone Number'})
    )

    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email', 'phone_number']

    def clean_email(self):
        email = self.cleaned_data.get('email', '').strip()
        if email:
            if email.lower() in get_predefined_admin_emails():
                raise forms.ValidationError("This email address is reserved for administrative accounts.")
            if User.objects.exclude(pk=self.instance.pk).filter(email__iexact=email).exists():
                raise forms.ValidationError("This email address is already in use by another account.")
        return email


class PatientProfileForm(forms.ModelForm):
    """
    Form for updating PatientProfile clinical and personal details.
    """
    class Meta:
        model = PatientProfile
        fields = [
            'date_of_birth',
            'gender',
            'blood_group',
            'address',
            'allergies',
            'profile_photo',
        ]
        widgets = {
            'date_of_birth': forms.DateInput(attrs={'class': 'form-input', 'type': 'date'}),
            'gender': forms.Select(attrs={'class': 'form-select'}),
            'blood_group': forms.Select(attrs={'class': 'form-select'}),
            'address': forms.Textarea(attrs={'class': 'form-textarea', 'rows': 3, 'placeholder': 'Enter your residential address'}),
            'allergies': forms.Textarea(attrs={'class': 'form-textarea', 'rows': 3, 'placeholder': 'List known allergies (e.g. Penicillin, Peanuts, Pollen)'}),
            'profile_photo': forms.FileInput(attrs={'class': 'form-file-input', 'accept': 'image/*'}),
        }

    def clean_profile_photo(self):
        photo = self.cleaned_data.get('profile_photo')
        if photo and hasattr(photo, 'size'):
            # Reject files larger than 5 MB
            max_size_mb = 5
            if photo.size > max_size_mb * 1024 * 1024:
                raise forms.ValidationError(f"Profile photo must be smaller than {max_size_mb}MB.")

            # Validate allowed image file extensions
            valid_extensions = ('.jpg', '.jpeg', '.png', '.webp')
            if not photo.name.lower().endswith(valid_extensions):
                raise forms.ValidationError("Unsupported image format. Allowed formats: JPG, PNG, WebP.")
        return photo


class PrescriptionUploadForm(forms.ModelForm):
    """
    Form for uploading a doctor's prescription image or multi-page PDF document.
    """
    class Meta:
        model = Prescription
        fields = ['prescription_image']
        widgets = {
            'prescription_image': forms.FileInput(attrs={
                'class': 'form-file-input',
                'accept': '.jpg,.jpeg,.png,.webp,.pdf,image/jpeg,image/png,image/webp,application/pdf',
                'id': 'prescription-image-input',
            }),
        }

    def clean_prescription_image(self):
        file = self.cleaned_data.get('prescription_image')
        if not file:
            raise forms.ValidationError("Please select a prescription file to upload.")

        # Validate allowed file extensions
        valid_extensions = ('.jpg', '.jpeg', '.png', '.webp', '.pdf')
        if not file.name.lower().endswith(valid_extensions):
            raise forms.ValidationError("Unsupported image format. Please upload JPG, JPEG, PNG, or WEBP.")

        # Content-based binary signature and size validation via Task 3A service
        from patient.services.prescription_processor import validate_prescription_file, PrescriptionValidationError
        try:
            validate_prescription_file(file)
        except PrescriptionValidationError as err:
            raise forms.ValidationError(str(err))

        return file


class PrescriptionMedicineForm(forms.ModelForm):
    """
    Form for adding a single prescribed medicine entry to a prescription.
    """
    medicine_name = forms.CharField(
        max_length=255,
        required=True,
        widget=forms.TextInput(attrs={
            'class': 'form-input',
            'placeholder': 'Medicine name (e.g. Paracetamol, Amoxicillin)',
            'required': 'required'
        })
    )
    strength = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-input',
            'placeholder': 'Strength (e.g. 500mg, 10ml)'
        })
    )
    dosage = forms.CharField(
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-input',
            'placeholder': 'Dosage (e.g. 1 tablet, 5ml)'
        })
    )
    frequency = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-input',
            'placeholder': 'Frequency (e.g. 1-0-1, Once daily, TDS)'
        })
    )
    duration = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-input',
            'placeholder': 'Duration (e.g. 5 days, 2 weeks)'
        })
    )
    instructions = forms.CharField(
        max_length=500,
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-input',
            'placeholder': 'Instructions (e.g. After meals, Before breakfast)'
        })
    )
    quantity = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-input',
            'placeholder': 'Quantity (e.g. 10 tablets, 1 bottle)'
        })
    )

    class Meta:
        model = PrescriptionMedicine
        fields = ['medicine_name', 'strength', 'dosage', 'frequency', 'duration', 'instructions', 'quantity']


class ConfirmExtractedMedicineForm(forms.Form):
    """
    Form for validating patient review and edits of an AI-extracted medicine candidate.
    """
    extracted_index = forms.IntegerField(
        min_value=0,
        required=True,
        widget=forms.HiddenInput()
    )
    medicine_name = forms.CharField(
        max_length=255,
        required=True,
        widget=forms.TextInput(attrs={
            'class': 'form-input',
            'placeholder': 'Medicine name (required)',
            'required': 'required'
        })
    )
    strength = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-input',
            'placeholder': 'Strength (e.g. 500mg)'
        })
    )
    dosage = forms.CharField(
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-input',
            'placeholder': 'Dosage (e.g. 1 tablet)'
        })
    )
    frequency = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-input',
            'placeholder': 'Frequency (e.g. 1-0-1, TDS)'
        })
    )
    duration = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-input',
            'placeholder': 'Duration (e.g. 5 days)'
        })
    )
    instructions = forms.CharField(
        max_length=500,
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-input',
            'placeholder': 'Instructions (e.g. After meals)'
        })
    )
    uncertainty_acknowledged = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={
            'class': 'form-checkbox'
        })
    )

    def clean_medicine_name(self):
        name = self.cleaned_data.get('medicine_name', '').strip()
        if not name:
            raise forms.ValidationError("Medicine name is required.")
        return name


class PatientFeedbackForm(forms.ModelForm):
    """
    Form for patient rating and comment submission for an approved pharmacy.
    Enforces mandatory 1-5 rating and non-empty comment.
    """
    rating = forms.IntegerField(
        min_value=1,
        max_value=5,
        required=True,
        error_messages={
            'required': 'Please select a rating between 1 and 5 stars.',
            'min_value': 'Rating must be between 1 and 5.',
            'max_value': 'Rating must be between 1 and 5.',
            'invalid': 'Rating must be a valid number between 1 and 5.',
        }
    )
    comment = forms.CharField(
        widget=forms.Textarea(attrs={
            'class': 'form-textarea',
            'rows': 4,
            'placeholder': 'Share your detailed feedback about the service, medicine availability, staff responsiveness, etc...',
            'maxlength': '1000',
        }),
        required=True,
        max_length=1000,
        strip=True,
        error_messages={
            'required': 'Please enter your feedback comment.',
            'max_length': 'Feedback comment cannot exceed 1000 characters.',
        }
    )

    class Meta:
        model = Feedback
        fields = ['rating', 'comment']

    def clean_rating(self):
        rating = self.cleaned_data.get('rating')
        if rating is None or rating < 1 or rating > 5:
            raise forms.ValidationError('Rating must be between 1 and 5.')
        return rating

    def clean_comment(self):
        comment = self.cleaned_data.get('comment', '').strip()
        if not comment:
            raise forms.ValidationError('Please enter your feedback comment.')
        return comment

