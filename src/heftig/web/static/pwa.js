"use strict";
// Installed as an app: the service worker (sw.js) only shows the offline page when Heftig cannot
// be reached. Browsers allow service workers over HTTPS and on localhost only.
(function () {
  if (document.body.classList.contains("offline")) {
    // the offline page: try again on request, when the network returns, or when the app is
    // brought back to the front
    const retry = () => location.reload();
    document.getElementById("retry").addEventListener("click", retry);
    window.addEventListener("online", retry);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") retry();
    });
    return;
  }
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => { /* not available: the browser's own error page */ });
  }
})();
