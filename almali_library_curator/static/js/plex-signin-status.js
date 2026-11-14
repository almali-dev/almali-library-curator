/* Poll the Plex sign-in status and update only the status line (#16).
 *
 * The waiting screen used to reload itself every three seconds, which threw
 * away a half-typed manual address or token and moved the page under a
 * screen reader. This script asks the app for the state of the sign-in and
 * rewrites the status line in place: the page itself is never reloaded, so
 * the manual form keeps what the owner typed. Without JavaScript the
 * "Check again" link still reloads the page, exactly as before.
 */
(function () {
  'use strict';

  var region = document.getElementById('plex-signin-status');
  var check = document.getElementById('plex-signin-check');
  if (!region || !check || !window.fetch) {
    return;
  }

  var url = region.getAttribute('data-status-url');
  var icons = region.getAttribute('data-icons-url');
  var timer = null;
  var pending = true;

  function setIcon(symbol) {
    var marker = region.querySelector('use');
    if (marker && symbol) {
      marker.setAttribute('href', icons + '#' + symbol);
    }
  }

  function setMessage(words) {
    var text = region.querySelector('span');
    if (text && words) {
      text.textContent = words;
    }
  }

  function refresh() {
    return window.fetch(url, {
      headers: {'Accept': 'application/json'},
      credentials: 'same-origin'
    })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        if (!data) {
          return;
        }
        setMessage(data.message);
        setIcon(data.icon);
        if (!data.pending) {
          // The sign-in left the waiting state: the owner decides when the
          // page is reloaded, so the link works as a plain link again.
          pending = false;
          if (timer) {
            window.clearInterval(timer);
            timer = null;
          }
        }
      })
      .catch(function () {
        // The app may be unreachable: the owner retries by hand.
      });
  }

  check.addEventListener('click', function (event) {
    if (!pending) {
      return;
    }
    // While the sign-in waits, "Check again" refreshes the status line only.
    event.preventDefault();
    refresh();
  });

  timer = window.setInterval(refresh, 3000);
})();
