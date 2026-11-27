/* Keep the outcome of an action readable where the page lands (#147).
 *
 * S1: the flash used to render at the top of <main> while a row action
 * redirects to the row far below it, so the owner saw no feedback at all.
 * The flash now lives in a sticky region (hud.css), and this script adds the
 * two things a static page cannot: the "js" class that reveals the close
 * button (without scripts no button that does nothing is shown) and the
 * button's own behaviour. S4: a notice the server marks with
 * data-focus-on-load is where the redirect wants the reader to start, so it
 * takes the focus once.
 */
(function () {
  'use strict';

  document.documentElement.classList.add('js');

  var closers = document.querySelectorAll('[data-dismiss-flash]');
  Array.prototype.forEach.call(closers, function (button) {
    button.addEventListener('click', function () {
      var message = button.closest('.flash-message');
      if (message && message.parentNode) {
        message.parentNode.removeChild(message);
      }
    });
  });

  var notice = document.querySelector('[data-focus-on-load]');
  if (notice && typeof notice.focus === 'function') {
    notice.focus();
  }
})();
