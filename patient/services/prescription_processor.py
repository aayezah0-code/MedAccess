"""
MedAccess Prescription File Processing Foundation (Phase 3, Task 3A).

Provides reusable, secure validation and in-memory preparation of uploaded
doctor's prescription images (JPG, PNG, WEBP) and multi-page PDFs for
future Google Gemini Vision analysis.

Key Capabilities:
- Content-based file signature verification (rejects spoofed extensions).
- Safe file size enforcement (max 10 MB).
- EXIF orientation correction and dimension optimization (max 1600px) with Pillow.
- Multi-page PDF rendering at 2x resolution (144 DPI) via PyMuPDF (fitz).
- Safe memory handling: zero permanent intermediate files created.
- Strict rejection of encrypted, corrupted, empty, or oversized documents (>10 pages).
"""

from dataclasses import dataclass, field
import io
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from PIL import Image, ImageOps
import pymupdf


# --- Configuration & Limits ---
MAX_FILE_SIZE_BYTES: int = 10 * 1024 * 1024  # 10 MB maximum file size
MAX_PDF_PAGES: int = 10                      # Maximum 10 pages per prescription
MAX_IMAGE_DIMENSION: int = 1600              # Maximum width/height in pixels
JPEG_QUALITY: int = 85                       # Optimized JPEG compression quality
PDF_RENDER_DPI: int = 144                    # 2x resolution rendering for crisp text


# --- Exceptions ---
class PrescriptionProcessingError(Exception):
    """Base exception for all prescription file processing errors."""
    pass


class PrescriptionValidationError(PrescriptionProcessingError):
    """Raised when an uploaded file fails format, size, encryption, or integrity checks."""
    pass


# --- Data Structures ---
@dataclass
class ProcessedPage:
    """
    Represents a single processed page or image of a prescription ready for Gemini Vision.
    """
    page_number: int        # 1-indexed (1 for single image, 1..N for multi-page PDF)
    image_bytes: bytes      # In-memory image bytes (JPEG for images, PNG for PDF pages)
    format: str             # 'JPEG' or 'PNG'
    mime_type: str          # 'image/jpeg' or 'image/png'
    width: int              # Width in pixels
    height: int             # Height in pixels


@dataclass
class ProcessedPrescription:
    """
    Structured result of prescription file preparation containing all rendered pages.
    """
    file_type: str                  # 'image' or 'pdf'
    original_format: str            # 'JPEG', 'PNG', 'WEBP', or 'PDF'
    page_count: int                 # Total number of pages
    pages: List[ProcessedPage]      # Rendered page objects in exact order
    metadata: Dict[str, Any] = field(default_factory=dict)


# --- File Validation & Inspection ---
def detect_file_signature(header: bytes) -> Optional[str]:
    """
    Inspects leading bytes of a file to detect true file format based on magic numbers.
    Never trusts file extensions or browser-supplied MIME types.
    """
    if len(header) < 4:
        return None

    # JPEG magic bytes: FF D8 FF
    if header.startswith(b'\xff\xd8\xff'):
        return 'JPEG'

    # PNG magic bytes: 89 50 4E 47 0D 0A 1A 0A
    if header.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'PNG'

    # WEBP magic bytes: RIFF....WEBP
    if len(header) >= 12 and header.startswith(b'RIFF') and header[8:12] == b'WEBP':
        return 'WEBP'

    # PDF magic bytes: %PDF-
    if header.startswith(b'%PDF-'):
        return 'PDF'

    return None


def read_file_bytes_safely(file_input: Any, max_size_bytes: int = MAX_FILE_SIZE_BYTES) -> bytes:
    """
    Safely reads file content into bytes while enforcing maximum size limits.
    Prevents unbounded memory exhaustion from oversized files.
    """
    # 1. Direct bytes
    if isinstance(file_input, bytes):
        if len(file_input) > max_size_bytes:
            raise PrescriptionValidationError(
                f"File size exceeds the maximum limit of {max_size_bytes // (1024 * 1024)} MB."
            )
        return file_input

    # 2. File path (str or Path)
    if isinstance(file_input, (str, Path)):
        path = Path(file_input)
        if not path.is_file():
            raise PrescriptionValidationError(f"Prescription file not found at: {path}")
        size = path.stat().st_size
        if size > max_size_bytes:
            raise PrescriptionValidationError(
                f"File size ({size / (1024 * 1024):.1f} MB) exceeds the limit of {max_size_bytes // (1024 * 1024)} MB."
            )
        return path.read_bytes()

    # 3. File-like object (UploadedFile, BytesIO, open file)
    if hasattr(file_input, 'read'):
        # Check size attribute if provided (Django UploadedFile)
        if hasattr(file_input, 'size') and file_input.size is not None:
            if file_input.size > max_size_bytes:
                raise PrescriptionValidationError(
                    f"File size ({file_input.size / (1024 * 1024):.1f} MB) exceeds the limit of {max_size_bytes // (1024 * 1024)} MB."
                )

        if hasattr(file_input, 'seek'):
            file_input.seek(0)

        # Read capped amount (+1 byte to detect overflow without loading unbounded data)
        data = file_input.read(max_size_bytes + 1)
        if len(data) > max_size_bytes:
            raise PrescriptionValidationError(
                f"File size exceeds the maximum limit of {max_size_bytes // (1024 * 1024)} MB."
            )

        if hasattr(file_input, 'seek'):
            file_input.seek(0)

        return data

    raise PrescriptionValidationError("Unsupported file input type. Expected bytes, path, or file-like object.")


def validate_prescription_file(file_input: Any, max_size_bytes: int = MAX_FILE_SIZE_BYTES) -> Tuple[str, bytes]:
    """
    Validates file size and inspects actual binary signatures.
    Returns:
        tuple (detected_format: str, file_bytes: bytes)
    Raises:
        PrescriptionValidationError on size or format violation.
    """
    data = read_file_bytes_safely(file_input, max_size_bytes=max_size_bytes)
    if not data:
        raise PrescriptionValidationError("The uploaded file is empty.")

    detected_format = detect_file_signature(data[:32])
    if not detected_format:
        raise PrescriptionValidationError(
            "Unsupported file format. Please upload a clear prescription photo (JPG, JPEG, PNG, WEBP) or a PDF document."
        )

    return detected_format, data


# --- Image Processing ---
def process_prescription_image(
    image_input: Any,
    max_dimension: int = MAX_IMAGE_DIMENSION,
    quality: int = JPEG_QUALITY
) -> ProcessedPrescription:
    """
    Processes an uploaded prescription image (JPEG, PNG, WEBP):
    1. Validates magic bytes and file size limit.
    2. Opens safely with Pillow and checks pixel integrity.
    3. Corrects EXIF orientation (e.g. mobile photo taken at an angle).
    4. Normalizes to RGB (compositing alpha transparency on a clean white background).
    5. Scales down only if longest dimension exceeds max_dimension (maintains aspect ratio & handwriting clarity).
    6. Encodes output as optimized JPEG in memory.
    """
    detected_format, data = validate_prescription_file(image_input)
    if detected_format == 'PDF':
        raise PrescriptionValidationError("PDF files must be processed using process_prescription_pdf().")

    try:
        with Image.open(io.BytesIO(data)) as raw_img:
            # Fully load and verify pixel data to detect corruption
            raw_img.load()
            orig_size = raw_img.size
            orig_mode = raw_img.mode

            # 1. Correct EXIF orientation
            transposed_img = ImageOps.exif_transpose(raw_img)
            if transposed_img is None:
                transposed_img = raw_img

            # 2. Convert to RGB safely
            # Handle alpha transparency (PNG/WEBP) by pasting on a crisp white canvas
            if transposed_img.mode in ('RGBA', 'LA') or (transposed_img.mode == 'P' and 'transparency' in transposed_img.info):
                canvas = Image.new('RGB', transposed_img.size, (255, 255, 255))
                rgba_img = transposed_img.convert('RGBA')
                canvas.paste(rgba_img, mask=rgba_img.split()[3])
                rgb_img = canvas
            else:
                rgb_img = transposed_img.convert('RGB')

            width, height = rgb_img.size
            resized = False

            # 3. Resize only when the longest dimension exceeds max_dimension
            longest_dim = max(width, height)
            if longest_dim > max_dimension:
                scale_ratio = float(max_dimension) / float(longest_dim)
                new_width = max(1, int(round(width * scale_ratio)))
                new_height = max(1, int(round(height * scale_ratio)))
                rgb_img = rgb_img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                width, height = new_width, new_height
                resized = True

            # 4. Save to optimized JPEG bytes in memory
            output_buffer = io.BytesIO()
            rgb_img.save(
                output_buffer,
                format='JPEG',
                quality=quality,
                optimize=True
            )
            processed_bytes = output_buffer.getvalue()

    except (Image.UnidentifiedImageError, OSError, SyntaxError, ValueError) as e:
        raise PrescriptionValidationError("Corrupted or invalid image file. Please upload a clear, readable prescription image.") from e

    page = ProcessedPage(
        page_number=1,
        image_bytes=processed_bytes,
        format='JPEG',
        mime_type='image/jpeg',
        width=width,
        height=height
    )

    return ProcessedPrescription(
        file_type='image',
        original_format=detected_format,
        page_count=1,
        pages=[page],
        metadata={
            'original_dimensions': orig_size,
            'original_mode': orig_mode,
            'processed_dimensions': (width, height),
            'resized': resized,
            'processed_size_bytes': len(processed_bytes)
        }
    )


# --- PDF Processing ---
def process_prescription_pdf(
    pdf_input: Any,
    max_pages: int = MAX_PDF_PAGES,
    dpi: int = PDF_RENDER_DPI
) -> ProcessedPrescription:
    """
    Processes an uploaded PDF prescription:
    1. Validates magic bytes (%PDF-) and file size limit.
    2. Opens document with PyMuPDF.
    3. Rejects encrypted or password-protected documents.
    4. Validates page count (1 to max_pages).
    5. Renders each page at 2x resolution (144 DPI) into crisp PNG images.
    6. Preserves exact page order and reports errors if any page fails.
    7. Closes document safely in a finally block.
    """
    detected_format, data = validate_prescription_file(pdf_input)
    if detected_format != 'PDF':
        raise PrescriptionValidationError(f"Expected a PDF document, but detected {detected_format} file.")

    doc = None
    try:
        try:
            doc = pymupdf.open(stream=data, filetype='pdf')
        except Exception as e:
            raise PrescriptionValidationError("Corrupted or invalid PDF file. Please ensure the document is not damaged.") from e

        # Check for encryption / password requirements
        if doc.is_encrypted or doc.needs_pass:
            raise PrescriptionValidationError("The uploaded PDF is password-protected or encrypted. Please upload an unencrypted prescription.")

        total_pages = len(doc)
        if total_pages == 0:
            raise PrescriptionValidationError("The uploaded PDF document contains no pages.")

        if total_pages > max_pages:
            raise PrescriptionValidationError(
                f"The PDF contains {total_pages} pages, which exceeds the allowed limit of {max_pages} pages per prescription."
            )

        processed_pages: List[ProcessedPage] = []
        page_metadata: List[Dict[str, Any]] = []

        # Render every page in exact sequence
        for page_idx in range(total_pages):
            try:
                page = doc[page_idx]
                # Render at 144 DPI (2x standard 72 DPI) for optimal OCR/Vision recognition
                pix = page.get_pixmap(dpi=dpi)
                png_bytes = pix.tobytes('png')

                processed_page = ProcessedPage(
                    page_number=page_idx + 1,
                    image_bytes=png_bytes,
                    format='PNG',
                    mime_type='image/png',
                    width=pix.width,
                    height=pix.height
                )
                processed_pages.append(processed_page)
                page_metadata.append({
                    'page_number': page_idx + 1,
                    'width': pix.width,
                    'height': pix.height,
                    'size_bytes': len(png_bytes)
                })

            except Exception as e:
                raise PrescriptionProcessingError(
                    f"Failed to render page {page_idx + 1} of the prescription PDF. Please verify the document is not corrupted."
                ) from e

        return ProcessedPrescription(
            file_type='pdf',
            original_format='PDF',
            page_count=total_pages,
            pages=processed_pages,
            metadata={
                'dpi': dpi,
                'total_pages': total_pages,
                'pages_info': page_metadata,
                'original_size_bytes': len(data)
            }
        )

    finally:
        if doc is not None:
            doc.close()


# --- Unified Processing Entrypoint ---
def process_prescription_file(
    file_input: Any,
    max_image_dimension: int = MAX_IMAGE_DIMENSION,
    max_pdf_pages: int = MAX_PDF_PAGES
) -> ProcessedPrescription:
    """
    Unified entry point for prescription document preparation.
    Inspects file content and automatically dispatches to image or PDF processing.
    """
    detected_format, data = validate_prescription_file(file_input)

    if detected_format == 'PDF':
        return process_prescription_pdf(data, max_pages=max_pdf_pages)
    else:
        return process_prescription_image(data, max_dimension=max_image_dimension)

