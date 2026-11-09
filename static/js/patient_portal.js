// Patient Portal — sidebar toggle for mobile
(function () {
    var toggleBtn = document.getElementById('sidebar-toggle-btn');
    var sidebar = document.getElementById('portal-sidebar');

    if (toggleBtn && sidebar) {
        toggleBtn.addEventListener('click', function () {
            sidebar.classList.toggle('open');
        });

        // Close sidebar when clicking outside on mobile
        document.addEventListener('click', function (event) {
            var isInsideSidebar = sidebar.contains(event.target);
            var isToggleBtn = toggleBtn.contains(event.target);
            if (!isInsideSidebar && !isToggleBtn && sidebar.classList.contains('open')) {
                sidebar.classList.remove('open');
            }
        });
    }
}());


// Prescription Details Modal Controller (Phase 4, Task 4B)
(function () {
    var activeModal = null;
    var lastActiveTrigger = null;

    function openModal(modalId, triggerEl) {
        var modal = document.getElementById(modalId);
        if (!modal) return;

        // Close any previously open modal cleanly
        if (activeModal && activeModal !== modal) {
            closeModal();
        }

        lastActiveTrigger = triggerEl || document.activeElement;
        activeModal = modal;

        modal.classList.add('active');
        modal.setAttribute('aria-hidden', 'false');
        document.body.classList.add('modal-open');

        // Focus close button or first interactive element for keyboard accessibility
        var focusTarget = modal.querySelector('.rx-modal-close-btn, [data-modal-close], button, a[href]');
        if (focusTarget) {
            try {
                focusTarget.focus();
            } catch (err) {
                // Focus failed silently
            }
        }
    }

    function closeModal() {
        if (!activeModal) return;

        activeModal.classList.remove('active');
        activeModal.setAttribute('aria-hidden', 'true');
        document.body.classList.remove('modal-open');

        // Return focus to the trigger button that opened this modal
        if (lastActiveTrigger && typeof lastActiveTrigger.focus === 'function') {
            try {
                lastActiveTrigger.focus();
            } catch (err) {
                // Focus restoration failed silently
            }
        }

        activeModal = null;
        lastActiveTrigger = null;
    }

    // Event delegation for opening and closing modals without page reload
    document.addEventListener('click', function (event) {
        // Open trigger
        var openBtn = event.target.closest('[data-modal-target]');
        if (openBtn) {
            event.preventDefault();
            var targetId = openBtn.getAttribute('data-modal-target');
            if (targetId) {
                openModal(targetId, openBtn);
            }
            return;
        }

        // Close trigger
        var closeBtn = event.target.closest('[data-modal-close], .rx-modal-close-btn');
        if (closeBtn) {
            event.preventDefault();
            closeModal();
            return;
        }

        // Click outside dialog content (on backdrop overlay)
        if (activeModal && event.target === activeModal) {
            event.preventDefault();
            closeModal();
        }
    });

    // Keyboard navigation: Escape key to close, Tab key to trap focus inside dialog
    document.addEventListener('keydown', function (event) {
        if (!activeModal) return;

        // Escape key closes modal
        if (event.key === 'Escape' || event.key === 'Esc' || event.keyCode === 27) {
            event.preventDefault();
            closeModal();
            return;
        }

        // Tab key traps focus inside the modal dialog
        if (event.key === 'Tab' || event.keyCode === 9) {
            var focusables = activeModal.querySelectorAll(
                'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), [tabindex="0"]'
            );
            if (!focusables || focusables.length === 0) return;

            var firstFocusable = focusables[0];
            var lastFocusable = focusables[focusables.length - 1];

            if (event.shiftKey) {
                // Shift + Tab: if on first element, wrap to last
                if (document.activeElement === firstFocusable) {
                    event.preventDefault();
                    lastFocusable.focus();
                }
            } else {
                // Tab: if on last element, wrap to first
                if (document.activeElement === lastFocusable) {
                    event.preventDefault();
                    firstFocusable.focus();
                }
            }
        }
    });
}());


// Prescription Medicine Confirmation Controller (Phase 4, Task 4C)
(function () {
    function getCookie(name) {
        var cookieValue = null;
        if (document.cookie && document.cookie !== '') {
            var cookies = document.cookie.split(';');
            for (var i = 0; i < cookies.length; i++) {
                var cookie = cookies[i].trim();
                if (cookie.substring(0, name.length + 1) === (name + '=')) {
                    cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                    break;
                }
            }
        }
        return cookieValue;
    }

    document.addEventListener('submit', function (event) {
        var form = event.target.closest('.js-confirm-extracted-form');
        if (!form) return;

        // Ensure browser HTML5 validation passes before intercepting
        if (!form.checkValidity()) {
            return;
        }

        event.preventDefault();

        var submitBtn = form.querySelector('button[type="submit"]');
        var originalBtnHtml = submitBtn ? submitBtn.innerHTML : '';
        if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.innerHTML = '<span>Saving...</span>';
        }

        var formData = new FormData(form);
        var actionUrl = form.getAttribute('action');
        var csrfToken = getCookie('csrftoken') || (form.querySelector('[name=csrfmiddlewaretoken]') ? form.querySelector('[name=csrfmiddlewaretoken]').value : '');

        fetch(actionUrl, {
            method: 'POST',
            body: formData,
            headers: {
                'X-Requested-With': 'XMLHttpRequest',
                'X-CSRFToken': csrfToken,
                'Accept': 'application/json'
            }
        })
        .then(function (response) {
            return response.json().then(function (data) {
                return { ok: response.ok, status: response.status, data: data };
            });
        })
        .then(function (res) {
            if (res.ok && res.data && res.data.status === 'success') {
                // Success: remove or update the candidate card
                var card = form.closest('.rx-candidate-card');
                if (card) {
                    card.style.transition = 'opacity 0.3s ease, transform 0.3s ease';
                    card.style.opacity = '0';
                    card.style.transform = 'scale(0.96)';
                    setTimeout(function () {
                        // Reload the current page to ensure complete server-rendered state and counters
                        window.location.reload();
                    }, 350);
                } else {
                    window.location.reload();
                }
            } else {
                var errorMsg = (res.data && res.data.message) ? res.data.message : 'An error occurred while confirming this medicine.';
                alert(errorMsg);
                if (submitBtn) {
                    submitBtn.disabled = false;
                    submitBtn.innerHTML = originalBtnHtml;
                }
            }
        })
        .catch(function () {
            // Network fallback: submit natively
            form.submit();
        });
    });
}());
