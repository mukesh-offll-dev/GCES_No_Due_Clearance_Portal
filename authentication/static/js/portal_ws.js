/**
 * PortalWS: Real-Time Live Sync & WebSocket Manager for GCES No Due Clearance Portal
 * 
 * Features:
 *  - Real-time WebSocket event delivery with session authentication
 *  - Cross-tab synchronization via BroadcastChannel API
 *  - Resilient auto-reconnect with exponential backoff & jitter
 *  - Keep-alive heartbeat (every 20s) to prevent proxy timeouts
 *  - Seamless background synchronization
 */

(function (window) {
  'use strict';

  var listeners = {};
  var syncHandlers = [];
  var ws = null;
  var reconnectAttempts = 0;
  var maxReconnectAttempts = 50;
  var baseReconnectDelay = 800;
  var maxReconnectDelay = 8000;
  var pingIntervalId = null;
  var isExplicitlyClosed = false;
  var wasConnected = false;
  var broadcastChannel = null;

  var processedMsgIds = new Set();
  var maxRecentIds = 250;
  var lastEventTimestamps = {};

  function isDuplicateEvent(eventName, data) {
    if (!eventName) return true;

    // 1. Check unique message ID if provided
    var msgId = data && data.msg_id;
    if (msgId) {
      if (processedMsgIds.has(msgId)) {
        return true;
      }
      processedMsgIds.add(msgId);
      if (processedMsgIds.size > maxRecentIds) {
        var first = processedMsgIds.values().next().value;
        processedMsgIds.delete(first);
      }
    }

    // 2. Debounce identical eventName + payload within 600ms
    try {
      var eventKey = eventName + ':' + (data ? (data.id || data.student_id || data.timestamp || '') : '');
      var now = Date.now();
      if (lastEventTimestamps[eventKey] && (now - lastEventTimestamps[eventKey] < 600)) {
        return true;
      }
      lastEventTimestamps[eventKey] = now;
    } catch (e) {}

    return false;
  }

  // Initialize Cross-Tab Broadcast Channel if available
  if ('BroadcastChannel' in window) {
    try {
      broadcastChannel = new BroadcastChannel('gces_nodue_sync');
      broadcastChannel.onmessage = function (event) {
        if (event && event.data && event.data.event) {
          var eventName = event.data.event;
          if (isDuplicateEvent(eventName, event.data)) return;
          PortalWS.dispatch(eventName, event.data);
          PortalWS.dispatch('*', event.data);
        }
      };
    } catch (e) {
      // Ignore broadcast channel failure in sandboxed iframes
    }
  }

  function getWebSocketUrl() {
    var protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return protocol + '//' + window.location.host + '/ws/portal/';
  }

  function log() {
    if (window.console && window.console.log) {
      var args = Array.prototype.slice.call(arguments);
      args.unshift('%c[PortalWS]', 'color:#2563eb;font-weight:bold');
      window.console.log.apply(window.console, args);
    }
  }

  function warn() {
    if (window.console && window.console.warn) {
      var args = Array.prototype.slice.call(arguments);
      args.unshift('[PortalWS]');
      window.console.warn.apply(window.console, args);
    }
  }

  var PortalWS = {
    connect: function () {
      if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        return;
      }

      isExplicitlyClosed = false;
      var url = getWebSocketUrl();
      log('Connecting to', url);

      try {
        ws = new WebSocket(url);
      } catch (err) {
        warn('Failed to initialize WebSocket:', err);
        PortalWS.scheduleReconnect();
        return;
      }

      ws.onopen = function () {
        log('✅ Connected to', getWebSocketUrl());
        var isReconnection = wasConnected;
        wasConnected = true;
        reconnectAttempts = 0;
        PortalWS.startHeartbeat();
        PortalWS.dispatch('ws:connected', { reconnected: isReconnection });

        if (isReconnection) {
          log('Reconnected! Running synchronization handlers.');
          for (var i = 0; i < syncHandlers.length; i++) {
            try {
              syncHandlers[i]();
            } catch (e) {
              warn('Error in sync handler:', e);
            }
          }
        }
      };

      ws.onmessage = function (event) {
        try {
          var data = JSON.parse(event.data);
          var eventName = data.event || data.type;
          if (eventName && isDuplicateEvent(eventName, data)) {
            log('Skipping duplicate event:', eventName);
            return;
          }
          log('📨 Message received  event=' + eventName, data);
          if (eventName) {
            PortalWS.dispatch(eventName, data);
            PortalWS.dispatch('*', data);

            // Forward to other open tabs in the same browser
            if (broadcastChannel) {
              try {
                broadcastChannel.postMessage(data);
              } catch (e) {}
            }
          }
        } catch (e) {
          warn('Error parsing message payload:', e);
        }
      };

      ws.onclose = function (event) {
        log('Connection closed. Code:', event.code, 'Reason:', event.reason);
        PortalWS.stopHeartbeat();
        PortalWS.dispatch('ws:disconnected', { code: event.code });

        if (!isExplicitlyClosed) {
          PortalWS.scheduleReconnect();
        }
      };

      ws.onerror = function (error) {
        warn('WebSocket error encountered:', error);
      };
    },

    disconnect: function () {
      isExplicitlyClosed = true;
      PortalWS.stopHeartbeat();
      if (ws) {
        ws.close();
        ws = null;
      }
    },

    scheduleReconnect: function () {
      if (isExplicitlyClosed || reconnectAttempts >= maxReconnectAttempts) {
        return;
      }

      reconnectAttempts++;
      var delay = Math.min(baseReconnectDelay * Math.pow(1.4, reconnectAttempts - 1), maxReconnectDelay);
      var jitter = delay * (Math.random() * 0.2);
      var totalDelay = Math.round(delay + jitter);

      log('Reconnecting in ' + totalDelay + 'ms (attempt ' + reconnectAttempts + ')');
      setTimeout(function () {
        PortalWS.connect();
      }, totalDelay);
    },

    startHeartbeat: function () {
      PortalWS.stopHeartbeat();
      pingIntervalId = setInterval(function () {
        if (ws && ws.readyState === WebSocket.OPEN) {
          try {
            ws.send(JSON.stringify({ type: 'ping' }));
          } catch (e) {
            warn('Heartbeat send failed:', e);
          }
        }
      }, 20000);
    },

    stopHeartbeat: function () {
      if (pingIntervalId) {
        clearInterval(pingIntervalId);
        pingIntervalId = null;
      }
    },

    // Reload-free content refresh: re-fetch the CURRENT page over the network
    // and swap ONLY the <main> element's contents. This keeps the WebSocket
    // connection, page scripts, event listeners, header/footer, and any modals
    // outside <main> fully intact — no navigation, no full-page reload — while
    // guaranteeing the visible data matches the database (server is the source
    // of truth). Used for roster-level changes (promotion, access toggle, new
    // pending request) where a targeted DOM patch would be brittle.
    softReloadMain: function () {
        if (PortalWS._softReloadPending) return;
        PortalWS._softReloadPending = true;
        // Coalesce bursts of events into a single fetch.
        clearTimeout(PortalWS._softReloadTimer);
        PortalWS._softReloadTimer = setTimeout(function () {
            var url = window.location.href;
            fetch(url, {
                credentials: 'same-origin',
                headers: { 'X-Requested-With': 'PortalWS-SoftReload' }
            })
                .then(function (res) {
                    if (!res.ok) throw new Error('HTTP ' + res.status);
                    return res.text();
                })
                .then(function (html) {
                    var doc = new DOMParser().parseFromString(html, 'text/html');
                    var freshMain = doc.querySelector('main');
                    var currentMain = document.querySelector('main');
                    if (freshMain && currentMain) {
                        currentMain.innerHTML = freshMain.innerHTML;
                        // Let the page re-run any DOM-dependent initializers
                        // (e.g. countdown timers) on the freshly injected nodes.
                        if (typeof window.__reinitPage === 'function') {
                            try { window.__reinitPage(); } catch (e) { warn('reinit failed:', e); }
                        }
                        log('Content refreshed without page reload.');
                    } else {
                        warn('softReloadMain: <main> not found; skipping swap.');
                    }
                })
                .catch(function (err) {
                    warn('softReloadMain failed (leaving page as-is):', err);
                })
                .then(function () {
                    PortalWS._softReloadPending = false;
                });
        }, 250);
    },

    broadcastLocal: function (eventName, data) {
      var payload = Object.assign({ event: eventName }, data || {});
      PortalWS.dispatch(eventName, payload);
      if (broadcastChannel) {
        try {
          broadcastChannel.postMessage(payload);
        } catch (e) {}
      }
    },

    on: function (eventName, callback) {
      if (typeof callback !== 'function') return;
      if (!listeners[eventName]) {
        listeners[eventName] = [];
      }
      listeners[eventName].push(callback);
    },

    off: function (eventName, callback) {
      if (!listeners[eventName]) return;
      if (!callback) {
        delete listeners[eventName];
        return;
      }
      listeners[eventName] = listeners[eventName].filter(function (cb) {
        return cb !== callback;
      });
    },

    onSync: function (callback) {
      if (typeof callback === 'function') {
        syncHandlers.push(callback);
      }
    },

    dispatch: function (eventName, data) {
      if (listeners[eventName]) {
        var cbs = listeners[eventName].slice();
        for (var i = 0; i < cbs.length; i++) {
          try {
            cbs[i](data);
          } catch (e) {
            warn('Error executing listener for event ' + eventName + ':', e);
          }
        }
      }
    }
  };

  // Re-connect when tab becomes active / visible
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') {
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        PortalWS.connect();
      }
    }
  });

  // Auto-connect when DOM is loaded
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      PortalWS.connect();
    });
  } else {
    PortalWS.connect();
  }

  window.PortalWS = PortalWS;
})(window);
