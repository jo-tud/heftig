// Runs synchronously in <head>: apply the privacy mode before anything is painted.
// Default is ON; only an explicit "off" on this device shows data unblurred.
(function () {
  document.documentElement.classList.add("js"); // CSS: JS-enhanced layouts (e.g. filter sheet)
  var on = true;
  try { on = localStorage.getItem("heftig-privacy") !== "off"; } catch (e) { /* storage blocked */ }
  if (on) document.documentElement.classList.add("privacy");
})();
