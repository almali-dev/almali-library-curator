/* Say a slow save is working (#147).
 *
 * S6: the Plex save tests the connection for up to ten seconds, the service
 * key save does the same, the connection test probes every service and a
 * scan takes minutes. A form marked data-busy sets aria-busy on itself,
 * disables its own submit button and swaps in the page's translated
 * "working…" label until the page unloads. It touches only the form that was
 * submitted, so the other forms of the screen stay usable, and without
 * scripts nothing changes at all.
 */
(function () {
  'use strict';

  var forms = document.querySelectorAll('form[data-busy]');
  Array.prototype.forEach.call(forms, function (form) {
    form.addEventListener('submit', function () {
      var label = form.getAttribute('data-busy-label');
      var button = form.querySelector('button[type="submit"], button:not([type])');
      form.setAttribute('aria-busy', 'true');
      if (!button) {
        return;
      }
      button.setAttribute('aria-busy', 'true');
      if (label) {
        button.textContent = label;
      }
      button.disabled = true;
    });
  });
})();
