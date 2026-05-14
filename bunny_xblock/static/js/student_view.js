/**
 * BunnyStudentView — LMS-side click-to-load player.
 *
 * Mirrors `app/components/LazyBunnyEmbed.tsx` in the Cubite repo: the iframe
 * is only mounted after the learner clicks the poster, which keeps the
 * embedded Bunny player JS off every article view.
 *
 * Two bootstrap paths so this works in both Open edX runtimes:
 *
 *   1. Legacy LMS / workbench / older Studio — calls `window.BunnyStudentView`
 *      via XBlock's `initialize_js` mechanism. We expose it on `window`.
 *
 *   2. `frontend-app-learning` MFE — renders XBlock HTML/CSS/JS via React,
 *      doesn't reliably call `initialize_js` for every block. We self-boot
 *      on DOMContentLoaded by scanning the DOM for our root element and
 *      reading the embed URL + title from data-* attributes.
 *
 * `data-bunny-initialized` guards against double-binding when both paths
 * fire for the same block.
 */
(function () {
  "use strict";

  function init(root, config) {
    if (!root) return;
    if (root.getAttribute("data-bunny-initialized") === "1") return;
    root.setAttribute("data-bunny-initialized", "1");

    var posterBtn = root.querySelector('[data-action="play"]');
    var slot = root.querySelector(".bunny-xblock__iframe-slot");
    var fallback = root.querySelector(".bunny-xblock__fallback");

    // Config from XBlock runtime takes precedence; fall back to data-*
    // attributes the template renders for the self-boot path.
    var embedUrl =
      (config && config.embedUrl) ||
      root.getAttribute("data-embed-url") ||
      "";
    var title =
      (config && config.title) ||
      root.getAttribute("data-bunny-title") ||
      "Bunny video";

    if (!embedUrl) {
      if (fallback) fallback.hidden = false;
      if (posterBtn) posterBtn.disabled = true;
      return;
    }

    function mountIframe() {
      if (!slot || slot.hasChildNodes()) return;
      var iframe = document.createElement("iframe");
      iframe.src = embedUrl;
      iframe.title = title;
      iframe.loading = "lazy";
      iframe.allow =
        "accelerometer; gyroscope; autoplay; encrypted-media; picture-in-picture";
      iframe.setAttribute("allowfullscreen", "true");
      iframe.className = "bunny-xblock__iframe";
      slot.appendChild(iframe);
      slot.removeAttribute("aria-hidden");
      if (posterBtn) {
        posterBtn.hidden = true;
        posterBtn.setAttribute("aria-hidden", "true");
      }
    }

    if (posterBtn) {
      posterBtn.addEventListener("click", mountIframe);
    }
  }

  // Path 1 — legacy XBlock runtime. `initialize_js("BunnyStudentView", cfg)`
  // calls this with (runtime, element, config). `element` is the host node.
  window.BunnyStudentView = function (runtime, element, config) {
    var root = element && (
      element.classList && element.classList.contains("bunny-xblock--student")
        ? element
        : element.querySelector && element.querySelector(".bunny-xblock--student")
    );
    init(root, config);
  };

  // Path 2 — self-bootstrap. Scan the DOM for any of our roots that haven't
  // been initialized yet. Idempotent via data-bunny-initialized.
  function autoBoot() {
    var roots = document.querySelectorAll(".bunny-xblock--student");
    for (var i = 0; i < roots.length; i++) {
      init(roots[i], null);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", autoBoot);
  } else {
    // Already past DOMContentLoaded — run on next tick to give any
    // late-arriving DOM from the MFE's render path time to settle.
    setTimeout(autoBoot, 0);
  }

  // The MFE re-renders unit content on navigation between blocks without
  // a full page reload. Watch for new root elements appearing and bootstrap
  // them too. Light-touch: we only call autoBoot when *something* changes
  // and rely on `data-bunny-initialized` to skip already-bound roots.
  if (typeof MutationObserver !== "undefined") {
    var pending = false;
    var observer = new MutationObserver(function () {
      if (pending) return;
      pending = true;
      // Coalesce multiple mutations into a single boot pass via rAF /
      // setTimeout fallback.
      var schedule = window.requestAnimationFrame || function (fn) { return setTimeout(fn, 16); };
      schedule(function () {
        pending = false;
        autoBoot();
      });
    });
    function startObserver() {
      if (document.body) {
        observer.observe(document.body, { childList: true, subtree: true });
      }
    }
    if (document.body) {
      startObserver();
    } else {
      document.addEventListener("DOMContentLoaded", startObserver);
    }
  }
})();
