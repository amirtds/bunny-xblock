/**
 * BunnyAuthorView — Studio-side inline authoring UI.
 *
 * State machine driven by the XBlock's `status` field (server-rendered) plus
 * JS-driven transitions for upload progress. Mirrors the Cubite EditorJS
 * plugin in shape; v0.1 ships single-video-per-block (no library picker).
 *
 *   empty  ──drop/click──▶  uploading ──TUS success──▶  processing
 *                                                    │
 *                                                    ▼ (poll loop)
 *                                                  ready  ◀── webhook
 *                                                    │
 *                                       failed (rare; from poll/webhook)
 *
 * Defensive panel hiding: each transition sets `hidden` AND toggles inline
 * `style.display`. The CSS reinstates `[hidden] { display: none !important }`
 * but if Studio injects any global rule that overrides the attribute again
 * (it has happened in the past), the inline style is the fallback.
 */

/* global tus */
function BunnyAuthorView(runtime, element, config) {
  "use strict";

  // ---- DOM lookup -----------------------------------------------------------------

  var root = element.querySelector(".bunny-xblock--author");
  if (!root) return;

  var panels = {
    empty: root.querySelector('[data-panel="empty"]'),
    uploading: root.querySelector('[data-panel="uploading"]'),
    processing: root.querySelector('[data-panel="processing"]'),
    ready: root.querySelector('[data-panel="ready"]'),
    failed: root.querySelector('[data-panel="failed"]'),
  };
  var fileInput = root.querySelector("[data-bunny-file]");
  var dropzone = root.querySelector("[data-bunny-dropzone]");
  var progressBar = root.querySelector("[data-bunny-progress]");
  var progressPct = root.querySelector("[data-bunny-pct]");
  var filenameLabel = root.querySelector("[data-bunny-filename]");
  var errorBox = root.querySelector("[data-bunny-error]");
  var failMessage = root.querySelector("[data-bunny-fail-message]");
  var titleInputs = root.querySelectorAll("[data-bunny-title]");

  var modal = root.querySelector("[data-bunny-modal]");
  var guidLabel = root.querySelector("[data-bunny-guid]");
  var durationContainer = root.querySelector("[data-bunny-duration]");
  var durationValue = root.querySelector("[data-bunny-duration-value]");
  var elapsedContainer = root.querySelector("[data-bunny-elapsed]");
  var elapsedValue = root.querySelector("[data-bunny-elapsed-value]");
  var thumbnailImg = root.querySelector("[data-bunny-thumbnail-img]");
  var thumbnailFile = root.querySelector("[data-bunny-thumbnail-file]");
  var thumbnailStatus = root.querySelector("[data-bunny-thumbnail-status]");

  // ---- State ----------------------------------------------------------------------

  var state = {
    guid: config.guid || "",
    libraryId: config.libraryId || "",
    title: config.title || "",
    status: config.status || (config.guid ? "encoding" : ""),
    durationSec: config.durationSec || 0,
    thumbnailUrl: config.thumbnailUrl || "",
    currentUpload: null,
    pollAbort: null,
  };

  var endpoints = config.endpoints || {};
  var setVideoUrl = runtime.handlerUrl(element, "set_video");
  var updateStatusUrl = runtime.handlerUrl(element, "update_status");
  var updateTitleUrl = runtime.handlerUrl(element, "update_title");
  var updateThumbnailUrl = runtime.handlerUrl(element, "update_thumbnail");
  var clearVideoUrl = runtime.handlerUrl(element, "clear_video");

  // ---- Panel switching (defensive: attribute + inline style) ----------------------

  function show(panel) {
    Object.keys(panels).forEach(function (key) {
      if (!panels[key]) return;
      var match = key === panel;
      panels[key].hidden = !match;
      // Belt-and-suspenders: inline display ensures we hide even if some
      // upstream CSS overrides `[hidden]`. The v0.1 bug was exactly this
      // (a `display: block` rule outweighing the UA `[hidden]` rule).
      panels[key].style.display = match ? "" : "none";
    });
    root.setAttribute("data-state", panel);

    if (panel === "processing" && state.guid) startPolling();
    if (panel !== "processing" && state.pollAbort) {
      state.pollAbort.abort();
      state.pollAbort = null;
    }
    if (panel !== "processing") stopElapsedTimer();
  }

  function showError(msg) {
    if (!errorBox) return;
    errorBox.textContent = msg;
    errorBox.hidden = !msg;
    errorBox.style.display = msg ? "" : "none";
  }

  // ---- Networking helpers ---------------------------------------------------------

  // Read the `csrftoken` cookie Django sets on every authenticated session.
  // DRF's SessionAuthentication enforces a CSRF check on POST/DELETE, so
  // any state-changing request from Studio JS must echo it back in the
  // X-CSRFToken header. Without this the API returns 403 with
  //   {"detail":"CSRF Failed: …"}
  // — which is exactly the failure mode that bit v0.1.
  function getCookie(name) {
    var m = document.cookie.match("(^|; )" + name.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\$&") + "=([^;]*)");
    return m ? decodeURIComponent(m[2]) : "";
  }

  function csrfHeaders(extra) {
    var headers = {
      "X-CSRFToken": getCookie("csrftoken"),
      "X-Requested-With": "XMLHttpRequest",
    };
    if (extra) {
      Object.keys(extra).forEach(function (k) { headers[k] = extra[k]; });
    }
    return headers;
  }

  function postJson(url, body, signal) {
    return fetch(url, {
      method: "POST",
      headers: csrfHeaders({
        "Content-Type": "application/json",
        "Accept": "application/json",
      }),
      credentials: "same-origin",
      body: body == null ? "{}" : JSON.stringify(body),
      signal: signal,
    });
  }

  function deleteJson(url) {
    return fetch(url, {
      method: "DELETE",
      headers: csrfHeaders({ "Accept": "application/json" }),
      credentials: "same-origin",
    });
  }

  function getJson(url, signal) {
    return fetch(url, { credentials: "same-origin", signal: signal });
  }

  // ---- Metadata rendering --------------------------------------------------------

  function formatDuration(sec) {
    if (!sec || sec <= 0) return "";
    var m = Math.floor(sec / 60);
    var s = Math.floor(sec % 60);
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  function syncReadyMeta() {
    if (guidLabel) guidLabel.textContent = state.guid ? state.guid.slice(0, 8) + "…" : "—";
    if (durationContainer && durationValue) {
      var formatted = formatDuration(state.durationSec);
      if (formatted) {
        durationValue.textContent = formatted;
        durationContainer.hidden = false;
        durationContainer.style.display = "";
      } else {
        durationContainer.hidden = true;
        durationContainer.style.display = "none";
      }
    }
    // Keep the thumbnail tile in sync with state. The img has a cache-busting
    // query so a same-URL refresh (after a custom thumbnail upload) still
    // re-paints in the browser.
    if (thumbnailImg) {
      if (state.thumbnailUrl) {
        var bust = (state.thumbnailUrl.indexOf("?") === -1 ? "?" : "&") + "v=" + (state.durationSec || 0);
        thumbnailImg.src = state.thumbnailUrl + bust;
        thumbnailImg.style.display = "";
      } else {
        thumbnailImg.removeAttribute("src");
        thumbnailImg.style.display = "none";
      }
    }
  }

  // ---- Upload flow ---------------------------------------------------------------

  function startUpload(file) {
    if (!file) return;
    showError("");
    if (filenameLabel) filenameLabel.textContent = file.name || "video";
    if (progressBar) progressBar.value = 0;
    if (progressPct) progressPct.textContent = "0%";
    show("uploading");

    postJson(endpoints.uploadToken, { title: file.name || state.title || "Untitled video" })
      .then(function (res) {
        if (!res.ok) {
          return res.json().catch(function () { return {}; }).then(function (body) {
            // DRF emits `detail`; our endpoints emit `error`. Show whichever
            // we can find, with HTTP status as a last-resort hint.
            var msg = (body && (body.error || body.detail)) || ("Upload token failed (HTTP " + res.status + ")");
            throw new Error(msg);
          });
        }
        return res.json();
      })
      .then(function (token) {
        var upload = new tus.Upload(file, {
          endpoint: endpoints.tusEndpoint,
          retryDelays: [0, 1000, 3000, 5000, 10000],
          headers: {
            AuthorizationSignature: token.signature,
            AuthorizationExpire: String(token.expires),
            VideoId: token.guid,
            LibraryId: token.library_id,
          },
          metadata: { filetype: file.type, title: file.name },
          onError: function (err) {
            console.error("[bunny:author] tus upload error", err);
            show("empty");
            showError("Upload failed. Check your connection and try again.");
          },
          onProgress: function (bytesUploaded, bytesTotal) {
            var pct = bytesTotal > 0 ? Math.round((bytesUploaded / bytesTotal) * 100) : 0;
            if (progressBar) progressBar.value = pct;
            if (progressPct) progressPct.textContent = pct + "%";
          },
          onSuccess: function () {
            finalizeUpload(token.guid, token.library_id);
          },
        });
        upload.start();
        state.currentUpload = upload;
      })
      .catch(function (err) {
        console.error("[bunny:author] upload-token failure", err);
        show("empty");
        showError(err.message || "Could not start upload.");
      });
  }

  function finalizeUpload(guid, libraryId) {
    state.guid = guid;
    state.libraryId = libraryId;
    state.currentUpload = null;
    syncReadyMeta();
    show("processing");

    postJson(endpoints.finalize.replace("{guid}", encodeURIComponent(guid)))
      .then(function (res) {
        return res.ok ? res.json() : Promise.reject(new Error("Finalize failed"));
      })
      .then(function (meta) {
        state.title = meta.title || state.title;
        state.status = meta.status || "encoding";
        state.durationSec = meta.duration_sec || 0;
        if (meta.thumbnail_url) state.thumbnailUrl = meta.thumbnail_url;
        setTitleInputs(state.title);
        syncReadyMeta();
        return postJson(setVideoUrl, {
          guid: meta.guid,
          library_id: meta.library_id,
          title: meta.title,
          duration_sec: meta.duration_sec || 0,
          thumbnail_url: meta.thumbnail_url || "",
          status: meta.status || "encoding",
        });
      })
      .then(function () {
        if (state.status === "ready") flipToReady();
        else show("processing");
      })
      .catch(function (err) {
        console.error("[bunny:author] finalize chain failed", err);
        showError("Upload finished but couldn't reconcile the metadata.");
        show("empty");
      });
  }

  function cancelUpload() {
    if (state.currentUpload) {
      try { state.currentUpload.abort(true); } catch (e) { /* ignore */ }
      state.currentUpload = null;
    }
    show("empty");
  }

  // ---- Polling -------------------------------------------------------------------

  // ---- Elapsed-time counter (visible while processing) ---------------------------

  var elapsedInterval = null;
  var elapsedStartedAt = null;

  function fmtElapsed(seconds) {
    var m = Math.floor(seconds / 60);
    var s = Math.floor(seconds % 60);
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  function startElapsedTimer() {
    if (elapsedInterval) return; // already running across panel re-shows
    elapsedStartedAt = Date.now();
    if (elapsedContainer) {
      elapsedContainer.hidden = false;
      elapsedContainer.style.display = "";
    }
    if (elapsedValue) elapsedValue.textContent = "0:00";
    elapsedInterval = setInterval(function () {
      if (!elapsedValue) return;
      var sec = Math.floor((Date.now() - elapsedStartedAt) / 1000);
      elapsedValue.textContent = fmtElapsed(sec);
    }, 1000);
  }

  function stopElapsedTimer() {
    if (elapsedInterval) {
      clearInterval(elapsedInterval);
      elapsedInterval = null;
    }
    elapsedStartedAt = null;
    if (elapsedContainer) {
      elapsedContainer.hidden = true;
      elapsedContainer.style.display = "none";
    }
  }

  function startPolling() {
    startElapsedTimer();
    if (state.pollAbort) state.pollAbort.abort();
    state.pollAbort = new AbortController();
    var signal = state.pollAbort.signal;
    var INTERVAL_MS = 5000;
    var TIMEOUT_MS = 30 * 60 * 1000;
    var started = Date.now();

    function tick() {
      if (signal.aborted) return;
      if (Date.now() - started > TIMEOUT_MS) return;
      getJson(endpoints.videoDetail.replace("{guid}", encodeURIComponent(state.guid)), signal)
        .then(function (res) { return res.ok ? res.json() : Promise.reject(new Error("video detail " + res.status)); })
        .then(function (meta) {
          if (signal.aborted) return;
          if (typeof meta.duration_sec === "number" && meta.duration_sec > 0) {
            state.durationSec = meta.duration_sec;
          }
          if (meta.thumbnail_url) state.thumbnailUrl = meta.thumbnail_url;
          if (meta.status !== state.status) {
            state.status = meta.status;
            postJson(updateStatusUrl, {
              status: meta.status,
              duration_sec: meta.duration_sec || 0,
              thumbnail_url: meta.thumbnail_url || "",
            }).catch(function () { /* fire and forget */ });
          }
          if (meta.status === "ready") flipToReady();
          else if (meta.status === "failed") flipToFailed();
          else setTimeout(tick, INTERVAL_MS);
        })
        .catch(function (err) {
          if (signal.aborted) return;
          console.warn("[bunny:author] poll error", err);
          setTimeout(tick, INTERVAL_MS);
        });
    }
    tick();
  }

  // ---- State flips ---------------------------------------------------------------

  function flipToReady() {
    syncReadyMeta();
    // If the iframe was already server-rendered with a signed URL, reuse it.
    var iframe = panels.ready && panels.ready.querySelector(".bunny-xblock__iframe");
    var existingSrc = iframe ? iframe.getAttribute("src") : "";
    if (existingSrc && existingSrc.indexOf("token=") !== -1) {
      show("ready");
      return;
    }
    // Otherwise fetch a fresh signed URL.
    var url = endpoints.embedUrl + "?guid=" + encodeURIComponent(state.guid);
    getJson(url)
      .then(function (res) { return res.ok ? res.json() : Promise.reject(new Error("embed-url " + res.status)); })
      .then(function (data) {
        if (iframe && data.url) {
          iframe.src = data.url;
          iframe.setAttribute("title", state.title || "Bunny video");
        }
        show("ready");
      })
      .catch(function (err) {
        console.error("[bunny:author] embed-url fetch failed", err);
        show("ready");
      });
  }

  function flipToFailed() {
    show("failed");
  }

  // ---- Confirm modal (replaces window.confirm) ----------------------------------

  // Remember whatever was focused before the modal opened so we can restore
  // it on close — standard a11y dialog hygiene.
  var modalReturnFocus = null;

  function getModalFocusables() {
    if (!modal) return [];
    return Array.prototype.slice.call(
      modal.querySelectorAll('button:not([disabled]), [tabindex]:not([tabindex="-1"])')
    );
  }

  function trapModalTab(e) {
    if (e.key !== "Tab") return;
    var nodes = getModalFocusables();
    if (!nodes.length) return;
    var first = nodes[0];
    var last = nodes[nodes.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }

  function openModal() {
    if (!modal) return;
    modalReturnFocus = document.activeElement;
    modal.setAttribute("data-open", "true");
    var confirmBtn = modal.querySelector('[data-action="modal-confirm"]');
    if (confirmBtn) confirmBtn.focus();
    document.addEventListener("keydown", trapModalTab);
  }

  function closeModal() {
    if (!modal) return;
    modal.removeAttribute("data-open");
    document.removeEventListener("keydown", trapModalTab);
    if (modalReturnFocus && typeof modalReturnFocus.focus === "function") {
      modalReturnFocus.focus();
    }
    modalReturnFocus = null;
  }

  // ---- Replace / delete ---------------------------------------------------------

  function replaceVideo() {
    state.guid = "";
    state.libraryId = "";
    state.status = "";
    state.title = "";
    state.durationSec = 0;
    state.thumbnailUrl = "";
    setTitleInputs("");
    syncReadyMeta();
    postJson(clearVideoUrl).catch(function () { /* ignore */ });
    show("empty");
  }

  // ---- Thumbnail replace -------------------------------------------------------

  function setThumbnailStatus(message, state) {
    if (!thumbnailStatus) return;
    if (message) {
      thumbnailStatus.textContent = message;
      thumbnailStatus.hidden = false;
      thumbnailStatus.style.display = "";
      thumbnailStatus.setAttribute("data-state", state || "info");
    } else {
      thumbnailStatus.textContent = "";
      thumbnailStatus.hidden = true;
      thumbnailStatus.style.display = "none";
      thumbnailStatus.removeAttribute("data-state");
    }
  }

  function uploadThumbnail(file) {
    if (!file || !state.guid) return;
    if (!/^image\/(jpeg|png|webp)$/.test(file.type)) {
      setThumbnailStatus("Use a JPG, PNG, or WebP image.", "error");
      return;
    }
    setThumbnailStatus("Uploading…", "info");
    var url = endpoints.thumbnail.replace("{guid}", encodeURIComponent(state.guid));
    var form = new FormData();
    form.append("thumbnail", file);
    fetch(url, {
      method: "POST",
      headers: csrfHeaders({ "Accept": "application/json" }),
      credentials: "same-origin",
      body: form,
    })
      .then(function (res) {
        if (!res.ok) {
          return res.json().catch(function () { return {}; }).then(function (body) {
            throw new Error((body && (body.error || body.detail)) || ("Thumbnail upload failed (HTTP " + res.status + ")"));
          });
        }
        return res.json();
      })
      .then(function (data) {
        if (data.thumbnail_url) {
          state.thumbnailUrl = data.thumbnail_url;
          // Cache-bust so Bunny serving the new image at the same URL is
          // re-fetched by the browser instead of pulled from cache.
          if (thumbnailImg) {
            var bust = (data.thumbnail_url.indexOf("?") === -1 ? "?" : "&") + "t=" + Date.now();
            thumbnailImg.src = data.thumbnail_url + bust;
            thumbnailImg.style.display = "";
          }
          postJson(updateThumbnailUrl, { thumbnail_url: data.thumbnail_url }).catch(function () { /* fire and forget */ });
        }
        setThumbnailStatus("Updated", "success");
        setTimeout(function () { setThumbnailStatus("", null); }, 2500);
      })
      .catch(function (err) {
        console.error("[bunny:author] thumbnail upload failed", err);
        setThumbnailStatus(err.message || "Upload failed", "error");
      });
  }

  function deleteVideo() {
    if (!state.guid) return;
    closeModal();
    var url = endpoints.videoDetail.replace("{guid}", encodeURIComponent(state.guid));
    deleteJson(url)
      .then(function (res) {
        if (!res.ok && res.status !== 404) throw new Error("delete " + res.status);
        return postJson(clearVideoUrl);
      })
      .then(function () {
        state.guid = "";
        state.libraryId = "";
        state.status = "";
        state.title = "";
        state.durationSec = 0;
        setTitleInputs("");
        syncReadyMeta();
        show("empty");
      })
      .catch(function (err) {
        console.error("[bunny:author] delete failed", err);
        showError("Couldn't delete from Bunny. Check the console for details.");
      });
  }

  // ---- Title sync ---------------------------------------------------------------

  function setTitleInputs(value) {
    titleInputs.forEach(function (input) { input.value = value || ""; });
  }

  function commitTitle(newTitle) {
    if (newTitle === state.title) return;
    state.title = newTitle;
    setTitleInputs(newTitle);
    postJson(updateTitleUrl, { title: newTitle }).catch(function () { /* ignore */ });
  }

  function onTitleBlur(e) {
    var newTitle = (e.target.value || "").trim().slice(0, 250);
    commitTitle(newTitle);
  }

  // Debounced commit on input so authors who don't blur before Save don't
  // lose their title. 600 ms is long enough that we're not chattering on
  // every keystroke, short enough that Save catches the latest value.
  var titleDebounce = null;
  function onTitleInput(e) {
    var value = (e.target.value || "").slice(0, 250);
    setTitleInputs(value);
    if (titleDebounce) clearTimeout(titleDebounce);
    titleDebounce = setTimeout(function () {
      commitTitle(value.trim());
    }, 600);
  }

  // ---- Event wiring -------------------------------------------------------------

  root.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-action]");
    if (!btn) return;
    var action = btn.getAttribute("data-action");
    switch (action) {
      case "choose": fileInput && fileInput.click(); break;
      case "cancel-upload": cancelUpload(); break;
      case "replace": replaceVideo(); break;
      case "delete": openModal(); break;
      case "modal-cancel": closeModal(); break;
      case "modal-confirm": deleteVideo(); break;
      case "upload-thumbnail": thumbnailFile && thumbnailFile.click(); break;
    }
  });

  if (thumbnailFile) {
    thumbnailFile.addEventListener("change", function (e) {
      var file = e.target.files && e.target.files[0];
      if (file) uploadThumbnail(file);
      e.target.value = "";
    });
  }

  // Modal: close on backdrop click + Escape.
  if (modal) {
    modal.addEventListener("click", function (e) {
      if (e.target === modal) closeModal();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && modal.getAttribute("data-open") === "true") closeModal();
    });
  }

  if (fileInput) {
    fileInput.addEventListener("change", function (e) {
      var file = e.target.files && e.target.files[0];
      if (file) startUpload(file);
      e.target.value = "";
    });
  }

  if (dropzone) {
    // Mouse-driven drag + drop.
    ["dragover", "dragenter"].forEach(function (ev) {
      dropzone.addEventListener(ev, function (e) {
        e.preventDefault();
        dropzone.classList.add("is-dragover");
      });
    });
    ["dragleave", "dragend", "drop"].forEach(function (ev) {
      dropzone.addEventListener(ev, function (e) {
        e.preventDefault();
        dropzone.classList.remove("is-dragover");
      });
    });
    dropzone.addEventListener("drop", function (e) {
      var file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (file) startUpload(file);
    });
    // Keyboard parity: Enter/Space on the dropzone opens the file picker so
    // it isn't purely a mouse interaction.
    dropzone.addEventListener("keydown", function (e) {
      if ((e.key === "Enter" || e.key === " ") && fileInput) {
        e.preventDefault();
        fileInput.click();
      }
    });
  }

  titleInputs.forEach(function (input) {
    input.addEventListener("input", onTitleInput);
    input.addEventListener("blur", onTitleBlur);
  });

  // ---- Initial render -----------------------------------------------------------

  syncReadyMeta();

  if (state.guid && state.status && state.status !== "ready" && state.status !== "failed") {
    show("processing");
  } else if (state.status === "failed") {
    show("failed");
  } else if (state.guid && state.libraryId) {
    show("ready");
  } else {
    show("empty");
  }
}
