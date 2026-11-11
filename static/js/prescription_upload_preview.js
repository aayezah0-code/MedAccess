/**
 * Client-side local preview controller for Patient Prescription Upload.
 * Supports instant local previews for JPG/JPEG/PNG/WEBP images and first-page
 * canvas previews for PDFs via PDF.js, with clean fallbacks and memory cleanup.
 */
(function () {
    'use strict';

    var MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024; // 10 MB
    var IMAGE_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.webp'];
    var IMAGE_MIME_TYPES = ['image/jpeg', 'image/png', 'image/webp'];
    var PDF_EXTENSION = '.pdf';
    var PDF_MIME_TYPE = 'application/pdf';

    function formatFileSize(bytes) {
        if (!bytes || bytes < 1024) {
            return (bytes || 0) + ' B';
        }
        if (bytes > 1024 * 1024) {
            return (bytes / (1024 * 1024)).toFixed(2) + ' MB';
        }
        return (bytes / 1024).toFixed(1) + ' KB';
    }

    function getFileExtension(filename) {
        if (!filename) return '';
        var idx = filename.lastIndexOf('.');
        return idx >= 0 ? filename.slice(idx).toLowerCase() : '';
    }

    function detectKind(file) {
        var ext = getFileExtension(file.name);
        var mime = (file.type || '').toLowerCase();

        if (IMAGE_EXTENSIONS.indexOf(ext) !== -1) {
            return { kind: 'image', label: ext.slice(1).toUpperCase() };
        }
        if (ext === PDF_EXTENSION) {
            return { kind: 'pdf', label: 'PDF' };
        }
        if (!ext && IMAGE_MIME_TYPES.indexOf(mime) !== -1) {
            return { kind: 'image', label: mime.split('/')[1].toUpperCase() };
        }
        if (!ext && mime === PDF_MIME_TYPE) {
            return { kind: 'pdf', label: 'PDF' };
        }
        return { kind: 'invalid', label: ext ? ext.slice(1).toUpperCase() : 'FILE' };
    }

    function initPrescriptionUploadPreview() {
        var dropzone = document.getElementById('file-dropzone');
        var emptyState = document.getElementById('dropzone-empty-state');
        var fileInput = document.getElementById('prescription-image-input');
        var previewContainer = document.getElementById('rx-preview-container');
        var previewStage = document.getElementById('rx-preview-stage');
        var previewBadge = document.getElementById('rx-preview-badge');
        var previewBadgeText = document.getElementById('rx-preview-badge-text');
        var previewPageInfo = document.getElementById('rx-preview-page-info');
        var previewLoading = document.getElementById('rx-preview-loading');
        var previewImg = document.getElementById('rx-preview-img');
        var previewCanvas = document.getElementById('rx-preview-canvas');
        var previewFallback = document.getElementById('rx-preview-fallback');
        var fallbackTitle = document.getElementById('rx-fallback-title');
        var fallbackDesc = document.getElementById('rx-fallback-desc');
        var fileInfo = document.getElementById('file-info');
        var selectedFileName = document.getElementById('selected-file-name');
        var selectedFileSize = document.getElementById('selected-file-size');
        var selectedFileMeta = document.getElementById('selected-file-meta');
        var removeBtn = document.getElementById('rx-preview-remove-btn');
        var clientError = document.getElementById('rx-client-error');
        var serverError = document.getElementById('rx-server-error');

        if (!dropzone || !fileInput || !fileInfo) {
            return;
        }

        // Configure PDF.js worker if PDF.js loaded cleanly
        var workerSrc = dropzone.getAttribute('data-pdf-worker-src');
        if (window.pdfjsLib && workerSrc && !window.pdfjsLib.GlobalWorkerOptions.workerSrc) {
            window.pdfjsLib.GlobalWorkerOptions.workerSrc = workerSrc;
        }

        var activeObjectUrl = null;
        var activeLoadingTask = null;
        var activeRenderTask = null;
        var previewGeneration = 0;

        function revokeActiveObjectUrl() {
            if (activeObjectUrl) {
                try {
                    URL.revokeObjectURL(activeObjectUrl);
                } catch (err) {
                    // Ignore revocation errors
                }
                activeObjectUrl = null;
            }
        }

        function cancelActivePdfTasks() {
            if (activeRenderTask && typeof activeRenderTask.cancel === 'function') {
                try {
                    activeRenderTask.cancel();
                } catch (err) {
                    // Ignore cancel errors
                }
            }
            activeRenderTask = null;

            if (activeLoadingTask && typeof activeLoadingTask.destroy === 'function') {
                try {
                    activeLoadingTask.destroy();
                } catch (err) {
                    // Ignore destroy errors
                }
            }
            activeLoadingTask = null;
        }

        function clearCanvas() {
            if (!previewCanvas) return;
            var ctx = previewCanvas.getContext('2d');
            if (ctx) {
                ctx.clearRect(0, 0, previewCanvas.width, previewCanvas.height);
            }
            previewCanvas.width = 0;
            previewCanvas.height = 0;
            previewCanvas.style.aspectRatio = '';
            previewCanvas.style.display = 'none';
        }

        function clearError() {
            dropzone.classList.remove('has-error');
            if (clientError) {
                clientError.textContent = '';
                clientError.style.display = 'none';
            }
        }

        function showError(message) {
            dropzone.classList.add('has-error');
            if (clientError) {
                clientError.textContent = message;
                clientError.style.display = 'block';
            }
        }

        function setBadge(kind, label, pageText) {
            if (!previewBadge || !previewBadgeText) return;
            previewBadge.classList.toggle('is-pdf', kind === 'pdf');
            previewBadge.classList.toggle('is-image', kind === 'image');
            previewBadgeText.textContent = label;
            previewBadge.style.display = 'inline-flex';

            if (previewPageInfo) {
                if (pageText) {
                    previewPageInfo.textContent = pageText;
                    previewPageInfo.style.display = 'inline-flex';
                } else {
                    previewPageInfo.textContent = '';
                    previewPageInfo.style.display = 'none';
                }
            }
        }

        function showFallback(title, desc) {
            if (previewLoading) previewLoading.style.display = 'none';
            if (previewImg) previewImg.style.display = 'none';
            clearCanvas();
            if (fallbackTitle) fallbackTitle.textContent = title;
            if (fallbackDesc) fallbackDesc.textContent = desc;
            if (previewFallback) previewFallback.style.display = 'flex';
        }

        function resetVisualStage() {
            revokeActiveObjectUrl();
            cancelActivePdfTasks();

            if (previewImg) {
                previewImg.onload = null;
                previewImg.onerror = null;
                previewImg.removeAttribute('src');
                previewImg.style.display = 'none';
            }
            clearCanvas();
            if (previewLoading) previewLoading.style.display = 'none';
            if (previewFallback) previewFallback.style.display = 'none';
            if (previewBadge) previewBadge.style.display = 'none';
            if (previewPageInfo) {
                previewPageInfo.textContent = '';
                previewPageInfo.style.display = 'none';
            }
            if (previewContainer) previewContainer.style.display = 'none';
        }

        function resetToEmptyState() {
            previewGeneration += 1;
            resetVisualStage();
            clearError();

            dropzone.classList.remove('has-file');
            if (emptyState) emptyState.style.display = '';
            fileInfo.style.display = 'none';
            selectedFileName.textContent = '';
            selectedFileSize.textContent = '';
            if (selectedFileMeta) {
                selectedFileMeta.textContent = '';
                selectedFileMeta.style.display = 'none';
            }
        }

        function populateFileMeta(file) {
            selectedFileName.textContent = file.name;
            selectedFileSize.textContent = formatFileSize(file.size);
            if (selectedFileMeta) {
                selectedFileMeta.textContent = '';
                selectedFileMeta.style.display = 'none';
            }
            fileInfo.style.display = 'flex';
        }

        function renderImagePreview(file, label, gen) {
            previewContainer.style.display = 'block';
            if (previewFallback) previewFallback.style.display = 'none';
            clearCanvas();
            setBadge('image', label, '');

            activeObjectUrl = URL.createObjectURL(file);
            previewImg.onload = function () {
                if (gen !== previewGeneration) return;
                if (previewLoading) previewLoading.style.display = 'none';
                previewImg.style.display = 'block';
                if (selectedFileMeta && previewImg.naturalWidth && previewImg.naturalHeight) {
                    selectedFileMeta.textContent = previewImg.naturalWidth + ' × ' + previewImg.naturalHeight + ' px';
                    selectedFileMeta.style.display = 'inline';
                }
            };
            previewImg.onerror = function () {
                if (gen !== previewGeneration) return;
                revokeActiveObjectUrl();
                showFallback(
                    'Image preview unavailable',
                    'Browser could not decode this image preview. You can still upload or select another file.'
                );
            };
            previewImg.src = activeObjectUrl;
        }

        async function renderPdfPreview(file, gen) {
            previewContainer.style.display = 'block';
            if (previewImg) previewImg.style.display = 'none';
            if (previewFallback) previewFallback.style.display = 'none';
            clearCanvas();
            setBadge('pdf', 'PDF', 'Rendering...');

            if (!window.pdfjsLib || typeof window.pdfjsLib.getDocument !== 'function') {
                setBadge('pdf', 'PDF', 'Page 1 preview unavailable');
                showFallback(
                    file.name,
                    'PDF preview library could not be loaded. Your PDF document is still ready to upload.'
                );
                return;
            }

            if (previewLoading) previewLoading.style.display = 'flex';

            var pdfDoc = null;
            try {
                var buffer = await file.arrayBuffer();
                if (gen !== previewGeneration) return;

                activeLoadingTask = window.pdfjsLib.getDocument({ data: buffer });
                pdfDoc = await activeLoadingTask.promise;
                if (gen !== previewGeneration) return;

                var totalPages = pdfDoc.numPages || 1;
                setBadge('pdf', 'PDF', totalPages > 1 ? ('Page 1 of ' + totalPages) : 'Page 1 of 1');
                if (selectedFileMeta) {
                    selectedFileMeta.textContent = totalPages + (totalPages === 1 ? ' page' : ' pages');
                    selectedFileMeta.style.display = 'inline';
                }

                var page = await pdfDoc.getPage(1);
                if (gen !== previewGeneration) return;

                var baseViewport = page.getViewport({ scale: 1 });
                var maxStageWidth = Math.max(220, (previewStage ? previewStage.clientWidth - 24 : 460));
                var maxStageHeight = window.innerWidth <= 600 ? 250 : 340;
                var cssScale = Math.min(
                    maxStageWidth / baseViewport.width,
                    maxStageHeight / baseViewport.height,
                    2.0
                );
                if (!isFinite(cssScale) || cssScale <= 0) {
                    cssScale = 1;
                }

                var dpr = Math.min(window.devicePixelRatio || 1, 2);
                var renderViewport = page.getViewport({ scale: cssScale * dpr });

                previewCanvas.width = Math.max(1, Math.floor(renderViewport.width));
                previewCanvas.height = Math.max(1, Math.floor(renderViewport.height));
                previewCanvas.style.aspectRatio = baseViewport.width + ' / ' + baseViewport.height;

                var context = previewCanvas.getContext('2d');
                activeRenderTask = page.render({
                    canvasContext: context,
                    viewport: renderViewport
                });
                await activeRenderTask.promise;
                if (gen !== previewGeneration) return;

                if (previewLoading) previewLoading.style.display = 'none';
                previewCanvas.style.display = 'block';
            } catch (err) {
                if (gen !== previewGeneration) return;
                if (err && err.name === 'RenderingCancelledException') return;

                setBadge('pdf', 'PDF', 'Preview unavailable');
                showFallback(
                    file.name,
                    'Could not render first page preview in browser. You can still upload or select a different PDF.'
                );
            } finally {
                activeRenderTask = null;
                activeLoadingTask = null;
                if (pdfDoc && typeof pdfDoc.destroy === 'function') {
                    try {
                        pdfDoc.destroy();
                    } catch (destroyErr) {
                        // Ignore cleanup errors
                    }
                }
            }
        }

        function handleFileSelection() {
            previewGeneration += 1;
            var currentGen = previewGeneration;

            if (serverError) {
                serverError.style.display = 'none';
            }

            if (!fileInput.files || !fileInput.files[0]) {
                resetToEmptyState();
                return;
            }

            var file = fileInput.files[0];
            resetVisualStage();
            clearError();

            dropzone.classList.add('has-file');
            if (emptyState) emptyState.style.display = 'none';
            populateFileMeta(file);

            var detected = detectKind(file);

            if (detected.kind === 'invalid') {
                showError(
                    'Unsupported file format. Please select a JPG, JPEG, PNG, WEBP, or PDF document.'
                );
                return;
            }

            if (file.size === 0) {
                showError('The selected file is empty. Please choose a valid prescription file.');
                return;
            }

            if (file.size > MAX_FILE_SIZE_BYTES) {
                var sizeMb = (file.size / (1024 * 1024)).toFixed(1);
                showError(
                    'File size (' + sizeMb + ' MB) exceeds the maximum limit of 10 MB.'
                );
                return;
            }

            if (detected.kind === 'image') {
                renderImagePreview(file, detected.label, currentGen);
            } else if (detected.kind === 'pdf') {
                renderPdfPreview(file, currentGen);
            }
        }

        fileInput.addEventListener('change', handleFileSelection);

        if (removeBtn) {
            removeBtn.addEventListener('click', function () {
                fileInput.value = '';
                resetToEmptyState();
            });
        }

        window.addEventListener('beforeunload', revokeActiveObjectUrl);

        // Restore preview if browser retained selected file across navigation
        if (fileInput.files && fileInput.files[0]) {
            handleFileSelection();
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initPrescriptionUploadPreview);
    } else {
        initPrescriptionUploadPreview();
    }
}());

