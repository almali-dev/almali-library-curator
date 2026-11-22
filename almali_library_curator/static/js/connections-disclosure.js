/* Open the service card an anchor names (S7).
 *
 * Each card of the Connections screen is addressable by its service
 * ("/connections#sonarr"), so a link can point at the card of the connection
 * it talks about. The server cannot see the fragment, so the browser opens
 * that card here, and only that one: the group is a native exclusive
 * accordion, so the browser closes the others.
 *
 * Progressive enhancement only: without JavaScript the link still lands on
 * the card, with its state, what the service adds and its Configure button in
 * reach. Nothing on the page changes meaning.
 */
(function () {
  'use strict';

  var GROUP = 'details[name="connections"]';

  function open() {
    var name = (window.location.hash || '').replace(/^#/, '');
    if (!name) {
      return;
    }
    var card = document.getElementById(name);
    var disclosure = card && card.querySelector(GROUP);
    if (!disclosure || disclosure.open) {
      return;
    }
    disclosure.open = true;
    disclosure.scrollIntoView({block: 'start'});
  }

  open();
  window.addEventListener('hashchange', open);
})();
