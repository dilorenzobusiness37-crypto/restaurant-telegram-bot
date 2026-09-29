// Menu editor: unsaved-changes state, spreadsheet-like Enter key,
// live "come lo vede il cliente" preview, and a green flash on rows just saved.
(function () {
  "use strict";

  var form = document.getElementById("menu-form");
  if (!form) return;
  var script = document.currentScript;
  var state = document.getElementById("save-state");
  var discard = document.getElementById("discard");
  var preview = document.getElementById("preview");
  var dirty = !!(script && script.dataset.dirty === "true");
  var SAVED_KEY = "menu-saved-rows";

  function markDirty() {
    dirty = true;
    state.textContent = "Modifiche non salvate";
    state.classList.add("is-dirty");
    discard.hidden = false;
  }

  form.addEventListener("input", function () { markDirty(); renderPreview(); });
  form.addEventListener("change", function () { markDirty(); renderPreview(); });
  discard.addEventListener("click", function () { dirty = false; });
  window.addEventListener("beforeunload", function (e) {
    if (dirty) { e.preventDefault(); e.returnValue = ""; }
  });

  // Enter moves to the next cell instead of submitting half-typed changes.
  form.addEventListener("keydown", function (e) {
    if (e.key !== "Enter" || e.target.tagName !== "INPUT") return;
    e.preventDefault();
    var fields = Array.prototype.filter.call(
      form.querySelectorAll("input[type=text], input[type=number]"),
      function (el) { return el.offsetParent !== null; }
    );
    var next = fields[fields.indexOf(e.target) + 1];
    if (next) { next.focus(); next.select(); }
  });

  // --- Remember which rows changed, to flash them after the save -----------
  form.addEventListener("submit", function () {
    dirty = false;
    var changed = [];
    form.querySelectorAll("[data-row]").forEach(function (row) {
      var modified = Array.prototype.some.call(row.querySelectorAll("input[type=text]"), function (i) {
        return i.value !== i.defaultValue;
      });
      var del = row.querySelector('[data-role="delete"]');
      var name = row.querySelector('[data-role="name"]').value.trim();
      if (modified && name && !(del && del.checked)) changed.push(name);
    });
    try { sessionStorage.setItem(SAVED_KEY, JSON.stringify(changed)); } catch (err) { /* private mode */ }
  });

  (function flashSavedRows() {
    var names = [];
    try {
      names = JSON.parse(sessionStorage.getItem(SAVED_KEY) || "[]");
      if (!document.querySelector(".notice-error")) sessionStorage.removeItem(SAVED_KEY);
    } catch (err) { return; }
    if (!names.length || !document.querySelector(".notice-ok")) return;
    form.querySelectorAll("[data-row]").forEach(function (row) {
      if (names.indexOf(row.querySelector('[data-role="name"]').value.trim()) !== -1) {
        row.classList.add("just-saved");
        setTimeout(function () { row.classList.remove("just-saved"); }, 2500);
      }
    });
  })();

  // --- Live preview of the bot's menu message -----------------------------
  function esc(text) {
    return text.replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function formatPrice(raw) {
    var cleaned = raw.replace(/[€\s]/g, "").replace(",", ".");
    if (!/^\d+(\.\d{1,2})?$/.test(cleaned)) return null;
    return "€ " + Number(cleaned).toFixed(2).replace(".", ",");
  }

  function value(root, role) {
    var el = root.querySelector('[data-role="' + role + '"]');
    return el ? el.value.trim() : "";
  }

  function renderPreview() {
    if (!preview) return;
    // Same layout as the bot's /menu answer (see show_menu in bot.py).
    var html = "<p><b>📖 Menù – " + esc(preview.dataset.restaurant || "") + "</b></p>";
    form.querySelectorAll("[data-sheet]").forEach(function (sheet) {
      var delCat = sheet.querySelector('[data-role="delete-category"]');
      if (delCat && delCat.checked) return;
      var items = [];
      sheet.querySelectorAll("[data-row]").forEach(function (row) {
        var del = row.querySelector('[data-role="delete"]');
        var name = value(row, "name");
        if ((del && del.checked) || !name) return;
        var price = formatPrice(value(row, "price"));
        var desc = value(row, "desc");
        items.push(
          '<p class="pv-item">• ' + esc(name) + " — <b>" +
          (price || '<span class="pv-missing">prezzo mancante</span>') + "</b>" +
          (desc ? '<i class="pv-desc">' + esc(desc) + "</i>" : "") + "</p>"
        );
      });
      var category = value(sheet, "category");
      if (!category && !items.length) return;
      html += '<p class="pv-cat">' + esc(value(sheet, "emoji")) + " <b>" +
        esc((category || "Senza nome").toUpperCase()) + "</b></p>" + items.join("");
    });
    html += '<p class="pv-foot"><i>Per allergeni e intolleranze chiedi al nostro personale.</i></p>';
    preview.innerHTML = html;
  }

  if (dirty) { state.classList.add("is-dirty"); }
  renderPreview();
})();
