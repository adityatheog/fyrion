"use strict";

/**
 * Settings editor behaviour.
 *
 * Design notes:
 *  - Every control carries data-field, data-kind and data-initial, so the form
 *    is rendered server-side and this script only tracks what changed.
 *  - Only changed fields are submitted, so a PATCH never rewrites a value the
 *    operator did not touch.
 *  - Fields marked data-required are skipped when blank, because the underlying
 *    columns are NOT NULL and clearing them would be rejected by the database.
 *  - The session CSRF token is sent in the X-CSRF-Token header; a cross-site
 *    form post cannot set it.
 */
(function () {
  var form = document.getElementById("settings-form");
  var bootstrapNode = document.getElementById("bootstrap");
  var saveButton = document.getElementById("save-button");
  var resetButton = document.getElementById("reset-button");
  var statusNode = document.getElementById("dirty-status");
  var toastStack = document.getElementById("toasts");

  if (!form || !bootstrapNode || !saveButton) {
    return;
  }

  var bootstrap;
  try {
    bootstrap = JSON.parse(bootstrapNode.getAttribute("data-payload") || "{}");
  } catch (error) {
    bootstrap = {};
  }

  var guildId = String(bootstrap.guild_id || "");
  var csrfToken = String(bootstrap.csrf || "");
  var controls = Array.prototype.slice.call(
    form.querySelectorAll("[data-field][data-kind]")
  );

  /* ------------------------------------------------------------------ toasts */

  function toast(kind, title, body) {
    if (!toastStack) {
      return;
    }

    var element = document.createElement("div");
    element.className = "toast toast-" + kind;

    var heading = document.createElement("div");
    heading.className = "toast-title";
    heading.textContent = title;
    element.appendChild(heading);

    if (body) {
      var text = document.createElement("div");
      text.className = "toast-body";
      text.textContent = body;
      element.appendChild(text);
    }

    toastStack.appendChild(element);
    window.setTimeout(function () {
      if (element.parentNode) {
        element.parentNode.removeChild(element);
      }
    }, kind === "error" ? 9000 : 4500);
  }

  /* ------------------------------------------------------------ value access */

  function currentValue(control) {
    if (control.dataset.kind === "bool") {
      return control.checked ? "true" : "false";
    }
    return String(control.value == null ? "" : control.value).trim();
  }

  function initialValue(control) {
    return String(control.dataset.initial == null ? "" : control.dataset.initial).trim();
  }

  function isDirty(control) {
    return currentValue(control) !== initialValue(control);
  }

  function changedControls() {
    return controls.filter(isDirty);
  }

  /* --------------------------------------------------------------- dirty UI */

  function refreshDirtyState() {
    var changed = changedControls();
    saveButton.disabled = changed.length === 0;

    if (statusNode) {
      if (changed.length === 0) {
        statusNode.textContent = "No unsaved changes";
        statusNode.setAttribute("data-dirty", "false");
      } else {
        statusNode.textContent =
          changed.length === 1
            ? "1 unsaved change"
            : changed.length + " unsaved changes";
        statusNode.setAttribute("data-dirty", "true");
      }
    }
  }

  /* ------------------------------------------------------------- serialising */

  function buildPayload() {
    var payload = {};
    var skipped = [];

    changedControls().forEach(function (control) {
      var field = control.dataset.field;
      var kind = control.dataset.kind;
      var required = control.dataset.required === "true";
      var raw = currentValue(control);

      if (kind === "bool") {
        payload[field] = raw === "true";
        return;
      }

      if (raw === "") {
        if (required) {
          // NOT NULL column: clearing it would be rejected, so leave it alone.
          skipped.push(field);
          return;
        }
        payload[field] = null;
        return;
      }

      if (kind === "int" || kind === "snowflake") {
        var parsed = Number(raw);
        if (!isFinite(parsed) || Math.floor(parsed) !== parsed) {
          skipped.push(field);
          return;
        }
        payload[field] = parsed;
        return;
      }

      payload[field] = raw;
    });

    return { payload: payload, skipped: skipped };
  }

  function applySettings(settings) {
    if (!settings) {
      return;
    }

    controls.forEach(function (control) {
      var field = control.dataset.field;
      if (!Object.prototype.hasOwnProperty.call(settings, field)) {
        return;
      }

      var value = settings[field];

      if (control.dataset.kind === "bool") {
        var checked = Boolean(value);
        control.checked = checked;
        control.dataset.initial = checked ? "true" : "false";
        return;
      }

      var text = value === null || typeof value === "undefined" ? "" : String(value);
      control.value = text;
      control.dataset.initial = text;
    });

    refreshDirtyState();
  }

  /* --------------------------------------------------------------- submitting */

  function describeErrors(data) {
    if (!data) {
      return "";
    }
    if (Array.isArray(data.details) && data.details.length) {
      return data.details
        .map(function (item) {
          return (item.field ? item.field + ": " : "") + item.message;
        })
        .join("; ");
    }
    if (data.reference) {
      return "Reference " + data.reference;
    }
    return "";
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();

    var built = buildPayload();
    var fields = Object.keys(built.payload);

    if (built.skipped.length) {
      toast(
        "error",
        "Some fields were skipped",
        built.skipped.join(", ") +
          " cannot be left empty or did not contain a whole number."
      );
    }

    if (!fields.length) {
      refreshDirtyState();
      return;
    }

    saveButton.disabled = true;
    saveButton.textContent = "Saving\u2026";

    fetch("/api/guilds/" + encodeURIComponent(guildId), {
      method: "PATCH",
      credentials: "same-origin",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        "X-CSRF-Token": csrfToken
      },
      body: JSON.stringify(built.payload)
    })
      .then(function (response) {
        if (response.status === 401) {
          window.location.href = "/login";
          return null;
        }
        return response
          .json()
          .catch(function () {
            return {};
          })
          .then(function (data) {
            return { ok: response.ok, status: response.status, data: data };
          });
      })
      .then(function (result) {
        if (!result) {
          return;
        }

        if (result.ok) {
          applySettings(result.data.settings);
          var updated = Array.isArray(result.data.updated)
            ? result.data.updated.length
            : fields.length;
          toast(
            "ok",
            "Settings saved",
            updated === 1 ? "1 setting updated." : updated + " settings updated."
          );
          return;
        }

        toast(
          "error",
          result.data.error || "The change was rejected (HTTP " + result.status + ")",
          describeErrors(result.data)
        );
      })
      .catch(function (error) {
        toast(
          "error",
          "The change could not be sent",
          error.message || "Check your connection and try again."
        );
      })
      .then(function () {
        saveButton.textContent = "Save changes";
        refreshDirtyState();
      });
  });

  if (resetButton) {
    resetButton.addEventListener("click", function () {
      controls.forEach(function (control) {
        if (control.dataset.kind === "bool") {
          control.checked = initialValue(control) === "true";
        } else {
          control.value = initialValue(control);
        }
      });
      refreshDirtyState();
      toast("ok", "Changes discarded", "The form was reset to the stored values.");
    });
  }

  controls.forEach(function (control) {
    var event = control.tagName === "SELECT" || control.type === "checkbox" ? "change" : "input";
    control.addEventListener(event, refreshDirtyState);
  });

  window.addEventListener("beforeunload", function (event) {
    if (changedControls().length) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  refreshDirtyState();
})();
