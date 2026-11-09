from django import forms
from django.contrib.auth.forms import PasswordResetForm, SetPasswordForm
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction

from accounts.models import User
from accounts.decorators import get_predefined_admin_emails
from patient.models import PatientProfile
from pharmacy.models import PharmacyProfile


class LoginForm(forms.Form):
    username = forms.CharField(
        max_length=150,
        required=True,
        widget=forms.TextInput(attrs={
            'placeholder': 'Enter your username',
            'class': 'form-input',
            'autocomplete': 'username',
        })
    )
    password = forms.CharField(
        required=True,
        widget=forms.PasswordInput(attrs={
            'placeholder': 'Enter your password',
            'class': 'form-input',
            'autocomplete': 'current-password',
        })
    )


class PatientRegistrationForm(forms.Form):
    first_name = forms.CharField(
        max_length=150,
        required=True,
        widget=forms.TextInput(attrs={'placeholder': 'First Name', 'class': 'form-input'})
    )
    last_name = forms.CharField(
        max_length=150,
        required=True,
        widget=forms.TextInput(attrs={'placeholder': 'Last Name', 'class': 'form-input'})
    )
    username = forms.CharField(
        max_length=150,
        required=True,
        widget=forms.TextInput(attrs={'placeholder': 'Choose a Username', 'class': 'form-input'})
    )
    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={'placeholder': 'Email Address', 'class': 'form-input'})
    )
    phone_number = forms.CharField(
        max_length=15,
        required=False,
        widget=forms.TextInput(attrs={'placeholder': 'Phone Number (e.g. +91 9876543210)', 'class': 'form-input'})
    )
    password = forms.CharField(
        required=True,
        widget=forms.PasswordInput(attrs={'placeholder': 'Create Password', 'class': 'form-input'})
    )
    confirm_password = forms.CharField(
        required=True,
        widget=forms.PasswordInput(attrs={'placeholder': 'Confirm Password', 'class': 'form-input'})
    )

    def clean_username(self):
        username = self.cleaned_data.get('username', '').strip()
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError("A user with this username already exists.")
        return username

    def clean_email(self):
        email = self.cleaned_data.get('email', '').strip().lower()
        if email in get_predefined_admin_emails():
            raise ValidationError("This email address is reserved for administrative accounts.")
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
            else:
                validate_password(password)
        return cleaned_data

    def save(self):
        cleaned_data = self.cleaned_data
        with transaction.atomic():
            user = User(
                username=cleaned_data['username'],
                email=cleaned_data['email'],
                first_name=cleaned_data['first_name'],
                last_name=cleaned_data['last_name'],
                phone_number=cleaned_data.get('phone_number', ''),
                role='PATIENT',
                is_approved=True,
                email_verified=False,
            )
            user.set_password(cleaned_data['password'])
            user.save()

            PatientProfile.objects.create(user=user)
            return user


class PharmacyRegistrationForm(forms.Form):
    # User Account Credentials
    username = forms.CharField(
        max_length=150,
        required=True,
        widget=forms.TextInput(attrs={'placeholder': 'Pharmacy Portal Username', 'class': 'form-input'})
    )
    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={'placeholder': 'Official Email Address', 'class': 'form-input'})
    )
    password = forms.CharField(
        required=True,
        widget=forms.PasswordInput(attrs={'placeholder': 'Create Password', 'class': 'form-input'})
    )
    confirm_password = forms.CharField(
        required=True,
        widget=forms.PasswordInput(attrs={'placeholder': 'Confirm Password', 'class': 'form-input'})
    )

    # Pharmacy Business & License Details
    pharmacy_name = forms.CharField(
        max_length=200,
        required=True,
        label="Pharmacy Name",
        widget=forms.TextInput(attrs={'placeholder': 'Full Registered Pharmacy Name', 'class': 'form-input'})
    )
    owner_name = forms.CharField(
        max_length=150,
        required=True,
        label="Owner Name",
        widget=forms.TextInput(attrs={'placeholder': 'Business Owner Name', 'class': 'form-input'})
    )
    pharmacist_name = forms.CharField(
        max_length=150,
        required=True,
        label="Pharmacist Name",
        widget=forms.TextInput(attrs={'placeholder': 'Qualified Pharmacist Name', 'class': 'form-input'})
    )
    pharmacy_license_number = forms.CharField(
        max_length=100,
        required=True,
        label="Drug License Number",
        widget=forms.TextInput(attrs={'placeholder': 'State Drug License Number (e.g. DL-12345)', 'class': 'form-input'})
    )
    gst_number = forms.CharField(
        max_length=20,
        required=True,
        label="GST Number",
        widget=forms.TextInput(attrs={'placeholder': 'GST Identification Number', 'class': 'form-input'})
    )
    phone_number = forms.CharField(
        max_length=15,
        required=True,
        label="Store Contact Number",
        widget=forms.TextInput(attrs={'placeholder': 'Store Contact Number (e.g. 022-12345678)', 'class': 'form-input'})
    )
    address = forms.CharField(
        required=True,
        widget=forms.Textarea(attrs={'placeholder': 'Complete Street Address of Pharmacy', 'rows': 3, 'class': 'form-input'})
    )
    city = forms.CharField(
        max_length=100,
        required=True,
        widget=forms.TextInput(attrs={'placeholder': 'City', 'class': 'form-input'})
    )
    state = forms.CharField(
        max_length=100,
        required=True,
        widget=forms.TextInput(attrs={'placeholder': 'State', 'class': 'form-input'})
    )
    pincode = forms.CharField(
        max_length=10,
        required=True,
        widget=forms.TextInput(attrs={'placeholder': 'PIN Code', 'class': 'form-input'})
    )

    def clean_username(self):
        username = self.cleaned_data.get('username', '').strip()
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError("A user with this username already exists.")
        return username

    def clean_email(self):
        email = self.cleaned_data.get('email', '').strip().lower()
        if email in get_predefined_admin_emails():
            raise ValidationError("This email address is reserved for administrative accounts.")
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
            else:
                validate_password(password)
        return cleaned_data

    def save(self):
        cleaned_data = self.cleaned_data
        with transaction.atomic():
            user = User(
                username=cleaned_data['username'],
                email=cleaned_data['email'],
                first_name=cleaned_data['owner_name'],
                phone_number=cleaned_data['phone_number'],
                role='PHARMACY',
                is_approved=False,
                email_verified=False,
            )
            user.set_password(cleaned_data['password'])
            user.save()

            PharmacyProfile.objects.create(
                user=user,
                pharmacy_name=cleaned_data['pharmacy_name'],
                owner_name=cleaned_data['owner_name'],
                pharmacist_name=cleaned_data['pharmacist_name'],
                pharmacy_license_number=cleaned_data['pharmacy_license_number'],
                gst_number=cleaned_data['gst_number'],
                phone_number=cleaned_data['phone_number'],
                email=cleaned_data['email'],
                address=cleaned_data['address'],
                city=cleaned_data['city'],
                state=cleaned_data['state'],
                pincode=cleaned_data['pincode'],
                approval_status='PENDING',
            )
            return user


class MedAccessPasswordResetForm(PasswordResetForm):
    """
    Password reset request form with MedAccess styling and customized error messages.
    Enforces server-side email format validation.
    """
    email = forms.EmailField(
        label='Registered Email Address',
        max_length=254,
        required=True,
        widget=forms.EmailInput(attrs={
            'placeholder': 'Enter your registered email address',
            'class': 'form-input',
            'autocomplete': 'email',
            'autofocus': True,
        }),
        error_messages={
            'required': 'Please enter your registered email address.',
            'invalid': 'Please enter a valid email address.',
        }
    )


class MedAccessSetPasswordForm(SetPasswordForm):
    """
    Password reset confirmation form applying MedAccess styling and Django password validators.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['new_password1'].widget.attrs.update({
            'class': 'form-input',
            'placeholder': 'Enter new password',
            'autocomplete': 'new-password',
        })
        self.fields['new_password2'].widget.attrs.update({
            'class': 'form-input',
            'placeholder': 'Confirm new password',
            'autocomplete': 'new-password',
        })


class ResendVerificationForm(forms.Form):
    """
    Form to request a new email verification link.
    """
    email = forms.EmailField(
        label='Registered Email Address',
        max_length=254,
        required=True,
        widget=forms.EmailInput(attrs={
            'placeholder': 'Enter your registered email address',
            'class': 'form-input',
            'autocomplete': 'email',
            'autofocus': True,
        }),
        error_messages={
            'required': 'Please enter your registered email address.',
            'invalid': 'Please enter a valid email address.',
        }
    )


