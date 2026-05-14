/**
 * BunnyStudentView — LMS-side click-to-load player.
 *
 * Mirrors `app/components/LazyBunnyEmbed.tsx` in the Cubite repo: the iframe
 * is only mounted after the learner clicks the poster, which keeps the
 * embedded Bunny player JS off every article view.
 *
 * `initialize_js` passes `runtime`, `element`, and the config dict we set on
 * the server in `BunnyVideoXBlock.student_view`.
 */
function BunnyStudentView(runtime, element, config) {
  "use strict";

  var root = element.querySelector(".bunny-xblock--student");
  if (!root) return;

  var posterBtn = root.querySelector('[data-action="play"]');
  var slot = root.querySelector(".bunny-xblock__iframe-slot");
  var fallback = root.querySelector(".bunny-xblock__fallback");
  var embedUrl = (config && config.embedUrl) || root.getAttribute("data-embed-url") || "";
  var title = (config && config.title) || "Bunny video";

  if (!embedUrl) {
    // No video set up yet — degrade visibly rather than leaving a dead poster.
    if (fallback) fallback.hidden = false;
    if (posterBtn) posterBtn.disabled = true;
    return;
  }

  function mountIframe() {
    if (slot.hasChildNodes()) return; // already mounted, avoid double-load
    var iframe = document.createElement("iframe");
    iframe.src = embedUrl;
    iframe.title = title;
    iframe.loading = "lazy";
    iframe.allow = "accelerometer; gyroscope; autoplay; encrypted-media; picture-in-picture";
    iframe.setAttribute("allowfullscreen", "true");
    iframe.className = "bunny-xblock__iframe";
    slot.appendChild(iframe);
    slot.removeAttribute("aria-hidden");
    if (posterBtn) {
      posterBtn.hidden = true;
      posterBtn.setAttribute("aria-hidden", "true");
    }
  }

  posterBtn.addEventListener("click", mountIframe);
  // Keyboard parity: Enter/Space already trigger button click natively, so
  // no extra handler is needed for a11y.
}
