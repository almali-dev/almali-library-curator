/* Follow a running analysis (#147).
 *
 * S2: the status box promised that the outcome appears there, but nothing
 * refreshed the page. This script asks the app for the state of the analysis
 * while it runs, reusing the sign-in poll's stop rules: one request every few
 * seconds, no request once the answer stops changing, and a bounded number of
 * polls. When the analysis finishes or fails, the page reloads once, so the
 * recorded outcome and the list it filled are on screen. Without scripts the
 * copy of the status box says to reload the page.
 */
(function () {
  'use strict';

  var region = document.getElementById('scan-status');
  if (!region || !window.fetch) {
    return;
  }

  var url = region.getAttribute('data-status-url');
  var interval = 3000;
  var attempts = 600;
  var polls = 0;
  var reloaded = false;
  var timer = null;

  function reload() {
    if (reloaded) {
      return;
    }
    reloaded = true;
    if (timer) {
      window.clearInterval(timer);
      timer = null;
    }
    window.location.reload();
  }

  function refresh() {
    polls += 1;
    if (polls > attempts) {
      if (timer) {
        window.clearInterval(timer);
        timer = null;
      }
      return;
    }
    window.fetch(url, {
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
        // The analysis left the running state: finished, failed or never run.
        if (!data.running) {
          reload();
        }
      })
      .catch(function () {
        // The app may be unreachable: the owner reloads by hand.
      });
  }

  timer = window.setInterval(refresh, interval);
})();
