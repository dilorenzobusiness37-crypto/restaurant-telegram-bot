// Dashboard feedback: toast messages, cancel with undo, "just saved" highlight.
// Everything here is an enhancement: without JavaScript the plain links and forms still work.
(function () {
  "use strict";

  var UNDO_MS = 2000;            // how long "Annulla" is offered before the cancellation is sent
  var HIGHLIGHT_MS = 2500;
  var meta = document.querySelector('meta[name="csrf-token"]');
  var csrf = meta ? meta.content : "";
  var toast = document.getElementById("toast");
  var toastTimer;

  function showToast(message, kind) {
    if (!toast) return;
    toast.textContent = message;
    toast.className = "toast toast-" + (kind || "ok");
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toast.hidden = true; }, 5000);
  }
  window.dashboardToast = showToast;

  function clearHighlights(root) {
    (root || document).querySelectorAll(".just-saved").forEach(function (el) {
      setTimeout(function () { el.classList.remove("just-saved"); }, HIGHLIGHT_MS);
    });
  }
  clearHighlights();

  function formBody() {
    var body = new FormData();
    body.append("csrf", csrf);
    return body;
  }

  // Re-render the bookings board in place, so totals and room load stay true.
  function refreshBoard() {
    return fetch(location.href, { credentials: "same-origin" })
      .then(function (r) { return r.text(); })
      .then(function (html) {
        var fresh = new DOMParser().parseFromString(html, "text/html").getElementById("board");
        var board = document.getElementById("board");
        if (fresh && board) board.replaceWith(fresh);
      });
  }

  // --- Cancel with undo ---------------------------------------------------
  var pending = new Map(); // url -> true while waiting for the undo window to close

  document.addEventListener("click", function (event) {
    var link = event.target.closest("a[data-cancel-url]");
    if (!link) return;
    event.preventDefault();

    var url = link.dataset.cancelUrl;
    if (pending.has(url)) return;
    var name = link.dataset.name;
    var row = link.closest(".arrival");
    var actions = link.parentNode;
    var original = actions.innerHTML;

    row.classList.add("is-pending");
    actions.innerHTML =
      '<span class="pending-label">Cancellata</span>' +
      '<button type="button" class="undo">Annulla<span class="visually-hidden"> la cancellazione di ' +
      name.replace(/[<>&"]/g, "") + "</span></button>";
    var undo = actions.querySelector(".undo");
    undo.focus();
    pending.set(url, true);
    var timer = setTimeout(commit, UNDO_MS);

    undo.addEventListener("click", function () {
      clearTimeout(timer);
      pending.delete(url);
      row.classList.remove("is-pending");
      actions.innerHTML = original;
      var again = actions.querySelector("a");
      if (again) again.focus();
      showToast("La prenotazione di " + name + " resta valida.", "ok");
    });

    function commit() {
      pending.delete(url);
      row.classList.add("is-leaving");
      fetch(url, {
        method: "POST",
        body: formBody(),
        credentials: "same-origin",
        headers: { "X-Requested-With": "fetch" }
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          showToast(data.message, data.kind || (data.ok ? "ok" : "error"));
          return refreshBoard();
        })
        .catch(function () {
          row.classList.remove("is-pending", "is-leaving");
          actions.innerHTML = original;
          showToast("Non sono riuscito a cancellare: controlla la connessione e riprova.", "error");
        });
    }
  });

  // Leaving the page during the undo window still cancels, as the owner asked.
  window.addEventListener("pagehide", function () {
    pending.forEach(function (_, url) { navigator.sendBeacon(url, formBody()); });
    pending.clear();
  });
})();
