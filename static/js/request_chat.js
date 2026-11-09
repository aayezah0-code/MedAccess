/**
 * MedAccess — Two-Way Prescription Request Chat Client
 * Handles real-time polling, message bubbles, character limits, keyboard shortcuts,
 * auto-scroll, read receipts, and read-only archiving for Patient and Pharmacy portals.
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

    function formatTime(isoString) {
        if (!isoString) return '';
        var date = new Date(isoString);
        var now = new Date();

        var hours = date.getHours();
        var minutes = date.getMinutes();
        var ampm = hours >= 12 ? 'PM' : 'AM';
        hours = hours % 12;
        hours = hours ? hours : 12;
        var minutesStr = minutes < 10 ? '0' + minutes : minutes;
        var timeStr = hours + ':' + minutesStr + ' ' + ampm;

        // If message is from today, show only time
        if (date.toDateString() === now.toDateString()) {
            return timeStr;
        }

        // Otherwise show Month Day, Time
        var monthNames = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
        return monthNames[date.getMonth()] + ' ' + date.getDate() + ', ' + timeStr;
    }

    function initRequestChat() {
        var container = document.getElementById('chat-messages-container');
        if (!container) return;

        var messagesUrl = container.getAttribute('data-messages-url');
        if (!messagesUrl) return;

        var form = document.getElementById('chat-send-form');
        var textarea = document.getElementById('chat-input-text');
        var sendBtn = document.getElementById('chat-send-btn');
        var charCounter = document.getElementById('chat-char-counter');
        var errorBanner = document.getElementById('chat-error-message');
        var cardEl = container.closest('.rx-chat-card') || container.parentElement;

        var knownMessageIds = new Set();
        var lastMessageId = 0;
        var isPolling = false;
        var isSending = false;
        var isWritable = container.getAttribute('data-is-writable') === 'true';

        function showError(msg) {
            if (!errorBanner) return;
            errorBanner.textContent = msg;
            errorBanner.style.display = 'block';
        }

        function hideError() {
            if (!errorBanner) return;
            errorBanner.textContent = '';
            errorBanner.style.display = 'none';
        }

        function switchToArchived(reason) {
            isWritable = false;
            container.setAttribute('data-is-writable', 'false');

            // Update status pill in header
            if (cardEl) {
                var statusPill = cardEl.querySelector('.chat-status-pill');
                if (statusPill) {
                    statusPill.className = 'chat-status-pill chat-status-archived';
                    statusPill.textContent = 'Archived';
                    statusPill.setAttribute('title', 'This conversation is preserved as a permanent historical record.');
                }
            }

            // Replace form with archived notice if present
            if (form && form.parentElement) {
                var notice = document.createElement('div');
                notice.className = 'chat-archived-notice';
                notice.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
                    '<rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect>' +
                    '<path d="M7 11V7a5 5 0 0 1 10 0v4"></path>' +
                    '</svg>' +
                    '<span>' + (reason || 'Messaging is archived and read-only for this request.') + '</span>';
                form.parentElement.replaceChild(notice, form);
            }
        }

        function isUserNearBottom() {
            var threshold = 100;
            return (container.scrollHeight - container.scrollTop - container.clientHeight) <= threshold;
        }

        function scrollToBottom(force) {
            if (force) {
                container.scrollTop = container.scrollHeight;
            } else {
                try {
                    container.scrollTo({ top: container.scrollHeight, behavior: 'smooth' });
                } catch (e) {
                    container.scrollTop = container.scrollHeight;
                }
            }
        }

        function renderMessageNode(msg) {
            var bubbleWrap = document.createElement('div');
            bubbleWrap.className = 'chat-msg-row ' + (msg.is_current_user ? 'chat-msg-outgoing' : 'chat-msg-incoming');
            bubbleWrap.setAttribute('data-msg-id', msg.id);

            // Sender name for incoming messages
            if (!msg.is_current_user && msg.sender_name) {
                var senderSpan = document.createElement('span');
                senderSpan.className = 'chat-sender-name';
                senderSpan.textContent = msg.sender_name;
                bubbleWrap.appendChild(senderSpan);
            }

            // Bubble body (SAFELY POPULATED VIA textContent)
            var bubble = document.createElement('div');
            bubble.className = 'chat-bubble';
            bubble.textContent = msg.message_text;
            bubbleWrap.appendChild(bubble);

            // Metadata row: time + read indicator
            var metaRow = document.createElement('div');
            metaRow.className = 'chat-meta-row';

            var timeSpan = document.createElement('span');
            timeSpan.className = 'chat-time';
            timeSpan.textContent = formatTime(msg.created_at);
            metaRow.appendChild(timeSpan);

            if (msg.is_current_user) {
                var readReceipt = document.createElement('span');
                readReceipt.className = 'chat-receipt' + (msg.is_read ? ' chat-read' : '');
                readReceipt.textContent = msg.is_read ? '✓ Read' : '✓ Sent';
                metaRow.appendChild(readReceipt);
            }

            bubbleWrap.appendChild(metaRow);
            return bubbleWrap;
        }

        function renderEmptyState() {
            var empty = document.createElement('div');
            empty.className = 'chat-empty-state';
            empty.id = 'chat-empty-state';
            empty.innerHTML = '<div class="chat-empty-icon">' +
                '<svg viewBox="0 0 24 24" width="32" height="32" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">' +
                '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>' +
                '</svg>' +
                '</div>' +
                '<div class="chat-empty-title">No messages yet</div>' +
                '<div class="chat-empty-sub">' + (isWritable ? 'Send a message below to start the conversation.' : 'No messages were exchanged during this order.') + '</div>';
            container.appendChild(empty);
        }

        function removeEmptyState() {
            var empty = document.getElementById('chat-empty-state');
            if (empty) empty.remove();
        }

        function fetchMessages(isInitial) {
            if (isPolling) return;
            isPolling = true;

            var url = messagesUrl;
            if (lastMessageId > 0) {
                url += (url.indexOf('?') === -1 ? '?' : '&') + 'after_id=' + lastMessageId;
            }

            var wasNearBottom = isUserNearBottom();

            fetch(url, {
                headers: { 'Accept': 'application/json' },
                credentials: 'same-origin'
            })
            .then(function (res) {
                if (!res.ok) throw new Error('Fetch messages failed: ' + res.status);
                return res.json();
            })
            .then(function (data) {
                if (data && data.status === 'success') {
                    // Check if server reports read-only status
                    if (data.is_writable === false && isWritable) {
                        switchToArchived('This request is now ' + (data.request_status || 'archived') + '. Messaging is read-only.');
                    }

                    // Remove initial loading spinner
                    if (isInitial) {
                        var loading = document.getElementById('chat-loading-state');
                        if (loading) loading.remove();
                    }

                    var newMessages = data.messages || [];

                    if (isInitial && newMessages.length === 0) {
                        renderEmptyState();
                    } else if (newMessages.length > 0) {
                        removeEmptyState();
                        var hasAdded = false;

                        for (var i = 0; i < newMessages.length; i++) {
                            var m = newMessages[i];
                            if (!knownMessageIds.has(m.id)) {
                                knownMessageIds.add(m.id);
                                if (m.id > lastMessageId) {
                                    lastMessageId = m.id;
                                }
                                container.appendChild(renderMessageNode(m));
                                hasAdded = true;
                            }
                        }

                        if (hasAdded && (isInitial || wasNearBottom)) {
                            scrollToBottom(isInitial);
                        }
                    }
                }
            })
            .catch(function (err) {
                if (isInitial) {
                    var loading = document.getElementById('chat-loading-state');
                    if (loading) {
                        loading.innerHTML = '<span style="color: #ef4444;">Failed to load messages. Refresh the page to retry.</span>';
                    }
                }
            })
            .finally(function () {
                isPolling = false;
            });
        }

        function handleSendMessage(e) {
            if (e) e.preventDefault();
            if (isSending || !textarea) return;

            var rawText = textarea.value;
            var text = rawText.trim();
            if (!text) {
                showError('Please enter a message before sending.');
                return;
            }
            if (text.length > 1000) {
                showError('Message cannot exceed 1000 characters.');
                return;
            }

            hideError();
            isSending = true;
            if (sendBtn) sendBtn.disabled = true;

            fetch(messagesUrl, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'X-CSRFToken': getCsrfToken()
                },
                credentials: 'same-origin',
                body: JSON.stringify({ message_text: text })
            })
            .then(function (res) {
                return res.json().then(function (data) {
                    return { ok: res.ok, status: res.status, data: data };
                });
            })
            .then(function (result) {
                if (result.ok && result.data && result.data.status === 'success') {
                    // Success! Clear input
                    textarea.value = '';
                    if (charCounter) charCounter.textContent = '0 / 1000';
                    removeEmptyState();

                    var sentMsg = result.data.message;
                    if (sentMsg && !knownMessageIds.has(sentMsg.id)) {
                        knownMessageIds.add(sentMsg.id);
                        if (sentMsg.id > lastMessageId) {
                            lastMessageId = sentMsg.id;
                        }
                        container.appendChild(renderMessageNode(sentMsg));
                        scrollToBottom(true);
                    }
                } else {
                    var errMsg = (result.data && result.data.error) || 'Failed to send message.';
                    showError(errMsg);
                    if (result.status === 403 && errMsg.toLowerCase().indexOf('archived') !== -1) {
                        switchToArchived(errMsg);
                    }
                }
            })
            .catch(function (err) {
                showError('Network error while sending message. Please try again.');
            })
            .finally(function () {
                isSending = false;
                if (sendBtn) {
                    sendBtn.disabled = !textarea.value.trim();
                }
                textarea.focus();
            });
        }

        // Attach input listener for character counting & send button state
        if (textarea) {
            textarea.addEventListener('input', function () {
                var len = textarea.value.length;
                if (charCounter) {
                    charCounter.textContent = len + ' / 1000';
                    if (len > 1000) {
                        charCounter.style.color = '#ef4444';
                    } else {
                        charCounter.style.color = '#94a3b8';
                    }
                }
                if (sendBtn) {
                    sendBtn.disabled = (len === 0 || len > 1000 || isSending);
                }
            });

            // Keyboard shortcut: Enter to send, Shift+Enter for new line
            textarea.addEventListener('keydown', function (e) {
                if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    if (!sendBtn.disabled) {
                        handleSendMessage();
                    }
                }
            });
        }

        if (form) {
            form.addEventListener('submit', handleSendMessage);
        }

        // Initial load
        fetchMessages(true);

        // Background polling every 5 seconds
        var pollTimer = setInterval(function () {
            if (!document.hidden) {
                fetchMessages(false);
            }
        }, 5000);

        // Visibility resume
        document.addEventListener('visibilitychange', function () {
            if (!document.hidden) {
                fetchMessages(false);
            }
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initRequestChat);
    } else {
        initRequestChat();
    }
})();

