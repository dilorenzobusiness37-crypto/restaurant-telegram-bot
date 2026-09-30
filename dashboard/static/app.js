// Dashboard feedback: toast messages, cancel with undo, "just saved" highlight,
// and live updates of the bookings board (bookings made or cancelled in the bot).
// Everything here is an enhancement: without JavaScript the plain links and forms still work.
(function () {
  "use strict";

  var UNDO_MS = 2000;            // how long "Annulla" is offered before the cancellation is sent
  var HIGHLIGHT_MS = 2500;
  var POLL_MS = 5000;            // how often the home page asks whether something changed
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

  // --- Redraw the board in place --------------------------------------------
  // Keeps open day panels and keyboard focus, so a refresh never "jumps" under the owner's hands.

  function focusSelector(el) {
    if (!el || el === document.body) return null;
    if (el.dataset && el.dataset.cancelUrl) return 'a[data-cancel-url="' + el.dataset.cancelUrl + '"]';
    var details = el.closest("details[data-key]");
    if (el.tagName === "SUMMARY" && details) return 'details[data-key="' + details.dataset.key + '"] > summary';
    if (el.id) return "#" + el.id;
    return null;
  }

  function refreshBoard() {
    return fetch(location.href, { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok || r.redirected) throw new Error("page " + r.status);
        return r.text();
      })
      .then(function (html) {
        var board = document.getElementById("board");
        var fresh = new DOMParser().parseFromString(html, "text/html").getElementById("board");
        if (!fresh || !board) return null;
        var open = Array.prototype.map.call(board.querySelectorAll("details[open][data-key]"), function (d) {
          return d.dataset.key;
        });
        var focused = board.contains(document.activeElement) ? focusSelector(document.activeElement) : null;

        open.forEach(function (key) {
          var d = fresh.querySelector('details[data-key="' + key + '"]');
          if (d) d.open = true;
        });
        board.replaceWith(fresh);
        if (focused) {
          var target = fresh.querySelector(focused);
          if (target) target.focus({ preventScroll: true });
        }
        clearHighlights(fresh);
        return fresh;
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
          return refreshBoard().then(function (fresh) { if (fresh) live.adopt(fresh); });
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

  // --- Live updates -------------------------------------------------------
  // Every few seconds ask the server for a short version string. Only when it changes
  // is the board redrawn; new bookings flash and get a small notice.

  var live = (function () {
    var board = document.getElementById("board");
    var stateUrl = board && board.dataset.stateUrl;
    var version = board && board.dataset.version;
    var known = {};        // booking id -> status, as currently shown
    var timer = null;
    var busy = false;
    var stopped = !stateUrl;

    function readKnown(root) {
      known = {};
      root.querySelectorAll("[data-booking]").forEach(function (el) {
        // A cancelled row wins over a confirmed one for the same id (never both, but be safe).
        if (known[el.dataset.booking] !== "cancelled") known[el.dataset.booking] = el.dataset.status;
      });
    }
    if (board) readKnown(board);

    function adopt(fresh) {
      version = fresh.dataset.version;
      readKnown(fresh);
    }

    function when(b) {
      var today = new Date();
      var iso = today.getFullYear() + "-" + String(today.getMonth() + 1).padStart(2, "0") + "-" + String(today.getDate()).padStart(2, "0");
      if (b.date === iso) return b.time;
      var d = new Date(b.date + "T12:00:00");
      var day = d.toLocaleDateString("it-IT", { weekday: "short", day: "numeric", month: "short" });
      return day + " alle " + b.time;
    }

    function people(n) { return n + (n === 1 ? " persona" : " persone"); }

    function announce(state) {
      var added = state.bookings.filter(function (b) { return b.status === "confirmed" && !(String(b.id) in known); });
      var cancelled = state.bookings.filter(function (b) {
        return b.status === "cancelled" && b.cancelled_by === "customer" && known[String(b.id)] === "confirmed";
      });
      if (added.length === 1) {
        var b = added[0];
        showToast("Nuova prenotazione: " + b.name + ", " + when(b) + ", " + people(b.people), "ok");
      } else if (added.length > 1) {
        showToast(added.length + " nuove prenotazioni: " + added.map(function (b) { return b.name; }).join(", "), "ok");
      } else if (cancelled.length) {
        var c = cancelled[0];
        showToast("Cancellata dal cliente: " + c.name + ", " + when(c) + ", " + people(c.people), "warn");
      }
      return added;
    }

    function highlight(fresh, added) {
      added.forEach(function (b) {
        fresh.querySelectorAll('[data-booking="' + b.id + '"]').forEach(function (el) {
          el.classList.add("just-saved");
          // A booking on another day sits in a closed panel: flash the day instead.
          var panel = el.closest("details:not([open])");
          if (panel) panel.querySelector("summary").classList.add("just-saved");
        });
      });
      clearHighlights(fresh);
    }

    function poll() {
      timer = null;
      if (stopped) return;
      // Don't redraw under the owner's hands: wait while an undo is pending or the tab is hidden.
      if (busy || pending.size || document.hidden) { schedule(); return; }
      busy = true;
      fetch(stateUrl, { credentials: "same-origin", headers: { "X-Requested-With": "fetch" }, cache: "no-store" })
        .then(function (r) {
          if (r.status === 401) {
            stopped = true;
            showToast("Sessione scaduta: ricarica la pagina per rientrare.", "warn");
            return null;
          }
          return r.ok ? r.json() : null;
        })
        .then(function (state) {
          if (!state || state.version === version) return null;
          var added = announce(state);
          return refreshBoard().then(function (fresh) {
            if (!fresh) return;
            adopt(fresh);
            highlight(fresh, added);
          });
        })
        .catch(function () { /* offline for a moment: try again at the next tick */ })
        .then(function () { busy = false; schedule(); });
    }

    function schedule() {
      if (!stopped && !timer) timer = setTimeout(poll, POLL_MS);
    }

    // Back on the tab: check at once instead of waiting for the next tick.
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && !stopped) {
        clearTimeout(timer);
        timer = null;
        poll();
      }
    });

    schedule();
    return { adopt: adopt, poll: poll };
  })();
  window.dashboardLive = live;
})();
