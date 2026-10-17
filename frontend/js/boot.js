// Boot theme: runs as a render-blocking classic script in <head>, before
// the stylesheet loads, so dark-mode users never see a light flash (FOUC).
// The theme is mirrored onto <html data-theme> for the CSS variable blocks;
// main.js later syncs the body class from /api/config as a fallback when
// localStorage is empty.
(function () {
  var theme = null;
  try {
    theme = localStorage.getItem("app_theme");
  } catch (e) {
    // Private browsing / blocked storage: fall through to light.
  }
  document.documentElement.setAttribute(
    "data-theme", theme === "dark" ? "dark" : "light"
  );
})();
