import { state } from './state.js';
import { $, createEmptyState } from './utils.js';
import { loadPhotos, requestNextPage, syncFilterUI } from './gallery.js';
import { openModal, renderModalContent, playCurrentLive } from './modal.js';
import { t } from './i18n.js';
import { VIDEO_TYPES } from './constants.js';

let autoplayTimer = null;
export let isAutoplaying = false;

export function initAutoplay() {
  const btn = $('autoplay-btn');
  const modal = $('autoplay-config-modal');
  const cancelBtn = $('autoplay-cancel-btn');
  const startBtn = $('autoplay-start-btn');
  const toggleBtn = $('modal-autoplay-toggle');
  const durationSlider = $('modal-autoplay-duration');

  // Move modal to document.body so it's not affected by any ancestor's
  // backdrop-filter / stacking-context that could clip position:fixed children
  if (modal && modal.parentElement !== document.body) {
    document.body.appendChild(modal);
  }

  if (btn) {
    btn.addEventListener('click', function(event) {
      openAutoplayConfig(event);
    });
  }
  
  if (cancelBtn) {
    cancelBtn.addEventListener('click', () => {
      modal.classList.remove('active');
    });
  }

  // Esc closes the config dialog, and opening it moves focus in — a
  // keyboard user otherwise lands in a dialog their Tab key can't see.
  // (The photo modal's Escape handler stays inert while the config modal
  // is up because the photo modal itself is closed at that point.)
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && modal.classList.contains('active')) {
      modal.classList.remove('active');
    }
  });
  
  if (startBtn) {
    startBtn.addEventListener('click', startAutoplay);
  }

  if (toggleBtn) {
    toggleBtn.addEventListener('click', () => {
      if (isAutoplaying) stopAutoplay();
      else startAutoplayFromCurrent();
    });
  }

  if (durationSlider) {
    const durVal = $('modal-autoplay-dur-val');
    durationSlider.addEventListener('input', () => {
      if (durVal) durVal.textContent = durationSlider.value + "s";
    });
  }

  // Remove the old 'change' event listener that resets the slideshow.
  // The user just wants to change the playback direction on the fly without reloading.
}

function openAutoplayConfig() {
  const modal = $('autoplay-config-modal');
  if (!modal) {
    console.error('Slideshow config modal element not found');
    return;
  }
  
  // Pre-fill dates
  $('autoplay-date-from').value = state.dateFrom || "";
  $('autoplay-date-to').value = state.dateTo || "";

  // Pre-fill locations by country
  const locContainer = $('autoplay-location-container');
  if (locContainer) {
    locContainer.innerHTML = '';
    
    // Aggregate locations by country using raw_country (English) as key for filtering
    // but display the translated display_country to the user
    const countries = {}; // key: raw_country, value: { displayName, count }
    for (const loc of state.locations) {
      const rawCountry = loc.raw_country || loc.display_country || "Unknown";
      const displayCountry = loc.display_country || rawCountry;
      if (!countries[rawCountry]) countries[rawCountry] = { displayName: displayCountry, count: 0 };
      countries[rawCountry].count += loc.count;
    }
    
    // Convert to array and sort by count descending
    const countryArray = Object.entries(countries)
      .map(([rawName, info]) => ({ rawName, displayName: info.displayName, count: info.count }))
      .sort((a, b) => b.count - a.count);
    
    // Check which countries are currently active (activeCountry stores raw English names)
    const activeCountries = state.activeCountry ? state.activeCountry.split(',') : [];
    
    for (const c of countryArray) {
      const label = document.createElement('label');
      label.className = 'autoplay-checkbox-label';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.className = 'autoplay-country-cb';
      cb.value = c.rawName; // Use raw English name as value so API filter matches DB
      if (activeCountries.includes(c.rawName)) {
        cb.checked = true;
      }
      label.appendChild(cb);
      label.appendChild(document.createTextNode(` ${c.displayName} (${c.count})`));
      locContainer.appendChild(label);
    }
  }

  // Pre-fill type based on current active filter
  const isAll = state.activeFilter === "all";
  const photoTypes = ["HEIC", "HEIF", "JPG", "WEBP", "AVIF", "photos"];
  const videoTypes = [...VIDEO_TYPES, "videos"];
  $('autoplay-type-photo').checked = isAll || state.activeFilter === "photos" || photoTypes.some(type => state.activeFilter.includes(type));
  $('autoplay-type-video').checked = isAll || state.activeFilter === "videos" || videoTypes.some(type => state.activeFilter.includes(type));
  $('autoplay-type-screenshot').checked = isAll || state.activeFilter === "screenshots" || state.activeFilter.includes("PNG") || state.activeFilter === "PNG";

  // Pre-fill sort order
  const sortOrderDesc = $('autoplay-sort-desc');
  const sortOrderAsc = $('autoplay-sort-asc');
  if (state.sortOrder === "asc" && sortOrderAsc) {
    sortOrderAsc.checked = true;
  } else if (sortOrderDesc) {
    sortOrderDesc.checked = true;
  }

  // Hook up duration slider display
  const durSlider = $('autoplay-duration');
  const durDisplay = $('autoplay-duration-display');
  if (durSlider && durDisplay) {
    durSlider.oninput = () => { durDisplay.textContent = durSlider.value + 's'; };
  }

  modal.classList.add('active');

  // Give keyboard users a starting point inside the dialog.
  const cancelBtn = $('autoplay-cancel-btn');
  if (cancelBtn) cancelBtn.focus();

}

async function startAutoplay() {
  $('autoplay-config-modal').classList.remove('active');
  
  // Update state filters
  const dateFrom = $('autoplay-date-from').value;
  const dateTo = $('autoplay-date-to').value;
  state.dateFrom = dateFrom ? dateFrom : null;
  state.dateTo = dateTo ? dateTo : null;
  
  const countryCheckboxes = document.querySelectorAll('.autoplay-country-cb:checked');
  const countries = Array.from(countryCheckboxes).map(cb => cb.value);
  state.activeCountry = countries.length > 0 ? countries.join(',') : null;
  state.activeLocation = null; // Clear exact location filter to avoid conflicts
  // The config modal owns the full filter set; a leftover free-text search
  // would narrow the slideshow beyond what its checkboxes show.
  state.searchQuery = "";
  
  const wantsPhoto = $('autoplay-type-photo').checked;
  const wantsVideo = $('autoplay-type-video').checked;
  const wantsScreenshot = $('autoplay-type-screenshot').checked;
  
  const sortOrderDesc = $('autoplay-sort-desc');
  const sortOrderAsc = $('autoplay-sort-asc');
  if (sortOrderDesc && sortOrderDesc.checked) {
    state.sortOrder = "desc";
  } else if (sortOrderAsc && sortOrderAsc.checked) {
    state.sortOrder = "asc";
  }
  
  if (wantsPhoto && wantsVideo && wantsScreenshot) {
     state.activeFilter = "all";
  } else if (wantsPhoto && !wantsVideo && !wantsScreenshot) {
     state.activeFilter = "photos";
  } else if (wantsVideo && !wantsPhoto && !wantsScreenshot) {
     state.activeFilter = "videos";
  } else if (wantsScreenshot && !wantsPhoto && !wantsVideo) {
     state.activeFilter = "screenshots";
  } else {
     let types = [];
     if (wantsPhoto) types.push("photos");
     if (wantsVideo) types.push("videos");
     if (wantsScreenshot) types.push("screenshots");
     if (types.length === 0) types = ["NONE"];
     state.activeFilter = types.join(",");
  }
  
  // Reset gallery to match these new filters
  state.currentPage = 1;
  state.photos = [];
  const gallery = $('gallery');
  if (gallery) {
      gallery.replaceChildren();
      const loadingEl = document.createElement("div");
      loadingEl.style.padding = "40px";
      loadingEl.style.textAlign = "center";
      loadingEl.style.color = "var(--text-muted)";
      loadingEl.textContent = t("preparing_slideshow");
      gallery.appendChild(loadingEl);
  }
  
  isAutoplaying = true;
  const toggleBtn = $('modal-autoplay-toggle');
  if (toggleBtn) toggleBtn.textContent = '⏸';
  
  // Load first page
  await loadPhotos(false);
  
  if (state.photos.length > 0) {
    openModal(0);
    autoplayLoop();
  } else {
    stopAutoplay();
    const gallery = $('gallery');
    if (gallery) {
      gallery.replaceChildren(createEmptyState("🔍", t("no_photos_criteria"), t("adjust_filters")));
    }
  }

  // The config above rewrote state.dateFrom/dateTo/sort/filter behind the
  // header UI's back — reflect the new values into the controls.
  syncFilterUI();
}

async function autoplayLoop() {
  if (autoplayTimer) clearTimeout(autoplayTimer);
  if (!isAutoplaying) return;
  if (state.modalPhotoIndex === -1) {
    stopAutoplay();
    return;
  }
  
  const dirEl = $('modal-autoplay-direction');
  const direction = dirEl ? dirEl.value : "forward";
  
  if (direction === "forward") {
    if (state.modalPhotoIndex >= state.photos.length - 1) {
      // Reached the end of current page
      if (state.currentPage < state.totalPages) {
        await requestNextPage();
      } else {
        // Reached the end of all photos
        stopAutoplay();
        return;
      }
    }
  } else {
    // If we are at index 0 and going backwards, stop.
    if (state.modalPhotoIndex <= 0) {
      stopAutoplay();
      return;
    }
  }
  
  const photo = state.photos[state.modalPhotoIndex];
  if (!photo) {
    stopAutoplay();
    return;
  }
  
  // Read from modal specific controls if they exist, otherwise fallback to main config
  const waitVideoModal = $('modal-autoplay-wait-video');
  const durationModal = $('modal-autoplay-duration');
  
  const waitVideo = waitVideoModal ? waitVideoModal.checked : $('autoplay-wait-video').checked;
  const durationStr = durationModal ? durationModal.value : $('autoplay-duration').value;
  const durationMs = (parseInt(durationStr) || 3) * 1000;
  
  let isVideoOrLive = VIDEO_TYPES.has(photo.file_type) ||
    photo.is_live_photo || photo.is_motion_photo;
  
  if (waitVideo && isVideoOrLive) {
    // We need to wait for the video to play
    // It takes a bit for the video element to be injected by modal.js
    setTimeout(() => {
      if (!isAutoplaying) return;
      const modalMedia = document.querySelectorAll('.modal-media');
      let videoEl = Array.from(modalMedia).find(el => el.tagName === "VIDEO");
      
      if (!videoEl && (photo.is_live_photo || photo.is_motion_photo)) {
         // Programmatic Live/Motion playback: muted so browser autoplay
         // policy allows it.
         playCurrentLive(true);
         // Give it a moment to inject video
         setTimeout(() => {
            const newMedia = document.querySelectorAll('.modal-media, video');
            videoEl = Array.from(newMedia).find(el => el.tagName === "VIDEO");
            waitForVideo(videoEl, durationMs, photo.id);
         }, 300);
      } else {
         waitForVideo(videoEl, durationMs, photo.id);
      }
    }, 200);
  } else {
    // Standard delay
    autoplayTimer = setTimeout(nextSlide, durationMs);
  }
}

function isCurrentSlide(photoId) {
    const current = state.photos[state.modalPhotoIndex];
    return !!current && current.id === photoId && isAutoplaying;
}

function waitForVideo(videoEl, fallbackMs, slidePhotoId) {
    if (autoplayTimer) clearTimeout(autoplayTimer);
    if (videoEl) {
        // Ensure it is playing. If the browser blocks the play() call, fall
        // back to a timed advance so the slideshow never stalls on it.
        if (videoEl.paused) {
            videoEl.play().catch(() => {
                if (isCurrentSlide(slidePhotoId)) {
                    autoplayTimer = setTimeout(nextSlide, fallbackMs);
                }
            });
        }
        // addEventListener (not onended=) so modal.js's own handlers
        // survive; advance only if this video is still the current slide.
        videoEl.addEventListener("ended", function () {
            if (!isCurrentSlide(slidePhotoId)) return;
            if (autoplayTimer) clearTimeout(autoplayTimer);
            autoplayTimer = setTimeout(nextSlide, 500); // 500ms pause after video
        });
        videoEl.addEventListener("error", function () {
            if (!isCurrentSlide(slidePhotoId)) return;
            if (autoplayTimer) clearTimeout(autoplayTimer);
            autoplayTimer = setTimeout(nextSlide, fallbackMs);
        });
    } else {
        autoplayTimer = setTimeout(nextSlide, fallbackMs);
    }
}

function nextSlide() {
  if (!isAutoplaying) return;
  
  const dirEl = $('modal-autoplay-direction');
  const direction = dirEl ? dirEl.value : "forward";
  
  if (direction === "forward") {
    if (state.modalPhotoIndex < state.photos.length - 1) {
      state.modalPhotoIndex++;
      renderModalContent();
      autoplayLoop();
    } else {
      // Need to load more, let loop handle it
      autoplayLoop();
    }
  } else {
    if (state.modalPhotoIndex > 0) {
      state.modalPhotoIndex--;
      renderModalContent();
      autoplayLoop();
    } else {
      stopAutoplay();
    }
  }
}

export function stopAutoplay() {
  isAutoplaying = false;
  if (autoplayTimer) clearTimeout(autoplayTimer);
  const toggleBtn = $('modal-autoplay-toggle');
  if (toggleBtn) toggleBtn.textContent = '▶';
}

export function startAutoplayFromCurrent() {
  isAutoplaying = true;
  const toggleBtn = $('modal-autoplay-toggle');
  if (toggleBtn) toggleBtn.textContent = '⏸';
  autoplayLoop();
}
