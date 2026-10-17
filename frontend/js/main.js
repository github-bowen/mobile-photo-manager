import { state } from './state.js';
import { api } from './api.js';
import { applyTranslations } from './i18n.js';
import { updateStats, setSwitchState } from './utils.js';
import { loadPhotos, resetAndReload, setupInfiniteScroll, requestNextPage } from './gallery.js';
import { loadTimeline, loadLocations, closeMobileSidebar, resetSidebarTimer } from './sidebar.js';
import { closeModal, renderModalContent, setupModalEvents } from './modal.js';
import { initAutoplay } from './autoplay.js';

const $ = (id) => document.getElementById(id);

async function checkScanStatus() {
  try {
    const status = await api("/api/scan/status");
    const scanBanner = $("scan-banner");
    const loadingOverlay = $("loading-overlay");
    const scanBannerText = $("scan-banner-text");
    const scanBannerProgress = $("scan-banner-progress");
    const loadingText = $("loading-text");
    const loadingProgressBar = $("loading-progress-bar");

    if (status.status === "complete" || status.status === "idle") {
      if (scanBanner) scanBanner.classList.add("hidden");
      if (loadingOverlay) loadingOverlay.classList.add("hidden");

      if (!state.scanComplete) {
        state.scanComplete = true;
        state.currentPage = 1;
        await loadPhotos(false);
        await loadTimeline();
        await loadLocations();
        updateStats();
      }
      return;
    }

    if (status.status === "error") {
      // The scan failed: stop polling, surface the error, and load whatever
      // data the database already holds instead of sticking on the overlay.
      if (loadingOverlay) loadingOverlay.classList.add("hidden");
      if (scanBanner) {
        scanBanner.classList.remove("hidden");
        if (scanBannerText) scanBannerText.textContent = status.message || "Scan failed";
        if (scanBannerProgress) scanBannerProgress.style.width = "100%";
        setTimeout(function () { scanBanner.classList.add("hidden"); }, 10000);
      }

      if (!state.scanComplete) {
        state.scanComplete = true;
        state.currentPage = 1;
        await loadPhotos(false);
        await loadTimeline();
        await loadLocations();
        updateStats();
      }
      return;
    }

    if (status.status === "scanning" || status.status === "generating_thumbnails" || status.status === "geocoding") {
      if (scanBanner) scanBanner.classList.remove("hidden");
      if (scanBannerText) scanBannerText.textContent = status.message || status.status;

      let pct = 100;
      if (status.total > 0) {
        pct = Math.round((status.progress / status.total) * 100);
      }
      if (scanBannerProgress) scanBannerProgress.style.width = pct + "%";

      if (state.photos.length === 0) {
        await loadPhotos(false);
        if (state.photos.length > 0) {
          await loadTimeline();
          await loadLocations();
          updateStats();
        }
      }

      if (state.photos.length === 0) {
        if (loadingOverlay) loadingOverlay.classList.remove("hidden");
        if (loadingText) loadingText.textContent = status.message || status.status;
        if (loadingProgressBar) loadingProgressBar.style.width = pct + "%";
      } else {
        if (loadingOverlay) loadingOverlay.classList.add("hidden");
      }

      setTimeout(checkScanStatus, 2000);
    }
  } catch (e) {
    setTimeout(checkScanStatus, 5000);
  }
}

function setupFilters() {
  const filterBtns = document.querySelectorAll(".filter-group .filter-btn");
  filterBtns.forEach(function (btn) {
    btn.addEventListener("click", function () {
      document.querySelectorAll(".filter-group .filter-btn").forEach(b => b.classList.remove("active"));
      this.classList.add("active");
      const filter = this.dataset.filter;
      state.activeFilter = filter;
      resetAndReload();
      resetSidebarTimer();
    });
  });

  const dateFromInput = $("date-from");
  const dateToInput = $("date-to");
  const searchInput = $("search-input");
  
  if (dateFromInput) {
    dateFromInput.addEventListener("change", function () {
      state.dateFrom = this.value || null;
      resetAndReload();
    });
  }

  if (dateToInput) {
    dateToInput.addEventListener("change", function () {
      state.dateTo = this.value || null;
      resetAndReload();
    });
  }

  let searchTimeout;
  if (searchInput) {
    searchInput.addEventListener("input", function () {
      clearTimeout(searchTimeout);
      searchTimeout = setTimeout(function () {
        const val = searchInput.value.trim();
        state.searchQuery = val;
        // Free-text search and location filters are mutually exclusive:
        // typing here drops any sidebar-picked place, keeping the query
        // string unambiguous (and the sidebar in charge when it's used).
        state.activeLocation = null;
        state.activeCountry = null;
        state.activeCity = null;
        resetAndReload();
      }, 400);
    });
  }

  const mobileMenuBtn = $("mobile-menu-btn");
  const sidebar = $("sidebar");
  const sidebarOverlay = $("sidebar-overlay");
  if (mobileMenuBtn && sidebar && sidebarOverlay) {
    mobileMenuBtn.addEventListener("click", function () {
      sidebar.classList.add("open");
      sidebarOverlay.classList.add("show");
      resetSidebarTimer();
    });
    sidebarOverlay.addEventListener("click", function () {
      closeMobileSidebar();
    });
  }

  const loadMoreBtn = $("load-more-btn");
  if (loadMoreBtn) {
    loadMoreBtn.addEventListener("click", function () {
      requestNextPage();
    });
  }
}

// role="switch" widgets activate on Enter/Space like native checkboxes do;
// the pills have no native key behavior of their own, so route keys to click.
function armPillKeyboard(btn) {
  if (!btn) return;
  btn.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      btn.click();
    }
  });
}

// Theme is stored on BOTH the body class (legacy + component selectors)
// and html[data-theme] (set pre-paint by boot.js). classList, not className
// — a bare className assignment would clobber any other body class.
function applyTheme() {
  document.body.classList.remove("theme-light", "theme-dark");
  document.body.classList.add("theme-" + state.theme);
  document.documentElement.setAttribute("data-theme", state.theme);
}

async function init() {
  const langToggle = $("lang-toggle");
  const loadOriginalToggle = $("load-original-toggle");
  const themeToggle = $("theme-toggle");
  const sortToggle = $("sort-toggle");

  try {
    const config = await api("/api/config");
    
    if (localStorage.getItem("app_language")) {
        state.language = localStorage.getItem("app_language");
    } else if (config.language) {
        state.language = config.language;
    }
    setSwitchState(langToggle, state.language === "en");

    if (localStorage.getItem("app_theme")) {
        state.theme = localStorage.getItem("app_theme");
    } else if (config.theme) {
        state.theme = config.theme;
    }
    setSwitchState(themeToggle, state.theme === "dark");

    if (localStorage.getItem("app_sort")) {
        state.sortOrder = localStorage.getItem("app_sort");
    }
    setSwitchState(sortToggle, state.sortOrder === "asc");

    if (config.load_original_on_click !== undefined) {
        state.loadOriginalOnClick = config.load_original_on_click;
        setSwitchState(loadOriginalToggle, state.loadOriginalOnClick);
    }
  } catch (e) {
    console.warn("Failed to load config, using defaults");
  }

  applyTranslations();
  applyTheme();

  setupFilters();
  setupModalEvents();
  setupInfiniteScroll();

  armPillKeyboard(loadOriginalToggle);
  armPillKeyboard(langToggle);
  armPillKeyboard(themeToggle);
  armPillKeyboard(sortToggle);

  if (loadOriginalToggle) {
    loadOriginalToggle.addEventListener("click", function (e) {
      const on = !this.classList.contains("active");
      setSwitchState(this, on);
      state.loadOriginalOnClick = on;
    });
  }

  if (langToggle) {
    langToggle.addEventListener("click", function (e) {
      const on = !this.classList.contains("active");
      setSwitchState(this, on);
      state.language = on ? "en" : "zh";
      localStorage.setItem("app_language", state.language);
      applyTranslations();
      loadTimeline();
      loadLocations();
      resetAndReload();
    });
  }

  if (themeToggle) {
    themeToggle.addEventListener("click", function (e) {
      const on = !this.classList.contains("active");
      setSwitchState(this, on);
      state.theme = on ? "dark" : "light";
      localStorage.setItem("app_theme", state.theme);
      applyTheme();
    });
  }

  if (sortToggle) {
    sortToggle.addEventListener("click", function (e) {
      const on = !this.classList.contains("active");
      setSwitchState(this, on);
      state.sortOrder = on ? "asc" : "desc";
      localStorage.setItem("app_sort", state.sortOrder);
      loadTimeline();
      resetAndReload();
    });
  }

  initAutoplay();

  await checkScanStatus();

  if (state.scanComplete && state.photos.length === 0) {
    await loadPhotos(false);
    await loadTimeline();
    await loadLocations();
    updateStats();
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
