/**
 * MedAccess — Notification Center Client
 * Handles notification bell badge, dropdown menu, unread count polling,
 * mark-as-read and mark-all-as-read interactions for Patient and Pharmacy portals.
 */
(function () {
    'use strict';

    function getCookie(name) {
        if (!document.cookie || document.cookie === '') return null;
        var cookies = document.cookie.split(';');
        for (var i = 0; i < cookies.length; i++) {
            var cookie = cookies[i].trim();
            if (cookie.substring(0, name.length + 1) === (name + '=')) {
                return decodeURIComponent(cookie.substring(name.length + 1));
            }
        }
        return null;
    }

    function getCsrfToken() {
        var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
        if (input && input.value) return input.value;
        return getCookie('csrftoken') || '';
    }

    function formatTimeAgo(isoString) {
        if (!isoString) return '';
        var date = new Date(isoString);
        var now = new Date();
        var diffSec = Math.floor((now - date) / 1000);
        if (diffSec < 0) diffSec = 0;

        if (diffSec < 60) return 'Just now';
        var diffMin = Math.floor(diffSec / 60);
        if (diffMin < 60) return diffMin + 'm ago';
        var diffHour = Math.floor(diffMin / 60);
        if (diffHour < 24) return diffHour + 'h ago';
        var diffDay = Math.floor(diffHour / 24);
        if (diffDay < 7) return diffDay + 'd ago';

        return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    }

    function getNotificationIconSvg(type) {
        switch (type) {
            case 'REQUEST_ACCEPTED':
            case 'REQUEST_COMPLETED':
                return '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline></svg>';
            case 'REQUEST_PROCESSING':
                return '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>';
            case 'REQUEST_READY':
                return '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 2L3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z"></path><line x1="3" y1="6" x2="21" y2="6"></line><path d="M16 10a4 4 0 0 1-8 0"></path></svg>';
            case 'REQUEST_REJECTED':
                return '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="15" y1="9" x2="9" y2="15"></line><line x1="9" y1="9" x2="15" y2="15"></line></svg>';
            case 'REQUEST_CANCELLED':
                return '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="8" y1="12" x2="16" y2="12"></line></svg>';
            case 'NEW_MESSAGE':
                return '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>';
            case 'REQUEST_SUBMITTED':
            default:
                return '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"></path><rect x="8" y="2" width="8" height="4" rx="1" ry="1"></rect></svg>';
        }
    }

    function getNotificationIconClass(type) {
        switch (type) {
            case 'REQUEST_ACCEPTED':
            case 'REQUEST_COMPLETED':
                return 'notif-type-success';
            case 'REQUEST_PROCESSING':
                return 'notif-type-purple';
            case 'REQUEST_READY':
                return 'notif-type-teal';
            case 'REQUEST_REJECTED':
                return 'notif-type-danger';
            case 'REQUEST_CANCELLED':
                return 'notif-type-neutral';
            case 'NEW_MESSAGE':
                return 'notif-type-indigo';
            case 'REQUEST_SUBMITTED':
            default:
                return 'notif-type-blue';
        }
    }

    function initNotificationCenter() {
        var wrapper = document.getElementById('notif-bell-wrapper');
        if (!wrapper) return;

        var apiList = wrapper.getAttribute('data-api-list');
        var apiUnread = wrapper.getAttribute('data-api-unread');
        var apiMarkAll = wrapper.getAttribute('data-api-mark-all');
        var apiMarkReadTemplate = wrapper.getAttribute('data-api-mark-read');

        if (!apiList || !apiUnread) return;

        var bellBtn = document.getElementById('notif-bell-btn');
        var notifBadge = document.getElementById('notif-badge');
        var dropdown = document.getElementById('notif-dropdown');
        var unreadPill = document.getElementById('notif-unread-pill');
        var markAllBtn = document.getElementById('notif-mark-all-btn');
        var notifBody = document.getElementById('notif-body');
        var notifFooter = document.getElementById('notif-footer');
        var loadMoreBtn = document.getElementById('notif-load-more-btn');

        var isOpen = false;
        var isLoading = false;
        var unreadCount = 0;
        var currentPage = 1;
        var hasNextPage = false;

        function updateUnreadBadge(count) {
            unreadCount = parseInt(count, 10) || 0;
            if (notifBadge) {
                if (unreadCount > 0) {
                    notifBadge.textContent = unreadCount > 99 ? '99+' : unreadCount;
                    notifBadge.style.display = 'inline-flex';
                } else {
                    notifBadge.textContent = '0';
                    notifBadge.style.display = 'none';
                }
            }

            if (unreadPill) {
                if (unreadCount > 0) {
                    unreadPill.textContent = unreadCount + ' new';
                    unreadPill.style.display = 'inline-block';
                } else {
                    unreadPill.style.display = 'none';
                }
            }

            if (markAllBtn) {
                markAllBtn.style.display = unreadCount > 0 ? 'inline-block' : 'none';
            }
        }

        function fetchUnreadCount() {
            fetch(apiUnread, {
                headers: { 'Accept': 'application/json' },
                credentials: 'same-origin'
            })
            .then(function (res) {
                if (!res.ok) throw new Error('Unread count failed: ' + res.status);
                return res.json();
            })
            .then(function (data) {
                if (data && data.status === 'success') {
                    updateUnreadBadge(data.unread_count);
                }
            })
            .catch(function () {
                // Silently swallow background polling errors
            });
        }

        function createNotificationItemEl(notif) {
            var item = document.createElement('a');
            item.className = 'notif-item' + (!notif.is_read ? ' unread' : '');
            item.href = notif.navigation_url || 'javascript:void(0)';
            item.setAttribute('data-id', notif.id);

            // Left icon
            var iconBox = document.createElement('div');
            iconBox.className = 'notif-item-icon ' + getNotificationIconClass(notif.notification_type);
            iconBox.innerHTML = getNotificationIconSvg(notif.notification_type);
            item.appendChild(iconBox);

            // Content body
            var contentBox = document.createElement('div');
            contentBox.className = 'notif-item-content';

            var headerRow = document.createElement('div');
            headerRow.className = 'notif-item-title';

            var titleSpan = document.createElement('span');
            titleSpan.className = 'notif-item-title-text';
            titleSpan.textContent = notif.title || notif.notification_type_display || 'Notification';
            headerRow.appendChild(titleSpan);

            var timeSpan = document.createElement('span');
            timeSpan.className = 'notif-item-time';
            timeSpan.textContent = formatTimeAgo(notif.created_at);
            headerRow.appendChild(timeSpan);

            contentBox.appendChild(headerRow);

            var msgP = document.createElement('p');
            msgP.className = 'notif-item-msg';
            msgP.textContent = notif.message || '';
            contentBox.appendChild(msgP);

            item.appendChild(contentBox);

            // Right unread indicator dot
            if (!notif.is_read) {
                var dot = document.createElement('span');
                dot.className = 'notif-item-dot';
                dot.setAttribute('title', 'Unread');
                item.appendChild(dot);
            }

            // Click interaction
            item.addEventListener('click', function (e) {
                if (!notif.is_read && apiMarkReadTemplate) {
                    var markReadUrl = apiMarkReadTemplate.replace('/0/', '/' + notif.id + '/');
                    // Mark as read immediately on server
                    fetch(markReadUrl, {
                        method: 'POST',
                        headers: {
                            'Accept': 'application/json',
                            'X-CSRFToken': getCsrfToken()
                        },
                        credentials: 'same-origin'
                    })
                    .then(function (res) { return res.json(); })
                    .then(function (resData) {
                        if (resData && resData.status === 'success') {
                            if (unreadCount > 0) updateUnreadBadge(unreadCount - 1);
                        }
                    })
                    .catch(function () {});
                }

                // If navigation url is set and different from current location, allow standard navigation
                if (notif.navigation_url && notif.navigation_url !== '#' && notif.navigation_url !== 'javascript:void(0)') {
                    // Let native anchor link navigate
                } else {
                    e.preventDefault();
                }
            });

            return item;
        }

        function fetchNotifications(page, append) {
            if (isLoading) return;
            isLoading = true;

            if (!append && notifBody) {
                notifBody.innerHTML = '<div class="notif-loading"><div class="notif-spinner"></div><span>Loading notifications...</span></div>';
            }

            var url = apiList + '?page=' + (page || 1);
            fetch(url, {
                headers: { 'Accept': 'application/json' },
                credentials: 'same-origin'
            })
            .then(function (res) {
                if (!res.ok) throw new Error('Notifications list failed: ' + res.status);
                return res.json();
            })
            .then(function (data) {
                if (data && data.status === 'success') {
                    currentPage = (data.pagination && data.pagination.page) || 1;
                    hasNextPage = (data.pagination && data.pagination.has_next) || false;
                    updateUnreadBadge(data.unread_count);

                    if (!append && notifBody) {
                        notifBody.innerHTML = '';
                    }

                    if (data.notifications && data.notifications.length > 0) {
                        for (var i = 0; i < data.notifications.length; i++) {
                            notifBody.appendChild(createNotificationItemEl(data.notifications[i]));
                        }
                    } else if (!append && notifBody) {
                        notifBody.innerHTML = '<div class="notif-empty">' +
                            '<svg viewBox="0 0 24 24" width="36" height="36" fill="none" stroke="#94a3b8" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">' +
                            '<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"></path>' +
                            '<path d="M13.73 21a2 2 0 0 1-3.46 0"></path>' +
                            '</svg>' +
                            '<div style="font-weight: 600; color: #334155; margin-top: 0.5rem;">No notifications yet</div>' +
                            '<div style="font-size: 0.775rem; color: #64748b; margin-top: 0.2rem;">You will receive alerts here when requests are updated or messages arrive.</div>' +
                            '</div>';
                    }

                    if (notifFooter) {
                        notifFooter.style.display = hasNextPage ? 'block' : 'none';
                    }
                }
            })
            .catch(function (err) {
                if (!append && notifBody) {
                    notifBody.innerHTML = '<div class="notif-error">Failed to load notifications. Please try again.</div>';
                }
            })
            .finally(function () {
                isLoading = false;
            });
        }

        function openDropdown() {
            isOpen = true;
            dropdown.style.display = 'flex';
            bellBtn.setAttribute('aria-expanded', 'true');
            fetchNotifications(1, false);
        }

        function closeDropdown() {
            isOpen = false;
            dropdown.style.display = 'none';
            bellBtn.setAttribute('aria-expanded', 'false');
        }

        bellBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            if (isOpen) {
                closeDropdown();
            } else {
                openDropdown();
            }
        });

        if (dropdown) {
            dropdown.addEventListener('click', function (e) {
                e.stopPropagation();
            });
        }

        document.addEventListener('click', function (e) {
            if (isOpen && !wrapper.contains(e.target)) {
                closeDropdown();
            }
        });

        document.addEventListener('keydown', function (e) {
            if (isOpen && e.key === 'Escape') {
                closeDropdown();
                bellBtn.focus();
            }
        });

        if (markAllBtn && apiMarkAll) {
            markAllBtn.addEventListener('click', function (e) {
                e.preventDefault();
                markAllBtn.disabled = true;
                markAllBtn.textContent = 'Marking...';

                fetch(apiMarkAll, {
                    method: 'POST',
                    headers: {
                        'Accept': 'application/json',
                        'X-CSRFToken': getCsrfToken()
                    },
                    credentials: 'same-origin'
                })
                .then(function (res) { return res.json(); })
                .then(function (data) {
                    if (data && data.status === 'success') {
                        updateUnreadBadge(0);
                        // Visually clear unread styles from existing elements in DOM
                        var unreadItems = notifBody.querySelectorAll('.notif-item.unread');
                        for (var i = 0; i < unreadItems.length; i++) {
                            unreadItems[i].classList.remove('unread');
                            var dot = unreadItems[i].querySelector('.notif-item-dot');
                            if (dot) dot.remove();
                        }
                    }
                })
                .catch(function () {})
                .finally(function () {
                    markAllBtn.disabled = false;
                    markAllBtn.textContent = 'Mark all read';
                });
            });
        }

        if (loadMoreBtn) {
            loadMoreBtn.addEventListener('click', function (e) {
                e.preventDefault();
                if (hasNextPage) {
                    fetchNotifications(currentPage + 1, true);
                }
            });
        }

        // Initial unread count fetch on page load
        fetchUnreadCount();

        // Background polling for unread count every 25 seconds
        var pollInterval = setInterval(function () {
            if (!document.hidden) {
                fetchUnreadCount();
            }
        }, 25000);

        // Resume immediately when tab visibility changes to visible
        document.addEventListener('visibilitychange', function () {
            if (!document.hidden) {
                fetchUnreadCount();
                if (isOpen) {
                    fetchNotifications(1, false);
                }
            }
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initNotificationCenter);
    } else {
        initNotificationCenter();
    }
})();

