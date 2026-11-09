import re
from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction

from accounts.models import User
from patient.models import PatientProfile
from admin_portal.models import PlatformSetting


class AdminPatientCreateForm(forms.Form):
    """
    Administrative form for provisioning patient accounts directly from Admin Portal.
    Guarantees role='PATIENT', verifies email/username uniqueness, sets password cleanly.
    """
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Unique Username', 'required': 'required'})
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={'class': 'form-input', 'placeholder': 'patient@example.com', 'required': 'required'})
    )
    first_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'First Name', 'required': 'required'})
    )
    last_name = forms.CharField(
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Last Name'})
    )
    phone_number = forms.CharField(
        max_length=15,
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'e.g. +91 9876543210'})
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-input', 'placeholder': 'Secure Password (min 8 characters)', 'required': 'required'})
    )
    confirm_password = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-input', 'placeholder': 'Confirm Password', 'required': 'required'})
    )
    gender = forms.ChoiceField(
        choices=[('', '-- Select Gender --')] + list(PatientProfile.GENDER_CHOICES),
        required=False,
        widget=forms.Select(attrs={'class': 'form-input'})
    )
    blood_group = forms.ChoiceField(
        choices=[('', '-- Select Blood Group --')] + list(PatientProfile.BLOOD_GROUP_CHOICES),
        required=False,
        widget=forms.Select(attrs={'class': 'form-input'})
    )
    date_of_birth = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'class': 'form-input', 'type': 'date'})
    )
    address = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'class': 'form-input', 'rows': 2, 'placeholder': 'Residential Address'})
    )
    allergies = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'class': 'form-input', 'rows': 2, 'placeholder': 'Known Drug Allergies / Notes'})
    )

    def clean_username(self):
        username = self.cleaned_data.get('username', '').strip()
        if not username:
            raise ValidationError("Username is required.")
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError("A user with this username already exists.")
        return username

    def clean_email(self):
        email = self.cleaned_data.get('email', '').strip().lower()
        if not email:
            raise ValidationError("Email address is required.")
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("A user with this email address already exists.")
        return email

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get('password')
        confirm_password = cleaned_data.get('confirm_password')

        if password and confirm_password:
            if password != confirm_password:
                self.add_error('confirm_password', "Passwords do not match.")
            if len(password) < 8:
                self.add_error('password', "Password must be at least 8 characters long.")
        return cleaned_data

    @transaction.atomic
    def save(self):
        cd = self.cleaned_data
        user = User.objects.create_user(
            username=cd['username'],
            email=cd['email'],
            password=cd['password'],
            first_name=cd['first_name'],
            last_name=cd.get('last_name', ''),
            role='PATIENT',
            phone_number=cd.get('phone_number', ''),
            is_approved=True,
            email_verified=True,
        )
        PatientProfile.objects.create(
            user=user,
            gender=cd.get('gender') or None,
            blood_group=cd.get('blood_group') or None,
            date_of_birth=cd.get('date_of_birth') or None,
            address=cd.get('address', ''),
            allergies=cd.get('allergies', ''),
        )
        return user


class AdminPatientEditForm(forms.Form):
    """
    Administrative form for updating existing patient personal & profile details.
    """
    first_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'First Name', 'required': 'required'})
    )
    last_name = forms.CharField(
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Last Name'})
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={'class': 'form-input', 'placeholder': 'Email Address', 'required': 'required'})
    )
    phone_number = forms.CharField(
        max_length=15,
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Phone Number'})
    )
    is_active = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={'class': 'form-checkbox'})
    )
    gender = forms.ChoiceField(
        choices=[('', '-- Select Gender --')] + list(PatientProfile.GENDER_CHOICES),
        required=False,
        widget=forms.Select(attrs={'class': 'form-input'})
    )
    blood_group = forms.ChoiceField(
        choices=[('', '-- Select Blood Group --')] + list(PatientProfile.BLOOD_GROUP_CHOICES),
        required=False,
        widget=forms.Select(attrs={'class': 'form-input'})
    )
    date_of_birth = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'class': 'form-input', 'type': 'date'})
    )
    address = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'class': 'form-input', 'rows': 2, 'placeholder': 'Residential Address'})
    )
    allergies = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'class': 'form-input', 'rows': 2, 'placeholder': 'Known Drug Allergies'})
    )

    def __init__(self, *args, **kwargs):
        self.patient_user = kwargs.pop('patient_user')
        super().__init__(*args, **kwargs)
        if self.patient_user:
            profile = getattr(self.patient_user, 'patient_profile', None)
            self.fields['first_name'].initial = self.patient_user.first_name
            self.fields['last_name'].initial = self.patient_user.last_name
            self.fields['email'].initial = self.patient_user.email
            self.fields['phone_number'].initial = self.patient_user.phone_number
            self.fields['is_active'].initial = self.patient_user.is_active
            if profile:
                self.fields['gender'].initial = profile.gender
                self.fields['blood_group'].initial = profile.blood_group
                self.fields['date_of_birth'].initial = profile.date_of_birth
                self.fields['address'].initial = profile.address
                self.fields['allergies'].initial = profile.allergies

    def clean_email(self):
        email = self.cleaned_data.get('email', '').strip().lower()
        if not email:
            raise ValidationError("Email address is required.")
        if User.objects.filter(email__iexact=email).exclude(id=self.patient_user.id).exists():
            raise ValidationError("This email address is already in use by another account.")
        return email

    @transaction.atomic
    def save(self):
        cd = self.cleaned_data
        self.patient_user.first_name = cd['first_name']
        self.patient_user.last_name = cd.get('last_name', '')
        self.patient_user.email = cd['email']
        self.patient_user.phone_number = cd.get('phone_number', '')
        self.patient_user.is_active = cd.get('is_active', True)
        self.patient_user.save()

        profile, _ = PatientProfile.objects.get_or_create(user=self.patient_user)
        profile.gender = cd.get('gender') or None
        profile.blood_group = cd.get('blood_group') or None
        profile.date_of_birth = cd.get('date_of_birth') or None
        profile.address = cd.get('address', '')
        profile.allergies = cd.get('allergies', '')
        profile.save()

        return self.patient_user


class PharmacyRejectForm(forms.Form):
    """
    Rejection modal form. Rejection reason is completely optional.
    """
    rejection_reason = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={
            'class': 'form-input',
            'rows': 4,
            'placeholder': 'Optional: specify reasons (e.g. invalid license number, blurry document, expired GSTIN)...',
        })
    )


class PlatformSettingsForm(forms.ModelForm):
    """
    Form for editing non-sensitive platform settings.
    """
    class Meta:
        model = PlatformSetting
        fields = [
            'platform_name',
            'support_email',
            'support_phone',
            'emergency_contact',
            'operating_hours',
            'allow_patient_registration',
            'allow_pharmacy_registration',
            'maintenance_mode',
        ]
        widgets = {
            'platform_name': forms.TextInput(attrs={'class': 'form-input', 'required': 'required'}),
            'support_email': forms.EmailInput(attrs={'class': 'form-input', 'required': 'required'}),
            'support_phone': forms.TextInput(attrs={'class': 'form-input', 'required': 'required'}),
            'emergency_contact': forms.TextInput(attrs={'class': 'form-input', 'required': 'required'}),
            'operating_hours': forms.TextInput(attrs={'class': 'form-input', 'required': 'required'}),
            'allow_patient_registration': forms.CheckboxInput(attrs={'class': 'form-checkbox'}),
            'allow_pharmacy_registration': forms.CheckboxInput(attrs={'class': 'form-checkbox'}),
            'maintenance_mode': forms.CheckboxInput(attrs={'class': 'form-checkbox'}),
        }

