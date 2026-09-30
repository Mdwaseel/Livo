/* Chart initialiser, shared by every page that renders charts.
 *
 * Each <canvas data-chart="id"> picks its config out of the {{ charts|json_script }}
 * block by id. The configs are built server-side in analytics/charts.py, so the
 * numbers are formatted once by the same code that computed them; what happens
 * here is only the part JSON cannot carry — callbacks, click handlers, and the
 * reduced-motion decision.
 *
 * Lives in a file rather than inline in a template because two pages need it
 * (the analytics dashboard and the business overview) and a second copy would
 * drift.
 */
(function () {
  var node = document.getElementById("chart-configs");
  if (!node || typeof Chart === "undefined") return;

  var configs;
  try {
    configs = JSON.parse(node.textContent);
  } catch (e) {
    return;   // a malformed payload leaves empty canvases, not a broken page
  }

  // Honour the OS-level setting rather than animating regardless — the same
  // rule the stylesheet's prefers-reduced-motion block already follows.
  var still = window.matchMedia
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function rupees(value) {
    // en-IN grouping (1,20,000) so chart axes read the same as every other
    // money figure in the console.
    return "₹" + Number(value).toLocaleString("en-IN",
      { maximumFractionDigits: 0 });
  }

  Chart.defaults.font.family =
    "Inter, -apple-system, Segoe UI, Roboto, sans-serif";
  Chart.defaults.color = "#5b6560";
  // Hover targets bigger than the marks themselves, so a thin bar or a 2px
  // line is still easy to hit with a trackpad.
  Chart.defaults.plugins.tooltip.caretPadding = 6;

  function applyCurrency(config) {
    // Format money in the tooltip and on the value axis. Set here rather than
    // server-side because a callback cannot survive JSON.
    config.options.plugins = config.options.plugins || {};
    config.options.plugins.tooltip = config.options.plugins.tooltip || {};
    config.options.plugins.tooltip.callbacks = {
      label: function (item) {
        var value = item.parsed.y != null ? item.parsed.y : item.parsed;
        // A horizontal bar's value is on x; a doughnut has no axis at all.
        if (item.parsed.x != null && item.parsed.y != null
            && item.chart.options.indexAxis === "y") {
          value = item.parsed.x;
        }
        var label = item.dataset.label || item.label || "";
        return (label ? label + ": " : "") + rupees(value);
      }
    };
    var scales = config.options.scales || {};
    var axis = config.options.indexAxis === "y" ? "x" : "y";
    if (scales[axis]) {
      scales[axis].ticks = scales[axis].ticks || {};
      scales[axis].ticks.callback = function (v) { return rupees(v); };
    }
  }

  function applyDrill(config, drill) {
    // Click a mark, open the thing it stands for. The index is the category
    // index, which is exactly how the server built the parallel URL list.
    config.options.onClick = function (event, elements) {
      if (!elements || !elements.length) return;
      var href = drill[elements[0].index];
      if (href) window.location.assign(href);
    };
    // A pointer only over marks that actually lead somewhere — a cursor that
    // lies about being clickable is worse than no cursor change at all.
    config.options.onHover = function (event, elements) {
      var over = elements && elements.length && drill[elements[0].index];
      if (event.native && event.native.target) {
        event.native.target.style.cursor = over ? "pointer" : "default";
      }
    };
  }

  document.querySelectorAll("canvas[data-chart]").forEach(function (canvas) {
    var config = configs[canvas.dataset.chart];
    if (!config || !config.options) return;

    var isCurrency = config.options._currency;
    var drill = config.options._drill || null;
    delete config.options._currency;
    delete config.options._drill;

    if (still) {
      // Reduced motion means no entrance and no transition, not a shorter one.
      config.options.animation = false;
      config.options.animations = false;
      delete config.options.transitions;
    }
    if (drill) applyDrill(config, drill);
    if (isCurrency) applyCurrency(config);

    new Chart(canvas, config);
  });
})();
