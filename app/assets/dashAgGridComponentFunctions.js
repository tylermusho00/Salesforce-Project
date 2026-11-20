// Custom AG Grid cell renderers (dash-ag-grid loads this file automatically from /assets).
var dagcomponentfuncs = (window.dashAgGridComponentFunctions = window.dashAgGridComponentFunctions || {});

// Color-coded tier pill: Critical / High / Medium / Low
dagcomponentfuncs.TierBadge = function (props) {
  if (!props.value) return null;
  return React.createElement(
    "span",
    { className: "slds-badge tier-" + String(props.value).toLowerCase() },
    props.value
  );
};

// Assigned / Manager Review pill
dagcomponentfuncs.StatusPill = function (props) {
  if (!props.value) return null;
  var cls = props.value === "Assigned" ? "status-assigned" : "status-review";
  return React.createElement("span", { className: "slds-badge " + cls }, props.value);
};

// Load vs max capacity bar for the rep table
dagcomponentfuncs.CapacityBar = function (props) {
  var d = props.data;
  if (!d) return null;
  var pct = d.max_capacity > 0 ? Math.min(100, (100 * d.load) / d.max_capacity) : 100;
  var color = pct >= 100 ? "#BA0517" : pct >= 80 ? "#DD7A01" : "#0176D3";
  return React.createElement(
    "div",
    { className: "cap-bar", title: d.load + " of " + d.max_capacity + " leads" },
    React.createElement("div", { className: "cap-fill", style: { width: pct + "%", background: color } }),
    React.createElement("span", { className: "cap-label" }, d.load + " / " + d.max_capacity)
  );
};
