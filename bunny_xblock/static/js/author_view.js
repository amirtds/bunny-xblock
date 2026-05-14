/**
 * BunnyAuthorView — Studio-side inline authoring UI.
 *
 * State machine driven by the XBlock's `status` field (server-rendered) plus
 * JS-driven transitions for upload progress. Closely mirrors the Cubite
 * EditorJS plugin in `app/plugins/bunny-stream/index.js`, minus the library
 * picker (v0.1 ships single-video-per-block per the user spec).
 *
 *   empty  ──drop/click──▶  uploading ──TUS success──▶  processing
 *                                                    │
 *                                                    ▼
 *                                       (poll until status changes)
 *                                                    │
 *                                                    ▼
 *                                                  ready  ◀── webhook
 *                                                    │
 *                                       failed (rare; from poll/webhook)
 *
 * Tus.Upload is loaded from the bundled `vendor/tus.min.js` so we don't depend
 * on any CDN at run time. The package's vendor script downloads it at build.
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

  // ---- State ----------------------------------------------------------------------

  var state = {
    guid: config.guid || "",
    libraryId: config.libraryId || "",
    title: config.title || "",
    status: config.status || (config.guid ? "encoding" : ""),
    currentUpload: null, // active tus.Upload instance
    pollAbort: null, // AbortController for current poll loop
  };

  var endpoints = config.endpoints || {};
  var setVideoUrl = runtime.handlerUrl(element, "set_video");
  var updateStatusUrl = runtime.handlerUrl(element, "update_status");
  var updateTitleUrl = runtime.handlerUrl(element, "update_title");
  var clearVideoUrl = runtime.handlerUrl(element, "clear_video");

  // ---- Panel switching ------------------------------------------------------------

  function show(panel) {
    Object.keys(panels).forEach(function (key) {
      if (panels[key]) panels[key].hidden = key !== panel;
    });
    root.setAttribute("data-state", panel);
    if (panel === "processing" && state.guid) startPolling();
    if (panel !== "processing" && state.pollAbort) {
      state.pollAbort.abort();
      state.pollAbort = null;
    }
  }

  function showError(msg) {
    if (!errorBox) return;
    errorBox.textContent = msg;
    errorBox.hidden = !msg;
  }

  // ---- Networking helpers ---------------------------------------------------------

  function postJson(url, body, signal) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      credentials: "same-origin",
      body: body == null ? "{}" : JSON.stringify(body),
      signal: signal,
    });
  }

  function getJson(url, signal) {
    return fetch(url, { credentials: "same-origin", signal: signal });
  }

  // ---- Upload flow ----------------------------------------------------------------

  function startUpload(file) {
    if (!file) return;
    showError("");
    filenameLabel.textContent = file.name || "video";
    progressBar.value = 0;
    progressPct.textContent = "0%";
    show("uploading");

    postJson(endpoints.uploadToken, { title: file.name || state.title || "Untitled video" })
      .then(function (res) {
        if (!res.ok) {
          return res.json().then(function (body) {
            throw new Error((body && body.error) || "Upload token failed");
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
            progressBar.value = pct;
            progressPct.textContent = pct + "%";
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
    show("processing");

    postJson(endpoints.finalize.replace("{guid}", encodeURIComponent(guid)))
      .then(function (res) {
        return res.ok ? res.json() : Promise.reject(new Error("Finalize failed"));
      })
      .then(function (meta) {
        state.title = meta.title || state.title;
        state.status = meta.status || "encoding";
        setTitleInputs(state.title);
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
        // If Bunny is already done by the time we finalize, jump straight to ready.
        if (state.status === "ready") flipToReady();
        else show("processing");
      })
      .catch(function (err) {
        console.error("[bunny:author] finalize chain failed", err);
        showError("Upload finished but Cubite couldn't reconcile the metadata.");
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

  // ---- Polling --------------------------------------------------------------------

  function startPolling() {
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
          // Sync any change back to the XBlock so a Studio reload reflects it.
          if (meta.status !== state.status) {
            state.status = meta.status;
            postJson(updateStatusUrl, {
              status: meta.status,
              duration_sec: meta.duration_sec || 0,
              thumbnail_url: meta.thumbnail_url || "",
            }).catch(function () { /* fire and forget */ });
          }
          if (meta.status === "ready") {
            flipToReady();
          } else if (meta.status === "failed") {
            flipToFailed();
          } else {
            setTimeout(tick, INTERVAL_MS);
          }
        })
        .catch(function (err) {
          if (signal.aborted) return;
          console.warn("[bunny:author] poll error", err);
          setTimeout(tick, INTERVAL_MS);
        });
    }
    tick();
  }

  // ---- State flips ----------------------------------------------------------------

  function flipToReady() {
    // Need a fresh signed iframe URL. The server-rendered one is only present
    // when the block initially loaded in `ready` state.
    var url = endpoints.embedUrl + "?guid=" + encodeURIComponent(state.guid);
    getJson(url)
      .then(function (res) { return res.ok ? res.json() : Promise.reject(new Error("embed-url " + res.status)); })
      .then(function (data) {
        var iframe = panels.ready.querySelector(".bunny-xblock__iframe");
        if (iframe && data.url) {
          iframe.src = data.url;
          iframe.setAttribute("title", state.title || "Bunny video");
        }
        show("ready");
      })
      .catch(function (err) {
        console.error("[bunny:author] embed-url fetch failed", err);
        show("ready"); // still show the panel; iframe stays blank
      });
  }

  function flipToFailed() {
    show("failed");
  }

  // ---- Replace / delete -----------------------------------------------------------

  function replaceVideo() {
    // Don't delete from Bunny — author may want to keep the original. Just
    // detach this block from it. The detached video remains in the library.
    state.guid = "";
    state.libraryId = "";
    state.status = "";
    state.title = "";
    setTitleInputs("");
    postJson(clearVideoUrl).catch(function () { /* ignore */ });
    show("empty");
  }

  function deleteVideo() {
    if (!state.guid) return;
    if (!window.confirm("Delete this video from Bunny permanently? This cannot be undone.")) return;
    var url = endpoints.videoDetail.replace("{guid}", encodeURIComponent(state.guid));
    fetch(url, { method: "DELETE", credentials: "same-origin" })
      .then(function (res) {
        if (!res.ok && res.status !== 404) {
          throw new Error("delete " + res.status);
        }
        return postJson(clearVideoUrl);
      })
      .then(function () {
        state.guid = "";
        state.libraryId = "";
        state.status = "";
        state.title = "";
        setTitleInputs("");
        show("empty");
      })
      .catch(function (err) {
        console.error("[bunny:author] delete failed", err);
        showError("Couldn't delete from Bunny. Check the console for details.");
      });
  }

  // ---- Title sync -----------------------------------------------------------------

  function setTitleInputs(value) {
    titleInputs.forEach(function (input) { input.value = value || ""; });
  }

  function onTitleBlur(e) {
    var newTitle = (e.target.value || "").trim().slice(0, 250);
    if (newTitle === state.title) return;
    state.title = newTitle;
    setTitleInputs(newTitle);
    postJson(updateTitleUrl, { title: newTitle }).catch(function () { /* ignore */ });
  }

  // ---- Event wiring ---------------------------------------------------------------

  root.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-action]");
    if (!btn) return;
    var action = btn.getAttribute("data-action");
    if (action === "choose") fileInput.click();
    else if (action === "cancel-upload") cancelUpload();
    else if (action === "replace") replaceVideo();
    else if (action === "delete") deleteVideo();
  });

  if (fileInput) {
    fileInput.addEventListener("change", function (e) {
      var file = e.target.files && e.target.files[0];
      if (file) startUpload(file);
      e.target.value = ""; // reset so the same file can be reselected later
    });
  }

  if (dropzone) {
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
  }

  titleInputs.forEach(function (input) {
    input.addEventListener("blur", onTitleBlur);
  });

  // ---- Initial state --------------------------------------------------------------

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
