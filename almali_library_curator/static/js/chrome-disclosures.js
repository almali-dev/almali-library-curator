/* Fold the secondary chrome on small screens (#65).
 *
 * On a phone the brand bar, the navigation, the sources panel, the language
 * links, the KPI cards and the scan steps used to push the candidate list two
 * or three screens down. They are <details> elements now: open in the markup,
 * so the shell is exactly what it always was without JavaScript and on every
 * desktop, and closed here while the viewport is narrower than the desktop
 * breakpoint (992 px, the width where the sidebar becomes a fixed column).
 *
 * Progressive enhancement only: this script never changes what a page means,
 * and every disclosure stays operable by keyboard and screen reader.
 */
(function () {
  'use strict';

  var DESKTOP = '(min-width: 992px)';
  var panels = document.querySelectorAll('details[data-collapse-small]');
  if (!panels.length) {
    return;
  }
  var wide = window.matchMedia ? window.matchMedia(DESKTOP) : null;

  function apply() {
    var open = !wide || wide.matches;
    for (var index = 0; index < panels.length; index++) {
      if (open) {
        panels[index].setAttribute('open', '');
      } else {
        panels[index].removeAttribute('open');
      }
    }
  }

  apply();
  if (wide && wide.addEventListener) {
    wide.addEventListener('change', apply);
  }
})();
