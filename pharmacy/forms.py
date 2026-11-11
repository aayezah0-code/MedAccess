import re
from decimal import Decimal
from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Q

from accounts.models import User
from pharmacy.models import PharmacyProfile, Medicine, Inventory, PharmacySupportRequest


class PharmacyProfileForm(forms.ModelForm):
    """
    Form for authenticated pharmacy owners to update their profile information.
    Strictly permits updates only to non-compliance fields.
    Regulatory credentials (license number, GSTIN, approval status) are protected.
    """

    class Meta:
        model = PharmacyProfile
        fields = [
            'pharmacy_name',
            'owner_name',
            'pharmacist_name',
            'phone_number',
            'email',
            'address',
            'city',
            'state',
            'pincode',
            'latitude',
            'longitude',
        ]
        widgets = {
            'pharmacy_name': forms.TextInput(attrs={
                'class': 'form-input',
                'placeholder': 'Registered Pharmacy Name',
                'required': 'required',
            }),
            'owner_name': forms.TextInput(attrs={
                'class': 'form-input',
                'placeholder': 'Business Owner Name',
                'required': 'required',
            }),
            'pharmacist_name': forms.TextInput(attrs={
                'class': 'form-input',
                'placeholder': 'Registered Pharmacist Name',
                'required': 'required',
            }),
            'phone_number': forms.TextInput(attrs={
                'class': 'form-input',
                'placeholder': 'Store Contact Number (e.g. 9876543210)',
                'required': 'required',
            }),
            'email': forms.EmailInput(attrs={
                'class': 'form-input',
                'placeholder': 'Official Email Address',
                'required': 'required',
            }),
            'address': forms.Textarea(attrs={
                'class': 'form-input',
                'rows': 3,
                'placeholder': 'Complete Street Address of Pharmacy',
                'required': 'required',
            }),
            'city': forms.TextInput(attrs={
                'class': 'form-input',
                'placeholder': 'City',
                'required': 'required',
            }),
            'state': forms.TextInput(attrs={
                'class': 'form-input',
                'placeholder': 'State',
                'required': 'required',
            }),
            'pincode': forms.TextInput(attrs={
                'class': 'form-input',
                'placeholder': '6-digit PIN Code (e.g. 462001)',
                'required': 'required',
            }),
            'latitude': forms.NumberInput(attrs={
                'class': 'form-input',
                'step': 'any',
                'placeholder': 'e.g. 23.259933',
            }),
            'longitude': forms.NumberInput(attrs={
                'class': 'form-input',
                'step': 'any',
                'placeholder': 'e.g. 77.412615',
            }),
        }

    def clean_email(self):
        email = self.cleaned_data.get('email', '').strip().lower()
        if not email:
            raise ValidationError("Email address is required.")

        user_id = self.instance.user_id if self.instance else None
        if User.objects.filter(email__iexact=email).exclude(pk=user_id).exists():
            raise ValidationError("This email address is already registered to another user account.")
        return email

    def clean_phone_number(self):
        phone = self.cleaned_data.get('phone_number', '').strip()
        if not phone:
            raise ValidationError("Phone number is required.")

        digits = re.sub(r'[\s\-+()]', '', phone)
        if not digits.isdigit() or len(digits) < 10 or len(digits) > 15:
            raise ValidationError("Please enter a valid phone number (between 10 and 15 digits).")
        return phone

    def clean_pincode(self):
        pincode = self.cleaned_data.get('pincode', '').strip()
        if not pincode:
            raise ValidationError("PIN code is required.")

        digits = re.sub(r'\s', '', pincode)
        if not digits.isdigit() or len(digits) < 4 or len(digits) > 10:
            raise ValidationError("Please enter a valid PIN/postal code.")
        return pincode

    def clean_latitude(self):
        lat = self.cleaned_data.get('latitude')
        if lat is not None:
            if lat < Decimal('-90.0') or lat > Decimal('90.0'):
                raise ValidationError("Latitude must be between -90.0 and 90.0 degrees.")
        return lat

    def clean_longitude(self):
        lon = self.cleaned_data.get('longitude')
        if lon is not None:
            if lon < Decimal('-180.0') or lon > Decimal('180.0'):
                raise ValidationError("Longitude must be between -180.0 and 180.0 degrees.")
        return lon

    def clean(self):
        cleaned_data = super().clean()
        lat = cleaned_data.get('latitude')
        lon = cleaned_data.get('longitude')

        if (lat is not None and lon is None) or (lat is None and lon is not None):
            raise ValidationError("Both latitude and longitude must be provided together, or both left blank.")

        return cleaned_data

    def save(self, commit=True):
        profile = super().save(commit=False)

        if profile.pk:
            orig = PharmacyProfile.objects.only(
                'pharmacy_license_number', 'gst_number', 'approval_status'
            ).get(pk=profile.pk)
            profile.pharmacy_license_number = orig.pharmacy_license_number
            profile.gst_number = orig.gst_number
            profile.approval_status = orig.approval_status

        if commit:
            profile.save()
            if profile.user:
                user = profile.user
                updated = False
                if profile.email and user.email != profile.email:
                    user.email = profile.email
                    updated = True
                if profile.phone_number and user.phone_number != profile.phone_number:
                    user.phone_number = profile.phone_number
                    updated = True
                if updated:
                    user.save(update_fields=['email', 'phone_number'])

        return profile


class PharmacyResubmissionForm(forms.ModelForm):
    """
    Form for rejected pharmacies to correct their credentials and resubmit for verification.
    Permits correcting pharmacy name, owner, pharmacist, license number, GST number,
    phone, address, city, state, pincode.
    Never permits modifying approval_status, role, is_staff or is_superuser.
    """
    class Meta:
        model = PharmacyProfile
        fields = [
            'pharmacy_name',
            'owner_name',
            'pharmacist_name',
            'pharmacy_license_number',
            'gst_number',
            'phone_number',
            'address',
            'city',
            'state',
            'pincode',
        ]
        widgets = {
            'pharmacy_name': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Registered Pharmacy Name', 'required': 'required'}),
            'owner_name': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Business Owner Name', 'required': 'required'}),
            'pharmacist_name': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Qualified Pharmacist Name', 'required': 'required'}),
            'pharmacy_license_number': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'State Drug License Number (e.g. DL-12345)', 'required': 'required'}),
            'gst_number': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'GST Identification Number', 'required': 'required'}),
            'phone_number': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Store Contact Number', 'required': 'required'}),
            'address': forms.Textarea(attrs={'class': 'form-input', 'rows': 3, 'placeholder': 'Complete Street Address of Pharmacy', 'required': 'required'}),
            'city': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'City', 'required': 'required'}),
            'state': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'State', 'required': 'required'}),
            'pincode': forms.TextInput(attrs={'class': 'form-input', 'placeholder': '6-digit PIN Code', 'required': 'required'}),
        }

    def clean_pincode(self):
        pincode = self.cleaned_data.get('pincode', '').strip()
        if not re.match(r'^\d{6}$', pincode):
            raise ValidationError("PIN code must be exactly 6 digits.")
        return pincode

    def save(self, commit=True):
        profile = super().save(commit=False)
        profile.approval_status = 'PENDING'
        if commit:
            profile.save()
            if profile.user:
                profile.user.is_approved = False
                profile.user.save(update_fields=['is_approved'])
        return profile



class MedicineForm(forms.ModelForm):
    """
    Form for adding a new medicine to the master catalogue.
    Validates name, composition, formulation, positive MRP, and optional image uploads.
    Performs normalized matching to prevent duplicate catalogue entries.
    """
    mrp = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=True,
        widget=forms.NumberInput(attrs={
            'class': 'form-input',
            'step': '0.01',
            'placeholder': 'Maximum Retail Price in INR (e.g. 55.00)',
            'required': 'required',
        })
    )
    medicine_image = forms.ImageField(
        required=False,
        widget=forms.FileInput(attrs={
            'class': 'form-input',
            'accept': 'image/jpeg,image/png,image/webp',
        })
    )

    class Meta:
        model = Medicine
        fields = [
            'medicine_name',
            'composition',
            'strength',
            'manufacturer',
            'dosage_form',
            'pack_size',
            'mrp',
            'medicine_image',
        ]
        widgets = {
            'medicine_name': forms.TextInput(attrs={
                'class': 'form-input',
                'placeholder': 'Trade / Brand Name (e.g. Dolo 650)',
                'required': 'required',
            }),
            'composition': forms.Textarea(attrs={
                'class': 'form-input',
                'rows': 2,
                'placeholder': 'Active Pharmaceutical Ingredients (e.g. Paracetamol)',
            }),
            'strength': forms.TextInput(attrs={
                'class': 'form-input',
                'placeholder': 'Strength / Concentration (e.g. 650 mg, 500 mg/5ml)',
            }),
            'manufacturer': forms.TextInput(attrs={
                'class': 'form-input',
                'placeholder': 'Pharma Manufacturer (e.g. Micro Labs Ltd)',
            }),
            'dosage_form': forms.TextInput(attrs={
                'class': 'form-input',
                'placeholder': 'Dosage Form (e.g. Tablet, Capsule, Syrup, Injection)',
            }),
            'pack_size': forms.TextInput(attrs={
                'class': 'form-input',
                'placeholder': 'Packaging Size (e.g. 15 Tablets, 100 ml Bottle)',
            }),
        }

    def clean_medicine_name(self):
        name = self.cleaned_data.get('medicine_name', '').strip()
        if not name:
            raise ValidationError("Medicine name is required.")
        return name

    def clean_mrp(self):
        mrp = self.cleaned_data.get('mrp')
        if mrp is None:
            raise ValidationError("MRP is required.")
        if mrp <= Decimal('0.00'):
            raise ValidationError("MRP must be greater than zero.")
        return mrp

    def clean_medicine_image(self):
        image = self.cleaned_data.get('medicine_image')
        if image:
            max_size = 5 * 1024 * 1024  # 5 MB
            if image.size > max_size:
                raise ValidationError("Image file size cannot exceed 5 MB.")

            allowed_extensions = ['.jpg', '.jpeg', '.png', '.webp']
            name_lower = image.name.lower()
            if not any(name_lower.endswith(ext) for ext in allowed_extensions):
                raise ValidationError("Unsupported image format. Allowed formats: JPG, PNG, WEBP.")

            content_type = getattr(image, 'content_type', '')
            if content_type:
                allowed_content_types = ['image/jpeg', 'image/png', 'image/webp']
                if content_type not in allowed_content_types:
                    raise ValidationError("Invalid image type. Allowed formats: JPEG, PNG, WEBP.")
        return image

    def clean(self):
        cleaned_data = super().clean()
        name = (cleaned_data.get('medicine_name') or '').strip()
        strength = (cleaned_data.get('strength') or '').strip()
        dosage_form = (cleaned_data.get('dosage_form') or '').strip()

        if name:
            duplicate_query = Q(medicine_name__iexact=name)
            if strength:
                duplicate_query &= Q(strength__iexact=strength)
            else:
                duplicate_query &= (Q(strength__isnull=True) | Q(strength=''))

            if dosage_form:
                duplicate_query &= Q(dosage_form__iexact=dosage_form)

            existing = Medicine.objects.filter(duplicate_query).first()
            if existing:
                spec = f"{existing.strength or 'Standard strength'}"
                if existing.dosage_form:
                    spec += f", {existing.dosage_form}"
                raise ValidationError(
                    f"A medicine with identical name and formulation ('{existing.medicine_name}' - {spec}) "
                    f"already exists in the MedAccess master catalogue. Please use the existing record."
                )

        return cleaned_data


class InventoryBatchCreateForm(forms.ModelForm):
    """
    Form for adding a new inventory batch to the authenticated pharmacy's stock.
    Restricts medicine selection to existing master catalogue medicines.
    Enforces non-negative quantities and limits, and prevents duplicate batches.
    """
    class Meta:
        model = Inventory
        fields = [
            'medicine',
            'batch_number',
            'expiry_date',
            'quantity',
            'unit',
            'low_stock_limit',
        ]
        widgets = {
            'medicine': forms.Select(attrs={
                'class': 'form-input',
                'required': 'required',
            }),
            'batch_number': forms.TextInput(attrs={
                'class': 'form-input',
                'placeholder': 'Batch / Lot Number (e.g. BATCH-2026-01)',
                'required': 'required',
            }),
            'expiry_date': forms.DateInput(attrs={
                'class': 'form-input',
                'type': 'date',
                'required': 'required',
            }),
            'quantity': forms.NumberInput(attrs={
                'class': 'form-input',
                'min': '0',
                'placeholder': 'Current Quantity in Stock',
                'required': 'required',
            }),
            'unit': forms.TextInput(attrs={
                'class': 'form-input',
                'placeholder': 'e.g. Units, Tablets, Strips, Bottles',
                'required': 'required',
            }),
            'low_stock_limit': forms.NumberInput(attrs={
                'class': 'form-input',
                'min': '0',
                'placeholder': 'Threshold for Low Stock Warning',
                'required': 'required',
            }),
        }
        error_messages = {
            'quantity': {
                'min_value': 'Quantity cannot be negative.',
            },
            'low_stock_limit': {
                'min_value': 'Low stock limit cannot be negative.',
            },
        }

    def __init__(self, *args, pharmacy_profile=None, **kwargs):
        self.pharmacy_profile = pharmacy_profile
        super().__init__(*args, **kwargs)
        self.fields['medicine'].queryset = Medicine.objects.all().order_by('medicine_name')
        self.fields['medicine'].empty_label = "-- Select Medicine from Master Catalogue --"
        if not self.initial.get('unit'):
            self.initial['unit'] = 'Units'
        if not self.initial.get('low_stock_limit'):
            self.initial['low_stock_limit'] = 10

    def clean_batch_number(self):
        batch_number = self.cleaned_data.get('batch_number', '').strip()
        if not batch_number:
            raise ValidationError("Batch number is required.")
        return batch_number

    def clean_quantity(self):
        quantity = self.cleaned_data.get('quantity')
        if quantity is None:
            raise ValidationError("Quantity is required.")
        if quantity < 0:
            raise ValidationError("Quantity cannot be negative.")
        return quantity

    def clean_low_stock_limit(self):
        limit = self.cleaned_data.get('low_stock_limit')
        if limit is None:
            raise ValidationError("Low stock limit is required.")
        if limit < 0:
            raise ValidationError("Low stock limit cannot be negative.")
        return limit


class PharmacySupportRequestForm(forms.ModelForm):
    """
    Validated contact support form for authenticated, approved pharmacy users.
    Ownership (pharmacy & submitted_by) is assigned strictly in the view.
    """

    class Meta:
        model = PharmacySupportRequest
        fields = ['subject', 'category', 'message']
        widgets = {
            'subject': forms.TextInput(attrs={
                'class': 'form-input',
                'id': 'support-subject-input',
                'placeholder': 'Briefly summarize your question or issue',
                'maxlength': '200',
                'required': 'required',
            }),
            'category': forms.Select(attrs={
                'class': 'form-select',
                'id': 'support-category-select',
                'required': 'required',
            }),
            'message': forms.Textarea(attrs={
                'class': 'form-textarea',
                'id': 'support-message-textarea',
                'rows': 5,
                'maxlength': '2000',
                'placeholder': 'Describe the issue in detail (include any Request ID, medicine name, or steps that triggered the issue)...',
                'required': 'required',
            }),
        }

    def clean_subject(self):
        subject = (self.cleaned_data.get('subject') or '').strip()
        if not subject:
            raise ValidationError("Please enter a subject for your support request.")
        if len(subject) < 5:
            raise ValidationError("Subject must be at least 5 characters long.")
        return subject

    def clean_message(self):
        message = (self.cleaned_data.get('message') or '').strip()
        if not message:
            raise ValidationError("Please describe your issue or question.")
        if len(message) < 15:
            raise ValidationError("Please provide at least 15 characters in your description so our team can assist effectively.")
        if len(message) > 2000:
            raise ValidationError("Description cannot exceed 2000 characters.")
        return message

    def clean(self):
        cleaned_data = super().clean()
        medicine = cleaned_data.get('medicine')
        batch_number = (cleaned_data.get('batch_number') or '').strip()

        if self.pharmacy_profile and medicine and batch_number:
            if Inventory.objects.filter(
                pharmacy=self.pharmacy_profile,
                medicine=medicine,
                batch_number__iexact=batch_number
            ).exists():
                raise ValidationError(
                    f"A batch with number '{batch_number}' for '{medicine.medicine_name}' already exists in your inventory. "
                    "Please update the existing batch instead of creating a duplicate."
                )
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.pharmacy_profile:
            instance.pharmacy = self.pharmacy_profile
        if commit:
            instance.save()
        return instance


class SmartStockEntryForm(InventoryBatchCreateForm):
    """
    Form for Smart Stock Entry / Update Stocks.
    Reuses all inventory validation and batch uniqueness checks from InventoryBatchCreateForm,
    using a hidden input for medicine ID populated via autocomplete.
    """
    medicine = forms.ModelChoiceField(
        queryset=Medicine.objects.all(),
        widget=forms.HiddenInput(attrs={'id': 'id_medicine'}),
        error_messages={
            'required': 'Please search and select a valid medicine from the catalogue.',
            'invalid_choice': 'The selected medicine is invalid or does not exist in the catalogue.',
        }
    )


class InventoryBatchUpdateForm(forms.ModelForm):
    """
    Form for updating an existing inventory batch (stock adjustment, expiry, limits).
    Medicine and batch number remain protected to maintain batch integrity.
    """
    class Meta:
        model = Inventory
        fields = [
            'quantity',
            'expiry_date',
            'unit',
            'low_stock_limit',
        ]
        widgets = {
            'quantity': forms.NumberInput(attrs={
                'class': 'form-input',
                'min': '0',
                'required': 'required',
            }),
            'expiry_date': forms.DateInput(attrs={
                'class': 'form-input',
                'type': 'date',
                'required': 'required',
            }),
            'unit': forms.TextInput(attrs={
                'class': 'form-input',
                'placeholder': 'e.g. Units, Tablets, Strips, Bottles',
                'required': 'required',
            }),
            'low_stock_limit': forms.NumberInput(attrs={
                'class': 'form-input',
                'min': '0',
                'required': 'required',
            }),
        }
        error_messages = {
            'quantity': {
                'min_value': 'Quantity cannot be negative.',
            },
            'low_stock_limit': {
                'min_value': 'Low stock limit cannot be negative.',
            },
        }

    def clean_quantity(self):
        quantity = self.cleaned_data.get('quantity')
        if quantity is None:
            raise ValidationError("Quantity is required.")
        if quantity < 0:
            raise ValidationError("Quantity cannot be negative.")
        return quantity

    def clean_low_stock_limit(self):
        limit = self.cleaned_data.get('low_stock_limit')
        if limit is None:
            raise ValidationError("Low stock limit is required.")
        if limit < 0:
            raise ValidationError("Low stock limit cannot be negative.")
        return limit


class PharmacySupportRequestForm(forms.ModelForm):
    """
    Validated contact support form for authenticated, approved pharmacy users.
    Ownership (pharmacy & submitted_by) is assigned strictly in the view.
    """

    class Meta:
        model = PharmacySupportRequest
        fields = ['subject', 'category', 'message']
        widgets = {
            'subject': forms.TextInput(attrs={
                'class': 'form-input',
                'id': 'support-subject-input',
                'placeholder': 'Briefly summarize your question or issue',
                'maxlength': '200',
                'required': 'required',
            }),
            'category': forms.Select(attrs={
                'class': 'form-select',
                'id': 'support-category-select',
                'required': 'required',
            }),
            'message': forms.Textarea(attrs={
                'class': 'form-textarea',
                'id': 'support-message-textarea',
                'rows': 5,
                'maxlength': '2000',
                'placeholder': 'Describe the issue in detail (include any Request ID, medicine name, or steps that triggered the issue)...',
                'required': 'required',
            }),
        }

    def clean_subject(self):
        subject = (self.cleaned_data.get('subject') or '').strip()
        if not subject:
            raise ValidationError("Please enter a subject for your support request.")
        if len(subject) < 5:
            raise ValidationError("Subject must be at least 5 characters long.")
        return subject

    def clean_message(self):
        message = (self.cleaned_data.get('message') or '').strip()
        if not message:
            raise ValidationError("Please describe your issue or question.")
        if len(message) < 15:
            raise ValidationError("Please provide at least 15 characters in your description so our team can assist effectively.")
        if len(message) > 2000:
            raise ValidationError("Description cannot exceed 2000 characters.")
        return message
