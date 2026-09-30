/* Client approvals — the reviewer's page and the team's "ask for approval" form.

   Progressive enhancement, like the calendar. Without this file the reviewer
   can still type a code, pick Approve or Request changes and submit, and the
   server still insists on feedback for a change request. This adds the parts
   that make it pleasant on a phone: the code submits itself when the sixth
   digit lands, the form's wording follows the choice, pictures open full-size,
   and the bottom bar gets out of the way once the decision is on screen.
*/
(function () {
  "use strict";

  // ---- the decision: approve, or request changes ----
  var decide = document.querySelector("[data-rv-decide]");
  if (decide) {
    var feedback = decide.querySelector("[data-rv-feedback]");
    var submit = decide.querySelector("[data-rv-submit]");

    var sync = function () {
      var checked = decide.querySelector('input[name="decision"]:checked');
      var value = checked ? checked.value : "approve";
      decide.setAttribute("data-decision", value);
      decide.querySelectorAll("[data-when]").forEach(function (el) {
        el.hidden = el.getAttribute("data-when") !== value;
      });
      feedback.required = value === "changes";
      feedback.placeholder = value === "changes"
        ? "e.g. Please change the offer date to 25 October and use the red logo."
        : "Optional — a note for the team.";
    };

    decide.addEventListener("change", function (event) {
      if (event.target.name !== "decision") return;
      sync();
      feedback.removeAttribute("aria-invalid");
      if (event.target.value === "changes") feedback.focus();
    });

    decide.addEventListener("submit", function (event) {
      if (feedback.required && !feedback.value.trim()) {
        event.preventDefault();
        feedback.setAttribute("aria-invalid", "true");
        feedback.focus();
        return;
      }
      // One answer, once: a double tap on a slow connection must not post twice.
      submit.disabled = true;
      submit.setAttribute("aria-busy", "true");
    });
    sync();

    var dock = document.querySelector("[data-rv-dock]");
    if (dock && "IntersectionObserver" in window) {
      new IntersectionObserver(function (entries) {
        dock.hidden = entries[0].isIntersecting;
      }, { rootMargin: "0px 0px -40px 0px" }).observe(decide);
    }
  }

  // ---- copy the text being approved ----
  document.querySelectorAll("[data-rv-copy]").forEach(function (button) {
    var label = button.querySelector("span");
    var original = label ? label.textContent : "";
    button.addEventListener("click", function () {
      var source = document.querySelector(button.getAttribute("data-rv-copy"));
      var report = function (ok) {
        if (!label) return;
        label.textContent = ok ? "Copied" : "Select the text to copy";
        window.setTimeout(function () { label.textContent = original; }, 1800);
      };
      if (source && navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(source.innerText.trim()).then(
          function () { report(true); }, function () { report(false); });
      } else {
        report(false);
      }
    });
  });

  // ---- the six-digit code ----
  var code = document.querySelector("[data-rv-code]");
  if (code) {
    code.addEventListener("input", function () {
      var digits = code.value.replace(/\D/g, "").slice(0, 6);
      if (digits !== code.value) code.value = digits;
      if (digits.length === 6 && !code.form.getAttribute("data-sent")) {
        code.form.setAttribute("data-sent", "1");
        if (typeof code.form.requestSubmit === "function") code.form.requestSubmit();
        else code.form.submit();
      }
    });
  }

  // ---- "send a new code" waits out the cooldown visibly ----
  document.querySelectorAll("[data-rv-wait]").forEach(function (button) {
    var left = parseInt(button.getAttribute("data-rv-wait"), 10) || 0;
    if (left <= 0) return;
    var label = button.textContent;
    button.disabled = true;
    (function tick() {
      if (left <= 0) {
        button.disabled = false;
        button.textContent = label;
        return;
      }
      button.textContent = label + " (" + left + "s)";
      left -= 1;
      window.setTimeout(tick, 1000);
    })();
  });

  // ---- pictures, full size ----
  var box = document.querySelector("[data-rv-lightbox]");
  if (box && typeof box.showModal === "function") {
    var opener = null;
    var picture = null;
    document.addEventListener("click", function (event) {
      var link = event.target.closest("[data-rv-zoom]");
      if (link) {
        event.preventDefault();
        opener = link;
        var thumb = link.querySelector("img");
        picture = document.createElement("img");
        picture.src = link.href;
        picture.alt = thumb ? thumb.alt : "";
        box.appendChild(picture);
        box.showModal();
        return;
      }
      if (box.open && (event.target === box || event.target === picture
          || event.target.closest("[data-rv-lightbox-close]"))) {
        box.close();
      }
    });
    box.addEventListener("close", function () {
      if (picture) picture.remove();
      picture = null;
      if (opener) opener.focus();
    });
  }

  // ---- "Email the client about this now" on the activity form ----
  // Who and what only appear once the box is ticked; a form re-shown with an
  // error keeps it ticked, so they reappear with it.
  document.querySelectorAll("[data-cc-notify]").forEach(function (box) {
    var toggle = box.querySelector("[data-cc-notify-toggle]");
    var body = box.querySelector("[data-cc-notify-body]");
    if (!toggle || !body) return;
    var sync = function () { body.hidden = !toggle.checked; };
    toggle.addEventListener("change", function () {
      sync();
      if (toggle.checked) {
        var first = body.querySelector("input:not([disabled])");
        if (first) first.focus();
      }
    });
    sync();
  });

  // ---- the team's form ----
  var form = document.querySelector("[data-ap-form]");
  if (!form) return;

  var addRow = form.querySelector("[data-ap-add-row]");
  if (addRow) {
    addRow.addEventListener("click", function () {
      var rows = form.querySelectorAll("[data-ap-new-row]");
      var last = rows[rows.length - 1];
      var clone = last.cloneNode(true);
      clone.querySelectorAll("input").forEach(function (input) { input.value = ""; });
      last.parentNode.insertBefore(clone, last.nextSibling);
      clone.querySelector("input").focus();
    });
  }

  var files = form.querySelector("[data-ap-files]");
  var list = form.querySelector("[data-ap-file-list]");
  var fileError = form.querySelector("[data-ap-file-error]");
  var drop = form.querySelector("[data-ap-drop]");
  var maxBytes = parseInt(form.getAttribute("data-max-bytes"), 10) || 0;
  var maxFiles = parseInt(form.getAttribute("data-max-files"), 10) || 0;
  var problem = "";

  var human = function (bytes) {
    return bytes < 1048576 ? Math.max(1, Math.round(bytes / 1024)) + " KB"
                           : (bytes / 1048576).toFixed(1) + " MB";
  };

  if (files && list) {
    // The server aborts an oversized upload mid-transfer, which the browser
    // shows as a dropped connection. Saying so before submit is far kinder.
    files.addEventListener("change", function () {
      list.innerHTML = "";
      problem = "";
      var tooBig = 0;
      Array.prototype.forEach.call(files.files, function (file) {
        var item = document.createElement("li");
        var name = document.createElement("span");
        var size = document.createElement("span");
        name.textContent = file.name;
        size.textContent = human(file.size);
        if (maxBytes && file.size > maxBytes) {
          item.className = "is-too-big";
          size.textContent += " — too large";
          tooBig += 1;
        }
        item.appendChild(name);
        item.appendChild(size);
        list.appendChild(item);
      });
      list.hidden = !files.files.length;
      if (tooBig) {
        problem = "Remove the files marked too large, or share them as a Drive link instead.";
      } else if (maxFiles && files.files.length > maxFiles) {
        problem = "Attach at most " + maxFiles + " files — put the rest in a Drive folder and add its link.";
      }
      if (fileError) {
        fileError.textContent = problem;
        fileError.hidden = !problem;
      }
    });
  }

  if (drop) {
    ["dragenter", "dragover"].forEach(function (name) {
      drop.addEventListener(name, function () { drop.classList.add("is-over"); });
    });
    ["dragleave", "drop"].forEach(function (name) {
      drop.addEventListener(name, function () { drop.classList.remove("is-over"); });
    });
  }

  form.addEventListener("submit", function (event) {
    if (problem) {
      event.preventDefault();
      if (files) files.focus();
      return;
    }
    var button = form.querySelector("[data-ap-submit]");
    if (button) button.disabled = true;
  });
})();
