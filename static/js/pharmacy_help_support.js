/**
 * Interactive controller for Pharmacy Portal Help & Support:
 * - Accessible FAQ accordion expand/collapse
 * - Live FAQ keyword search & category filtering
 * - Support description live character counter
 */
(function () {
    'use strict';

    function initPharmacyHelpSupport() {
        var searchInput = document.getElementById('faq-search-input');
        var clearBtn = document.getElementById('faq-search-clear');
        var resetBtn = document.getElementById('faq-reset-btn');
        var resultsCount = document.getElementById('faq-results-count');
        var emptyState = document.getElementById('faq-empty-state');
        var accordionRoot = document.getElementById('faq-accordion-root');
        var categoryTabs = Array.prototype.slice.call(document.querySelectorAll('[data-faq-filter]'));
        var categoryGroups = Array.prototype.slice.call(document.querySelectorAll('.faq-category-group'));
        var faqItems = Array.prototype.slice.call(document.querySelectorAll('[data-faq-item]'));

        var activeCategory = 'all';
        var totalCount = faqItems.length;

        function setExpanded(card, shouldExpand) {
            var btn = card.querySelector('.faq-question-btn');
            var panel = card.querySelector('.faq-answer-panel');
            if (!btn || !panel) return;

            card.classList.toggle('is-open', shouldExpand);
            btn.setAttribute('aria-expanded', shouldExpand ? 'true' : 'false');
            panel.hidden = !shouldExpand;
        }

        // Accordion toggle behavior
        faqItems.forEach(function (card) {
            var btn = card.querySelector('.faq-question-btn');
            if (!btn) return;
            btn.addEventListener('click', function () {
                var isExpanded = btn.getAttribute('aria-expanded') === 'true';
                setExpanded(card, !isExpanded);
            });
        });

        function applyFilters() {
            var query = searchInput ? searchInput.value.trim().toLowerCase() : '';
            if (clearBtn) {
                clearBtn.style.display = query ? 'inline-flex' : 'none';
            }

            var visibleCount = 0;

            faqItems.forEach(function (card) {
                var itemCategory = card.getAttribute('data-faq-category') || '';
                var searchText = card.getAttribute('data-faq-search') || '';
                var categoryMatches = activeCategory === 'all' || itemCategory === activeCategory;
                var queryMatches = !query || searchText.indexOf(query) !== -1;
                var visible = categoryMatches && queryMatches;

                card.style.display = visible ? '' : 'none';
                if (visible) {
                    visibleCount += 1;
                    if (query) {
                        setExpanded(card, true);
                    }
                }
            });

            categoryGroups.forEach(function (group) {
                var groupItems = Array.prototype.slice.call(group.querySelectorAll('[data-faq-item]'));
                var groupVisibleCount = groupItems.filter(function (el) {
                    return el.style.display !== 'none';
                }).length;

                group.style.display = groupVisibleCount > 0 ? '' : 'none';
                var badge = group.querySelector('.faq-category-count');
                if (badge) {
                    badge.textContent = String(groupVisibleCount);
                }
            });

            if (resultsCount) {
                if (query || activeCategory !== 'all') {
                    resultsCount.textContent =
                        'Showing ' + visibleCount + ' of ' + totalCount + ' help ' +
                        (visibleCount === 1 ? 'topic' : 'topics');
                } else {
                    resultsCount.textContent = 'Showing all ' + totalCount + ' help topics';
                }
            }

            if (emptyState && accordionRoot) {
                emptyState.style.display = visibleCount === 0 ? 'flex' : 'none';
                accordionRoot.style.display = visibleCount === 0 ? 'none' : '';
            }
        }

        if (searchInput) {
            searchInput.addEventListener('input', applyFilters);
        }

        if (clearBtn) {
            clearBtn.addEventListener('click', function () {
                if (searchInput) {
                    searchInput.value = '';
                    searchInput.focus();
                }
                applyFilters();
            });
        }

        categoryTabs.forEach(function (tab) {
            tab.addEventListener('click', function () {
                activeCategory = tab.getAttribute('data-faq-filter') || 'all';
                categoryTabs.forEach(function (other) {
                    var isCurrent = other === tab;
                    other.classList.toggle('is-active', isCurrent);
                    other.setAttribute('aria-selected', isCurrent ? 'true' : 'false');
                });
                applyFilters();
            });
        });

        if (resetBtn) {
            resetBtn.addEventListener('click', function () {
                activeCategory = 'all';
                if (searchInput) {
                    searchInput.value = '';
                }
                categoryTabs.forEach(function (tab) {
                    var isAll = tab.getAttribute('data-faq-filter') === 'all';
                    tab.classList.toggle('is-active', isAll);
                    tab.setAttribute('aria-selected', isAll ? 'true' : 'false');
                });
                faqItems.forEach(function (card) {
                    setExpanded(card, false);
                });
                applyFilters();
            });
        }

        // Character counter for support description textarea
        var messageTextarea = document.getElementById('support-message-textarea');
        var messageCounter = document.getElementById('support-message-counter');
        if (messageTextarea && messageCounter) {
            var updateCounter = function () {
                var len = messageTextarea.value.length;
                messageCounter.textContent = len + ' / 2000';
            };
            messageTextarea.addEventListener('input', updateCounter);
            updateCounter();
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initPharmacyHelpSupport);
    } else {
        initPharmacyHelpSupport();
    }
}());

