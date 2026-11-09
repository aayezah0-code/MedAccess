/**
 * MedAccess Global Light/Dark Theme Switcher
 * Persists theme in localStorage ('medaccess_theme')
 * Respects system prefers-color-scheme if no manual preference is saved.
 * Seamlessly transitions theme across all portals (Public, Patient, Pharmacy, Admin).
 */
(function () {
    'use strict';

    var STORAGE_KEY = 'medaccess_theme';

    function getPreferredTheme() {
        var stored = null;
        try {
            stored = localStorage.getItem(STORAGE_KEY);
        } catch (e) {
            // LocalStorage might be blocked or restricted
        }
        if (stored === 'light' || stored === 'dark') {
            return stored;
        }
        if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
            return 'dark';
        }
        return 'light';
    }

    function applyTheme(theme) {
        if (theme !== 'light' && theme !== 'dark') {
            theme = getPreferredTheme();
        }

        // Apply attribute to both <html> and <body> for 100% selector reliability
        document.documentElement.setAttribute('data-theme', theme);
        if (document.body) {
            document.body.setAttribute('data-theme', theme);
        }

        // Update accessibility attributes and titles on all theme buttons
        var toggleBtns = document.querySelectorAll('.theme-toggle-btn');
        toggleBtns.forEach(function (btn) {
            btn.setAttribute('aria-pressed', theme === 'dark' ? 'true' : 'false');
            btn.setAttribute('title', theme === 'dark' ? 'Switch to Light Theme' : 'Switch to Dark Theme');
            btn.setAttribute('aria-label', theme === 'dark' ? 'Switch to Light Theme' : 'Switch to Dark Theme');
            btn.onclick = function (e) {
                if (e) {
                    e.preventDefault();
                    e.stopPropagation();
                }
                toggleTheme();
            };
        });
    }

    function toggleTheme() {
        var current = document.documentElement.getAttribute('data-theme') || 
                      (document.body && document.body.getAttribute('data-theme')) || 
                      getPreferredTheme();
        var next = current === 'dark' ? 'light' : 'dark';
        try {
            localStorage.setItem(STORAGE_KEY, next);
        } catch (e) {
            // Ignore storage write errors in private mode
        }
        applyTheme(next);
    }

    // Expose globally for debug or external callers
    window.medaccessToggleTheme = toggleTheme;

    function initTheme() {
        var activeTheme = document.documentElement.getAttribute('data-theme') || getPreferredTheme();
        applyTheme(activeTheme);
    }

    // Initialize immediately
    initTheme();

    // Re-verify once DOM is ready (in case body was not yet ready)
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initTheme);
    } else {
        initTheme();
    }

    // Document-level event delegation ensures clicks are never missed,
    // regardless of whether the user clicked the button itself or the nested SVG icon/paths.
    document.addEventListener('click', function (e) {
        var btn = e.target.closest('.theme-toggle-btn');
        if (btn) {
            e.preventDefault();
            e.stopPropagation();
            toggleTheme();
        }
    });

    // Listen for OS color scheme changes if user hasn't explicitly set a preference
    if (window.matchMedia) {
        window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function (e) {
            var stored = null;
            try {
                stored = localStorage.getItem(STORAGE_KEY);
            } catch (err) {}
            if (!stored) {
                applyTheme(e.matches ? 'dark' : 'light');
            }
        });
    }
})();
