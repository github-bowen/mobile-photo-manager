import { state } from './state.js';
import { api } from './api.js';
import { t } from './i18n.js';
import { $, formatDateLabel, formatDuration, createEmptyState } from './utils.js';
import { openModal } from './modal.js';
import { toggleFavorite } from './favorites.js';

let galleryObserver = null;
const VIDEO_TYPES = new Set(["MOV", "MP4", "3GP"]);

// O(1) photo lookups. The IntersectionObserver callback fires for every
// card crossing the viewport edge, so a linear find() over state.photos
// there costs O(N) per scroll burst on large libraries. Rebuilt whenever
// state.photos is reassigned or appended (loadPhotos).
const photoById = new Map();
const photoIndexById = new Map();

function rebuildPhotoIndex() {
  photoById.clear();
  photoIndexById.clear();
  state.photos.forEach(function (photo, index) {
    photoById.set(photo.id, photo);
    photoIndexById.set(photo.id, index);
  });
}

// --- Card pool: recycle far-off-screen cards ---
// The near observer (600px margin) loads/unloads card *content*; the far
// observer (3000px margin) swaps whole cards that are way outside the
// viewport for a lightweight placeholder and returns them to a pool. The
// DOM then holds at most viewport + 3000px worth of live cards however
// deep the library scrolls. Cards are interchangeable: every listener
// reads the photo identity from the card's data-id at event time, so a
// pooled card never needs its handlers rebound.
const CARD_POOL_LIMIT = 2000;
const cardPool = [];
let farObserver = null;

function initVirtualization() {
  if (galleryObserver) {
    galleryObserver.disconnect();
  }
  if (farObserver) {
    farObserver.disconnect();
  }

  galleryObserver = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      const card = entry.target;
      const photoId = Number(card.dataset.id);
      const photo = photoById.get(photoId);
      if (!photo) return;

      if (entry.isIntersecting) {
        if (!card.hasAttribute('data-rendered')) {
          renderCardContent(card, photo);
          card.setAttribute('data-rendered', 'true');
        }
      } else {
        if (card.hasAttribute('data-rendered')) {
          // Unload DOM elements to save memory (DOM Virtualization)
          unloadCardContent(card);
        }
      }
    });
  }, {
    root: $('gallery-container'),
    rootMargin: '600px 0px', // Buffer zone to prevent flashing
    threshold: 0
  });

  farObserver = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      const el = entry.target;
      if (entry.isIntersecting) {
        if (el.classList.contains('photo-card-placeholder')) {
          restoreCard(el);
        }
      } else {
        if (el.classList.contains('photo-card')) {
          recycleCard(el);
        }
      }
    });
  }, {
    root: $('gallery-container'),
    rootMargin: '3000px 0px', // Beyond this, swap the whole card out
    threshold: 0
  });
}

function recycleCard(card) {
  const placeholder = document.createElement("div");
  placeholder.className = "photo-card-placeholder";
  placeholder.dataset.recycleId = card.dataset.id;
  card.replaceWith(placeholder);

  // Empty the card before pooling (clears hover timers and preview videos
  // too) and drop its observer registrations so dropped cards can be GC'd.
  unloadCardContent(card);
  galleryObserver.unobserve(card);
  farObserver.unobserve(card);

  if (cardPool.length < CARD_POOL_LIMIT) {
    cardPool.push(card);
  }

  farObserver.observe(placeholder);
}

function restoreCard(placeholder) {
  farObserver.unobserve(placeholder);
  const photo = photoById.get(Number(placeholder.dataset.recycleId));
  if (!photo) {
    // The photo left the current view (filter switch) while it was
    // recycled — the placeholder has nothing to restore into.
    placeholder.remove();
    return;
  }

  const card = cardPool.pop() || buildNewCard();
  card.setAttribute("data-id", photo.id);

  // Restored cards appear instantly. A temporary class removed on the next
  // frame wouldn't work — CSS would treat the none->fadeIn change as a new
  // animation and replay the entry fade one frame later.
  card.style.animation = "none";
  placeholder.replaceWith(card);

  galleryObserver.observe(card);
  farObserver.observe(card);
}

export function unloadCardContent(card) {
  // Clear any pending hover-preview timer so it can't attach a zombie
  // <video> to a card that just left the viewport.
  if (card.__hoverTimer) {
    clearTimeout(card.__hoverTimer);
    card.__hoverTimer = null;
  }
  const video = card.querySelector("video");
  if (video) {
    video.pause();
    video.removeAttribute("src");
    video.load();
    video.remove();
  }
  card.replaceChildren();
  card.removeAttribute("data-rendered");
}

function renderCardContent(card, photo) {
  const img = document.createElement("img");
  img.className = "loading";
  img.alt = photo.filename || t("photo_alt");
  // No cache-busting query: the thumbnail URL is stable, so the browser
  // cache and the server's ETag/304 revalidation do the invalidation work
  // (a replaced file gets a fresh thumbnail with a new ETag on rescan).
  img.src = "/api/photos/" + photo.id + "/thumbnail/small";
  img.onload = () => img.className = "loaded";
  img.onerror = () => {
    img.className = "loaded";
    img.style.backgroundColor = "var(--bg-tertiary)";
  };
  card.appendChild(img);

  const overlay = document.createElement("div");
  overlay.className = "photo-card-overlay";

  if (photo.is_live_photo || photo.is_motion_photo) {
    const badge = document.createElement("span");
    badge.className = "photo-card-badge " + (photo.is_motion_photo ? "motion" : "live");
    badge.textContent = photo.is_motion_photo ? "MOTION" : "LIVE";
    overlay.appendChild(badge);
  } else if (VIDEO_TYPES.has(photo.file_type)) {
    const badge = document.createElement("span");
    badge.className = "photo-card-badge video";
    const dur = photo.duration ? formatDuration(photo.duration) : t("video_dur");
    badge.textContent = dur;
    overlay.appendChild(badge);
  }

  if (photo.location_name) {
    const locBadge = document.createElement("span");
    locBadge.className = "photo-card-badge";
    locBadge.textContent = "📍";
    overlay.appendChild(locBadge);
  }

  card.appendChild(overlay);

  // Interactive favorite button at top-right
  const favBtn = document.createElement("button");
  favBtn.className = "photo-card-fav-btn" + (photo.is_favorite ? " active" : "");
  favBtn.setAttribute("type", "button");
  favBtn.innerHTML = photo.is_favorite ? "★" : "☆";
  const favTitle = photo.is_favorite ? t("unfavorite_btn") : t("favorite_btn");
  favBtn.title = favTitle;
  favBtn.setAttribute("aria-label", favTitle);
  favBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleFavorite(photo);
  });
  card.appendChild(favBtn);
}

function buildNewCard() {
  // Card listeners are bound once per card *element* and read the photo
  // identity from the card's data-id at event time, so pooled cards can be
  // reused for any photo without rebinding. Hover-preview eligibility is
  // therefore checked inside the timer, not at bind time.
  const card = document.createElement("div");
  card.className = "photo-card";

  card.addEventListener("click", function () {
    const idx = photoIndexById.get(Number(card.dataset.id));
    if (idx !== undefined) {
      openModal(idx);
    }
  });

  card.addEventListener("mouseenter", function () {
    card.__hoverTimer = setTimeout(function () {
      const photo = photoById.get(Number(card.dataset.id));
      const eligible = photo &&
        ((photo.is_live_photo && photo.live_photo_mov) || photo.is_motion_photo);
      if (!eligible) return;
      const existing = card.querySelector("video");
      if (existing) {
        existing.play().catch(function () {});
        return;
      }
      if (!card.hasAttribute("data-rendered")) return;
      const video = document.createElement("video");
      video.muted = true;
      video.loop = true;
      video.playsInline = true;
      video.src = "/api/photos/" + photo.id +
        (photo.is_motion_photo ? "/motion-video" : "/live-video");
      video.play().catch(function () {});
      card.appendChild(video);
    }, 200);
  });

  card.addEventListener("mouseleave", function () {
    if (card.__hoverTimer) {
      clearTimeout(card.__hoverTimer);
      card.__hoverTimer = null;
    }
    const video = card.querySelector("video");
    if (video) {
      video.pause();
      video.removeAttribute("src");
      video.load();
      video.remove();
    }
  });

  return card;
}

function createPhotoCard(photo) {
  const card = cardPool.pop() || buildNewCard();
  card.setAttribute("data-id", photo.id);

  if (galleryObserver) {
    galleryObserver.observe(card);
    if (farObserver) {
      farObserver.observe(card);
    }
  } else {
    renderCardContent(card, photo);
  }

  return card;
}

function groupByDate(photos) {
  const groups = {};
  for (const photo of photos) {
    const date = photo.taken_at ? photo.taken_at.split("T")[0] : t("unknown_date");
    if (!groups[date]) {
      groups[date] = [];
    }
    groups[date].push(photo);
  }
  return groups;
}

function getNewPhotos() {
  const start = (state.currentPage - 1) * state.perPage;
  return state.photos.slice(start);
}

export function renderGallery(append) {
  const gallery = $("gallery");
  if (!append) {
    gallery.replaceChildren();
    if (galleryObserver) galleryObserver.disconnect();
    initVirtualization();
  }

  if (state.photos.length === 0) {
    const isFav = state.activeFilter === "favorites";
    const emptyEl = isFav
      ? createEmptyState("⭐", t("no_favorites"), t("no_favorites_hint"))
      : createEmptyState("📷", t("no_photos"), t("adjust_filters"));
    gallery.appendChild(emptyEl);
    return;
  }

  const groups = groupByDate(append ? getNewPhotos() : state.photos);

  // Batch inserts: new date groups and their cards accumulate in fragments
  // and attach once, so a full render causes one layout pass instead of a
  // reflow per card. For big rebuilds (>500 cards) the staggered fadeIn is
  // skipped outright — inline animation:none on those cards; a container
  // class removed on the next frame would restart the animation on all of
  // them at once (CSS treats a none->fadeIn change as a brand-new
  // animation), which is the flash this batch is meant to prevent.
  const bulk = !append && state.photos.length > 500;
  const newGroups = document.createDocumentFragment();

  for (const [date, photos] of Object.entries(groups)) {
    let groupEl = append ? gallery.querySelector(`[data-date="${CSS.escape(date)}"]`) : null;
    let gridEl;

    if (!groupEl) {
      groupEl = document.createElement("div");
      groupEl.className = "gallery-date-group";
      groupEl.setAttribute("data-date", date);

      const label = document.createElement("div");
      label.className = "gallery-date-label";

      let locText = "";
      const locationSets = {};
      for (const photo of photos) {
          if (!photo.display_location) continue;
          const partsDisp = photo.display_location.split(", ");
          if (partsDisp.length < 2) continue;

          let country = partsDisp[partsDisp.length - 1].trim();
          let city = partsDisp[0];
          if (city.includes(" (")) city = city.split(" (")[0];
          if (city.includes(" / ")) city = city.split(" / ")[0];
          if (state.language === "zh" && (city.toLowerCase() === "zuerich" || city === "Zurich")) {
              city = "苏黎世";
          } else if (state.language === "en" && (city.toLowerCase() === "zuerich" || city === "苏黎世")) {
              city = "Zurich";
          }
          city = city.trim();

          if (!locationSets[country]) locationSets[country] = new Set();
          locationSets[country].add(city);
      }

      const locStrings = [];
      for (const [country, citiesSet] of Object.entries(locationSets)) {
          const cities = Array.from(citiesSet);
          if (cities.length > 0) {
              locStrings.push(`${country} (${cities.join(", ")})`);
          } else {
              locStrings.push(country);
          }
      }
      if (locStrings.length > 0) {
          locText = " - " + locStrings.join(" | ");
      }

      label.textContent = formatDateLabel(date) + locText;
      groupEl.appendChild(label);

      gridEl = document.createElement("div");
      gridEl.className = "gallery-grid";
      groupEl.appendChild(gridEl);

      newGroups.appendChild(groupEl);
    } else {
      gridEl = groupEl.querySelector(".gallery-grid");
    }

    const cards = document.createDocumentFragment();
    for (const photo of photos) {
      const card = createPhotoCard(photo);
      if (bulk) card.style.animation = "none";
      cards.appendChild(card);
    }
    gridEl.appendChild(cards);
  }

  if (newGroups.childElementCount > 0) {
    gallery.appendChild(newGroups);
  }
}

export function updateLoadMoreButton() {
  const loadMoreContainer = $("load-more-container");
  if (state.currentPage < state.totalPages) {
    loadMoreContainer.style.display = "";
  } else {
    loadMoreContainer.style.display = "none";
  }
}

export async function loadPhotos(append) {
  if (state.loading) return;
  state.loading = true;

  const gallery = $("gallery");
  if (!append) {
    gallery.replaceChildren();
    const loadingEl = document.createElement("div");
    loadingEl.className = "gallery-loading";
    loadingEl.style.padding = "var(--space-2xl)";
    loadingEl.style.textAlign = "center";
    loadingEl.style.color = "var(--text-muted)";
    loadingEl.textContent = "⏳ " + t("loading");
    gallery.appendChild(loadingEl);
  }

  const params = new URLSearchParams();
  params.set("page", state.currentPage.toString());
  params.set("per_page", state.perPage.toString());

  if (state.activeFilter !== "all") {
    if (state.activeFilter === "favorites") {
      params.set("favorites", "true");
      params.set("category", "favorites");
    } else if (state.activeFilter === "photos") {
      params.set("category", "photos");
    } else if (state.activeFilter === "videos") {
      params.set("category", "videos");
    } else if (state.activeFilter === "screenshots" || state.activeFilter === "PNG") {
      params.set("screenshots", "true");
    } else if (
      state.activeFilter.includes("photos") ||
      state.activeFilter.includes("videos") ||
      state.activeFilter.includes("screenshots")
    ) {
      params.set("category", state.activeFilter);
    } else {
      params.set("file_type", state.activeFilter);
    }
  }

  if (state.activeLocation) params.set("location", state.activeLocation);
  if (state.activeCountry) params.set("country", state.activeCountry);
  if (state.activeCity) params.set("city", state.activeCity);
  if (state.dateFrom) params.set("date_from", state.dateFrom + "T00:00:00");
  if (state.dateTo) params.set("date_to", state.dateTo + "T23:59:59");
  if (state.language) params.set("lang", state.language);
  if (state.sortOrder) params.set("sort_order", state.sortOrder);

  try {
    const data = await api("/api/photos?" + params.toString());
    if (append) {
      state.photos = state.photos.concat(data.photos);
    } else {
      state.photos = data.photos;
    }
    rebuildPhotoIndex();
    state.total = data.total;
    state.totalPages = data.pages;

    renderGallery(append);
    updateLoadMoreButton();
  } catch (e) {
    if (append) {
      // Roll the page counter back so the failed page is retried
      // instead of being silently skipped.
      state.currentPage = Math.max(1, state.currentPage - 1);
    } else {
      gallery.replaceChildren();
      const emptyEl = createEmptyState("⚠️", t("failed_load"), t("try_again"));
      gallery.appendChild(emptyEl);
    }
  }

  state.loading = false;
}

export function resetAndReload() {
  state.currentPage = 1;
  state.photos = [];
  loadPhotos(false);
}

export async function requestNextPage() {
  // Single entry point for advancing a page: guards loading state and the
  // last-page boundary so concurrent callers (infinite scroll, modal
  // navigation, slideshow, load-more button) can't double-increment.
  if (state.loading) return false;
  if (state.currentPage >= state.totalPages) return false;
  state.currentPage++;
  await loadPhotos(true);
  return true;
}

export function setupInfiniteScroll() {
  const galleryContainer = $("gallery-container");
  galleryContainer.addEventListener("scroll", function () {
    const scrollBottom = galleryContainer.scrollHeight - galleryContainer.scrollTop - galleryContainer.clientHeight;
    if (scrollBottom < 800) { // increased threshold slightly for smoother scroll
      requestNextPage();
    }
  });
}

export function syncFilterUI() {
  // Reflect state (filters, dates, sort) back into the header controls.
  // Used after the slideshow config rewrites state behind their back.
  document.querySelectorAll(".filter-group .filter-btn").forEach(function (btn) {
    btn.classList.toggle("active", btn.dataset.filter === state.activeFilter);
  });

  const dateFromInput = $("date-from");
  const dateToInput = $("date-to");
  if (dateFromInput) dateFromInput.value = state.dateFrom || "";
  if (dateToInput) dateToInput.value = state.dateTo || "";

  const sortToggle = $("sort-toggle");
  if (sortToggle) sortToggle.classList.toggle("active", state.sortOrder === "asc");
}

// One toggle -> one "favorite-changed" event -> every view updates itself.
// The card button state lives here; the modal keeps its own subscriber.
document.addEventListener("favorite-changed", function (e) {
  const { photoId, isFavorite } = e.detail;
  const card = document.querySelector(`.photo-card[data-id="${photoId}"]`);

  if (card) {
    const favBtn = card.querySelector(".photo-card-fav-btn");
    if (favBtn) {
      favBtn.classList.toggle("active", isFavorite);
      favBtn.innerHTML = isFavorite ? "★" : "☆";
      const label = isFavorite ? t("unfavorite_btn") : t("favorite_btn");
      favBtn.title = label;
      favBtn.setAttribute("aria-label", label);
    }
  }

  // If currently filtered by favorites and unfavorited, remove the photo
  // from the view. Its card may be pooled behind a lightweight placeholder
  // if it is far off-screen — remove that too, and drop the recycled copy
  // from the pool by leaving it unclaimed (a stale pool entry's identity is
  // replaced whenever the pool hands it to another photo).
  if (state.activeFilter === "favorites" && !isFavorite) {
    const placeholder = document.querySelector(
      `.photo-card-placeholder[data-recycle-id="${photoId}"]`
    );
    if (placeholder) {
      farObserver && farObserver.unobserve(placeholder);
      placeholder.remove();
    }
    if (!card) return;

    card.style.transition = "opacity 0.25s ease, transform 0.25s ease";
    card.style.opacity = "0";
    card.style.transform = "scale(0.9)";
    setTimeout(() => {
      galleryObserver && galleryObserver.unobserve(card);
      farObserver && farObserver.unobserve(card);
      card.remove();
      const gallery = $("gallery");
      // Placeholders stand in for real remaining photos — the view is only
      // empty when neither a card nor a placeholder is left.
      if (gallery && !gallery.querySelector(".photo-card, .photo-card-placeholder")) {
        gallery.replaceChildren(createEmptyState("⭐", t("no_favorites"), t("no_favorites_hint")));
      }
    }, 250);
  }
});
