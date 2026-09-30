/* Client calendar — layout switch, the day sheet, copying the share link.

   Progressive enhancement throughout. Without this file the page still works:
   both layouts show, every busy day in the grid is a link to that day in the
   list, and nothing is hidden behind a click. This adds the sheet that slides
   in when a date is tapped, remembers grid-or-list, and makes the sheet behave
   like a proper dialog — focus goes in, Escape and swipe-down close it, arrow
   keys step between busy days, and focus comes back to the date you opened.
*/
(function () {
  "use strict";

  // ---- copy the share link (planner only; outside the calendar root) ----
  var copyButton = document.querySelector("[data-cc-copy]");
  if (copyButton) {
    copyButton.addEventListener("click", function () {
      var source = document.querySelector("[data-cc-copy-source]");
      var label = copyButton.querySelector("[data-cc-copy-label]");
      var original = label ? label.textContent : "";
      function report(ok) {
        if (label) label.textContent = ok ? "Copied" : "Press Ctrl+C";
        copyButton.classList.toggle("is-copied", ok);
        window.setTimeout(function () {
          if (label) label.textContent = original;
          copyButton.classList.remove("is-copied");
        }, 2000);
      }
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(source.value).then(
          function () { report(true); },
          function () { source.select(); report(false); });
      } else {
        source.select();
        var ok = false;
        try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
        report(ok);
      }
    });
  }

  var root = document.querySelector("[data-cc]");
  if (!root) return;

  var STORE_KEY = "cc-view";
  function remember(view) {
    try { window.localStorage.setItem(STORE_KEY, view); } catch (e) { /* private mode */ }
  }

  // ---- layout ----
  // The initial choice was made inline, before paint (_view_boot.html).
  function setView(view) {
    root.setAttribute("data-view", view);
    remember(view);
  }

  // ---- day sheet ----
  var sheet = root.querySelector("[data-cc-sheet]");
  var scrim = root.querySelector("[data-cc-scrim]");
  var titleEl = sheet && sheet.querySelector("[data-cc-sheet-title]");
  var countEl = sheet && sheet.querySelector("[data-cc-sheet-count]");
  var bodyEl = sheet && sheet.querySelector("[data-cc-sheet-body]");
  var prevButton = sheet && sheet.querySelector("[data-cc-prev]");
  var nextButton = sheet && sheet.querySelector("[data-cc-next]");
  var order = Array.prototype.map.call(
    root.querySelectorAll("[data-cc-daygroup]"),
    function (group) { return group.getAttribute("data-cc-daygroup"); });

  var current = null;
  var closeTimer = null;
  var HASH = /^#d-(\d{4}-\d{2}-\d{2})$/;

  function setHash(iso) {
    var base = window.location.pathname + window.location.search;
    window.history.replaceState(window.history.state, "", iso ? base + "#d-" + iso : base);
  }

  function fill(iso) {
    var group = root.querySelector('[data-cc-daygroup="' + iso + '"]');
    if (!group) return false;
    current = iso;
    var cards = group.querySelector(".cc-day-cards");
    var count = cards ? cards.querySelectorAll(".cc-card").length : 0;
    titleEl.textContent = group.getAttribute("data-label");
    countEl.textContent = count + (count === 1 ? " activity" : " activities");
    bodyEl.innerHTML = "";
    if (cards) bodyEl.appendChild(cards.cloneNode(true));
    bodyEl.scrollTop = 0;
    var index = order.indexOf(iso);
    prevButton.disabled = index <= 0;
    nextButton.disabled = index === -1 || index >= order.length - 1;
    return true;
  }

  function open(iso) {
    if (!sheet || !fill(iso)) return;
    window.clearTimeout(closeTimer);
    sheet.hidden = false;
    scrim.hidden = false;
    document.documentElement.classList.add("cc-lock");
    // Two frames: the first lets the browser lay the sheet out off-screen, so
    // the second has something to transition from.
    window.requestAnimationFrame(function () {
      window.requestAnimationFrame(function () { root.classList.add("is-sheet-open"); });
    });
    setHash(iso);
    sheet.querySelector("[data-cc-close]:not(.cc-scrim)").focus({ preventScroll: true });
  }

  function close() {
    if (current === null) return;
    var was = current;
    current = null;
    root.classList.remove("is-sheet-open");
    document.documentElement.classList.remove("cc-lock");
    setHash(null);
    closeTimer = window.setTimeout(function () {
      sheet.hidden = true;
      scrim.hidden = true;
    }, 320);
    // Back to the date that was open last — which, after stepping with the
    // arrows, is not necessarily the one that was tapped.
    var cell = root.querySelector('[data-cc-day="' + was + '"]');
    if (cell) cell.focus({ preventScroll: false });
  }

  function step(delta) {
    if (current === null) return;
    var index = order.indexOf(current) + delta;
    if (index < 0 || index >= order.length) return;
    fill(order[index]);
    setHash(order[index]);
  }

  // ---- events ----
  root.addEventListener("click", function (event) {
    var viewButton = event.target.closest("[data-cc-set-view]");
    if (viewButton) {
      event.preventDefault();
      setView(viewButton.getAttribute("data-cc-set-view"));
      return;
    }
    if (event.target.closest("[data-cc-close]")) { close(); return; }
    if (event.target.closest("[data-cc-prev]")) { step(-1); return; }
    if (event.target.closest("[data-cc-next]")) { step(1); return; }

    var day = event.target.closest("[data-cc-day]");
    if (day && root.getAttribute("data-view") !== "list") {
      event.preventDefault();
      open(day.getAttribute("data-cc-day"));
    }
  });

  document.addEventListener("keydown", function (event) {
    if (current === null) return;
    if (event.key === "Escape") {
      event.preventDefault();
      close();
    } else if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      step(event.key === "ArrowLeft" ? -1 : 1);
    } else if (event.key === "Tab") {
      // Keep focus inside the open dialog.
      var focusable = sheet.querySelectorAll(
        'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])');
      if (!focusable.length) return;
      var first = focusable[0];
      var last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first.focus();
      }
    }
  });

  // Swipe the bottom sheet down to dismiss it, the way every phone sheet works.
  if (sheet) {
    var handle = sheet.querySelector("[data-cc-sheet-drag]");
    var startY = null;
    var dragArea = [sheet.querySelector(".cc-sheet-grip"), handle];
    dragArea.forEach(function (el) {
      if (!el) return;
      el.addEventListener("touchstart", function (event) {
        startY = event.touches[0].clientY;
      }, { passive: true });
      el.addEventListener("touchmove", function (event) {
        if (startY === null) return;
        var delta = Math.max(0, event.touches[0].clientY - startY);
        sheet.style.transition = "none";
        sheet.style.transform = "translateY(" + delta + "px)";
      }, { passive: true });
      el.addEventListener("touchend", function (event) {
        if (startY === null) return;
        var delta = event.changedTouches[0].clientY - startY;
        startY = null;
        sheet.style.transition = "";
        sheet.style.transform = "";
        if (delta > 90) close();
      });
    });
  }

  // A shared link to a specific day (#d-2026-09-14) opens straight onto it.
  var match = HASH.exec(window.location.hash);
  if (match) {
    if (root.getAttribute("data-view") === "grid") {
      open(match[1]);
    } else {
      var target = document.getElementById("d-" + match[1]);
      if (target) target.scrollIntoView({ block: "start" });
    }
  }
})();
