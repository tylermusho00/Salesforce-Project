// Big-number "count up" animation for the metric components.
// Whenever Dash writes a new value into a .metric-value element (e.g. "47,011" or "98.6%"),
// we animate from 0 up to that number, keeping its prefix/suffix and number format.
(function () {
  var PATTERN = /^([^\d-]*)(-?[\d,]*\.?\d+)(.*)$/;

  function animate(el, text) {
    var m = PATTERN.exec(text);
    if (!m) { el._final = text; return; }
    var prefix = m[1], numText = m[2], suffix = m[3];
    var target = parseFloat(numText.replace(/,/g, ""));
    var decimals = (numText.split(".")[1] || "").length;
    var useCommas = numText.indexOf(",") >= 0 || target >= 1000;
    var start = performance.now(), duration = 900;
    el._final = text;
    el._animating = true;

    function format(v) {
      var s = v.toFixed(decimals);
      if (useCommas) {
        var parts = s.split(".");
        parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
        s = parts.join(".");
      }
      return prefix + s + suffix;
    }
    function step(now) {
      var t = Math.min(1, (now - start) / duration);
      var eased = 1 - Math.pow(1 - t, 3); // ease-out
      el.textContent = t < 1 ? format(target * eased) : text;
      if (t < 1) requestAnimationFrame(step); else el._animating = false;
    }
    requestAnimationFrame(step);
  }

  var observer = new MutationObserver(function (mutations) {
    mutations.forEach(function (mu) {
      var el = mu.target.nodeType === 3 ? mu.target.parentElement : mu.target;
      if (!el || !el.classList || !el.classList.contains("metric-value")) return;
      if (el._animating) return;
      var text = el.textContent.trim();
      if (text && text !== el._final && text !== "—") animate(el, text);
    });
  });

  function start() {
    observer.observe(document.body, { subtree: true, childList: true, characterData: true });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
