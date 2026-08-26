"use strict";

/**
 * Server picker behaviour.
 *
 * The guild cards themselves are rendered server-side; this script only fills
 * the live counters from GET /api/bot/stats. A failure is reported in place
 * rather than left as an ellipsis, so a stale panel is never mistaken for a
 * healthy one.
 */
(function () {
  var statsRoot = document.getElementById("stats");
  var note = document.getElementById("stats-note");

  if (!statsRoot) {
    return;
  }

  function setValue(key, value) {
    var node = statsRoot.querySelector('[data-stat="' + key + '"]');
    if (node) {
      node.textContent = value;
    }
  }

  function formatNumber(value) {
    if (typeof value !== "number" || !isFinite(value)) {
      return "n/a";
    }
    return value.toLocaleString();
  }

  function showNote(message) {
    if (!note) {
      return;
    }
    note.textContent = message;
    note.hidden = false;
  }

  function render(stats) {
    setValue("guilds", formatNumber(stats.guilds));
    setValue("users", formatNumber(stats.users));
    setValue("shards", formatNumber(stats.shards));
    setValue(
      "latency_ms",
      typeof stats.latency_ms === "number" ? stats.latency_ms + " ms" : "n/a"
    );
    setValue("commands", formatNumber(stats.commands));
    setValue(
      "cogs",
      Array.isArray(stats.cogs) ? formatNumber(stats.cogs.length) : "n/a"
    );

    var parts = [];
    if (stats.note) {
      parts.push(stats.note);
    }
    if (stats.database && typeof stats.database.schema_version !== "undefined") {
      parts.push(
        "Database schema v" +
          stats.database.schema_version +
          " \u2022 pool " +
          stats.database.pool_size +
          " (" +
          stats.database.available_connections +
          " idle)"
      );
    }
    if (parts.length) {
      showNote(parts.join(" \u2022 "));
    }
  }

  function failed(message) {
    ["guilds", "users", "shards", "latency_ms", "commands", "cogs"].forEach(
      function (key) {
        setValue(key, "\u2014");
      }
    );
    showNote(message);
  }

  fetch("/api/bot/stats", {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    cache: "no-store"
  })
    .then(function (response) {
      if (response.status === 401) {
        window.location.href = "/login";
        return null;
      }
      if (!response.ok) {
        throw new Error("HTTP " + response.status);
      }
      return response.json();
    })
    .then(function (stats) {
      if (stats) {
        render(stats);
      }
    })
    .catch(function (error) {
      failed(
        "Live statistics are unavailable right now (" + error.message + ")."
      );
    });
})();
