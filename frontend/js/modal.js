import { state } from './state.js';
import { t } from './i18n.js';
import { $, formatDate, formatTime, formatFileSize, formatDuration } from './utils.js';
import { requestNextPage } from './gallery.js';
import { toggleFavorite } from './favorites.js';

const VIDEO_TYPES = new Set(["MOV", "MP4", "3GP"]);

// Points at the play function of the currently displayed Live/Motion photo so
// the slideshow can trigger playback programmatically (muted, to satisfy
// browser autoplay policy) instead of poking the badge DOM.
let currentPlayLive = null;

export function playCurrentLive(muted) {
  if (typeof currentPlayLive === "function") {
    currentPlayLive({ muted: !!muted });
  }
}

export function openModal(index) {
  if (!state.photos || state.photos.length === 0) return;
  state.modalPhotoIndex = index;
  renderModalContent();
  
  const modalBackdrop = $("modal-backdrop");
  modalBackdrop.classList.add("active");
  
  document.body.style.overflow = "hidden";
}

export function closeModal() {
  state.modalPhotoIndex = -1;
  currentPlayLive = null;
  const modalBackdrop = $("modal-backdrop");
  const modalImageContainer = $("modal-image-container");
  const modalInfo = $("modal-info");

  modalBackdrop.classList.remove("active");
  document.body.style.overflow = "";

  const existing = modalImageContainer.querySelectorAll(".modal-media, img, video");
  existing.forEach(function (el) {
    if (el.tagName === "VIDEO") el.pause();
    el.remove();
  });

  // Rescue toolbar before clearing info
  const toolbar = document.getElementById("modal-autoplay-toolbar");
  if (toolbar) {
      document.body.appendChild(toolbar);
  }

  modalInfo.replaceChildren();
}

export async function nextModalPhoto() {
  if (!state.photos || state.photos.length === 0) return;
  if (state.modalPhotoIndex < state.photos.length - 1) {
    state.modalPhotoIndex++;
    renderModalContent();
  } else if (state.currentPage < state.totalPages) {
    const loaded = await requestNextPage();
    if (loaded && state.modalPhotoIndex < state.photos.length - 1) {
      state.modalPhotoIndex++;
      renderModalContent();
    }
  } else if (state.photos.length > 1) {
    state.modalPhotoIndex = 0;
    renderModalContent();
  }
}

export async function prevModalPhoto() {
  if (!state.photos || state.photos.length === 0) return;
  if (state.modalPhotoIndex > 0) {
    state.modalPhotoIndex--;
    renderModalContent();
  } else if (state.photos.length > 1) {
    state.modalPhotoIndex = state.photos.length - 1;
    renderModalContent();
  }
}

export function renderModalContent() {
  const photo = state.photos[state.modalPhotoIndex];
  if (!photo) return;

  const modalImageContainer = $("modal-image-container");
  const modalPrev = $("modal-prev");
  const modalNext = $("modal-next");

  const existingMedia = modalImageContainer.querySelectorAll(".modal-media, .modal-top-fav-btn");
  existingMedia.forEach(function (el) {
    if (el.tagName === "VIDEO") el.pause();
    el.remove();
  });

  if (VIDEO_TYPES.has(photo.file_type)) {
    const video = document.createElement("video");
    video.className = "modal-media";
    video.controls = true;
    video.autoplay = true;
    video.src = "/api/photos/" + photo.id + "/file";
    modalImageContainer.insertBefore(video, modalPrev);
  } else {
    const wrapper = document.createElement("div");
    wrapper.className = "modal-media";
    wrapper.style.position = "relative";
    wrapper.style.display = "flex";
    wrapper.style.justifyContent = "center";
    wrapper.style.alignItems = "center";
    wrapper.style.width = "100%";
    wrapper.style.height = "100%";

    const img = document.createElement("img");
    img.style.maxWidth = "100%";
    img.style.maxHeight = "100%";
    img.style.objectFit = "contain";
    img.alt = photo.filename || t("photo_alt");
    img.src = state.loadOriginalOnClick 
              ? "/api/photos/" + photo.id + "/render" 
              : "/api/photos/" + photo.id + "/thumbnail/medium";
    wrapper.appendChild(img);

    currentPlayLive = null;
    if (photo.is_live_photo || photo.is_motion_photo) {
      const liveBadge = document.createElement("div");
      liveBadge.id = "modal-live-badge";
      liveBadge.className = "modal-live-badge";
      liveBadge.textContent = t("play_live");
      
      let isPlaying = false;
      const playLive = (opts = {}) => {
          if (isPlaying) return;
          isPlaying = true;
          liveBadge.textContent = t("playing_live");
          liveBadge.classList.add("playing");
          
          const video = document.createElement("video");
          video.style.position = "absolute";
          video.style.top = "0";
          video.style.left = "0";
          video.style.width = "100%";
          video.style.height = "100%";
          video.style.objectFit = "contain";
          video.autoplay = true;
          video.muted = !!opts.muted;
          video.src = "/api/photos/" + photo.id +
            (photo.is_motion_photo ? "/motion-video" : "/live-video");
          
          // addEventListener (not onended=/onerror=) so the slideshow's own
          // handlers in autoplay.js coexist with ours instead of being
          // silently overwritten.
          video.addEventListener("ended", function () {
              liveBadge.textContent = t("play_live");
              liveBadge.classList.remove("playing");
              isPlaying = false;
              video.remove();
              img.style.visibility = "visible";
          });
          video.addEventListener("error", function () {
              liveBadge.textContent = t("error_lbl");
              liveBadge.classList.remove("playing");
              isPlaying = false;
          });
          
          img.style.visibility = "hidden";
          wrapper.appendChild(video);
      };

      currentPlayLive = playLive;
      liveBadge.addEventListener("click", function (e) { e.stopPropagation(); playLive(); });
      wrapper.appendChild(liveBadge);
    }
    
    modalImageContainer.insertBefore(wrapper, modalPrev);
  }

  renderModalInfo(photo);

  const hasMultiple = state.photos.length > 1 || state.currentPage < state.totalPages;
  modalPrev.style.display = hasMultiple ? "flex" : "none";
  modalNext.style.display = hasMultiple ? "flex" : "none";
}

function renderModalInfo(photo) {
  const modalInfo = $("modal-info");
  const modalImageContainer = $("modal-image-container");
  
  // Rescue the toolbar so it doesn't get wiped
  const toolbar = document.getElementById("modal-autoplay-toolbar");
  if (toolbar) {
      document.body.appendChild(toolbar);
  }
  
  modalInfo.replaceChildren();

  const titleContainer = document.createElement("div");
  titleContainer.style.display = "flex";
  titleContainer.style.alignItems = "flex-start";
  titleContainer.style.justifyContent = "space-between";
  titleContainer.style.marginBottom = "var(--space-lg)";

  const title = document.createElement("div");
  title.className = "modal-info-title";
  title.style.wordBreak = "break-all";
  title.textContent = photo.filename || "Untitled";
  titleContainer.appendChild(title);
  modalInfo.appendChild(titleContainer);

  if (toolbar) {
      toolbar.style.display = "flex";
      modalInfo.appendChild(toolbar);
  }

  if (photo.display_location) {
    const loc = document.createElement("div");
    loc.className = "modal-info-location";
    loc.textContent = "📍 " + photo.display_location;
    modalInfo.appendChild(loc);
  }

  if (photo.description) {
    const descriptionSection = createInfoSection(t("description_sec"));
    const description = document.createElement("div");
    description.className = "modal-info-description";
    description.textContent = photo.description;
    descriptionSection.appendChild(description);
    modalInfo.appendChild(descriptionSection);
  }

  if (photo.taken_at) {
    const section = createInfoSection(t("date_time_sec"));
    addInfoRow(section, t("date_lbl"), formatDate(photo.taken_at));
    addInfoRow(section, t("time_lbl"), formatTime(photo.taken_at));
    if (photo.timezone) {
      addInfoRow(section, t("timezone_lbl"), photo.timezone);
    }
    modalInfo.appendChild(section);
  }

  if (photo.camera_make || photo.camera_model || photo.lens_model) {
    const section = createInfoSection(t("camera_sec"));
    if (photo.camera_make) addInfoRow(section, t("make_lbl"), photo.camera_make);
    if (photo.camera_model) addInfoRow(section, t("model_lbl"), photo.camera_model);
    if (photo.lens_model) addInfoRow(section, t("lens_lbl"), photo.lens_model);
    modalInfo.appendChild(section);
  }

  const fileSection = createInfoSection(t("file_sec"));
  addInfoRow(fileSection, t("type_lbl"), photo.file_type);
  addInfoRow(fileSection, t("size_lbl"), formatFileSize(photo.file_size));
  if (photo.width && photo.height) {
    addInfoRow(fileSection, t("dim_lbl"), photo.width + " × " + photo.height);
  }
  if (photo.duration) {
    addInfoRow(fileSection, t("dur_lbl"), formatDuration(photo.duration));
  }
  addInfoRow(fileSection, t("dir_lbl"), photo.directory);
  modalInfo.appendChild(fileSection);

  if (photo.latitude != null && photo.longitude != null) {
    const lat = Number(photo.latitude);
    const lng = Number(photo.longitude);
    if (!isNaN(lat) && !isNaN(lng)) {
      const gpsSection = createInfoSection(t("gps_sec"));
      addInfoRow(gpsSection, t("lat_lbl"), lat.toFixed(6));
      addInfoRow(gpsSection, t("lng_lbl"), lng.toFixed(6));
      if (photo.altitude != null) {
        const alt = Number(photo.altitude);
        if (!isNaN(alt)) {
          addInfoRow(gpsSection, t("alt_lbl"), alt.toFixed(1) + "m");
        }
      }
      modalInfo.appendChild(gpsSection);
    }
  }

  const tagsSection = createInfoSection(t("tags_sec"));
  if (photo.is_live_photo) addInfoRow(tagsSection, t("live_photo_lbl"), t("yes_lbl"));
  if (photo.is_motion_photo) addInfoRow(tagsSection, t("motion_photo_lbl"), t("yes_lbl"));
  if (photo.is_screenshot) addInfoRow(tagsSection, t("screenshot_lbl"), t("yes_lbl"));
  if (photo.is_edited) addInfoRow(tagsSection, t("edited_lbl"), t("yes_lbl"));
  if (photo.is_favorite) addInfoRow(tagsSection, t("favorite_lbl"), t("yes_lbl"));
  if (photo.takeout_metadata) addInfoRow(tagsSection, t("takeout_lbl"), t("yes_lbl"));
  if (tagsSection.childElementCount > 1) {
    modalInfo.appendChild(tagsSection);
  }

  const actionsSection = createInfoSection(t("actions_sec"));

  const topFavBtn = document.createElement("button");
  topFavBtn.className = "modal-top-fav-btn" + (photo.is_favorite ? " active" : "");
  topFavBtn.setAttribute("type", "button");
  const favTitle = photo.is_favorite ? t("unfavorite_btn") : t("favorite_btn");
  topFavBtn.title = favTitle;
  topFavBtn.setAttribute("aria-label", favTitle);
  topFavBtn.innerHTML = photo.is_favorite ? "★" : "☆";

  const favActionBtn = document.createElement("button");
  favActionBtn.className = "filter-btn modal-fav-btn" + (photo.is_favorite ? " active" : "");
  favActionBtn.style.width = "100%";
  favActionBtn.style.marginTop = "8px";
  favActionBtn.style.justifyContent = "center";
  favActionBtn.innerHTML = photo.is_favorite 
    ? `⭐ ${t("unfavorite_btn")}` 
    : `☆ ${t("favorite_btn")}`;

  topFavBtn.onclick = (e) => {
    e.stopPropagation();
    toggleFavorite(photo);
  };
  favActionBtn.onclick = () => toggleFavorite(photo);

  modalImageContainer.appendChild(topFavBtn);
  actionsSection.appendChild(favActionBtn);

  const viewOriginalBtn = document.createElement("button");
  viewOriginalBtn.className = "filter-btn";
  viewOriginalBtn.style.width = "100%";
  viewOriginalBtn.style.marginTop = "8px";
  viewOriginalBtn.style.justifyContent = "center";
  viewOriginalBtn.style.background = "var(--accent-gradient)";
  viewOriginalBtn.style.color = "#fff";
  viewOriginalBtn.style.border = "none";
  viewOriginalBtn.textContent = t("view_original");
  viewOriginalBtn.onclick = function() {
      if (VIDEO_TYPES.has(photo.file_type)) {
           window.open("/api/photos/" + photo.id + "/file", "_blank");
      } else {
           if (state.loadOriginalOnClick) {
               window.open("/api/photos/" + photo.id + "/file", "_blank");
               return;
           }
           viewOriginalBtn.textContent = t("loading");
           viewOriginalBtn.disabled = true;
           viewOriginalBtn.style.opacity = "0.7";
           
           const imgElement = modalImageContainer.querySelector("img");
           if (imgElement) {
               const newImg = new Image();
               newImg.onload = () => {
                   imgElement.src = newImg.src;
                   viewOriginalBtn.textContent = t("view_original") + t("original_loaded");
               };
               newImg.onerror = () => {
                   viewOriginalBtn.textContent = t("load_error");
                   viewOriginalBtn.disabled = false;
                   viewOriginalBtn.style.opacity = "1";
               };
               newImg.src = "/api/photos/" + photo.id + "/render";
           }
      }
  };
  actionsSection.appendChild(viewOriginalBtn);

  const downloadBtn = document.createElement("button");
  downloadBtn.className = "filter-btn";
  downloadBtn.style.width = "100%";
  downloadBtn.style.marginTop = "8px";
  downloadBtn.style.justifyContent = "center";
  downloadBtn.textContent = "⬇ " + t("download_btn");
  downloadBtn.onclick = function() {
      // Content-Disposition: attachment makes the anchor click a save
      // instead of a navigation, so the modal stays exactly where it is.
      const a = document.createElement("a");
      a.href = "/api/photos/" + photo.id + "/file?download=true";
      document.body.appendChild(a);
      a.click();
      a.remove();
  };
  actionsSection.appendChild(downloadBtn);
  modalInfo.appendChild(actionsSection);
}

function createInfoSection(titleText) {
  const section = document.createElement("div");
  section.className = "modal-info-section";

  const title = document.createElement("div");
  title.className = "modal-info-section-title";
  title.textContent = titleText;
  section.appendChild(title);

  return section;
}

function addInfoRow(section, label, value) {
  const row = document.createElement("div");
  row.className = "modal-info-row";

  const labelEl = document.createElement("span");
  labelEl.className = "modal-info-label";
  labelEl.textContent = label;
  row.appendChild(labelEl);

  const valueEl = document.createElement("span");
  valueEl.className = "modal-info-value";
  valueEl.textContent = value || "—";
  row.appendChild(valueEl);

  section.appendChild(row);
}

// Keep the modal's favorite buttons in sync when the state is toggled
// anywhere (card button or the modal itself) — see favorites.js.
document.addEventListener("favorite-changed", function (e) {
  const current = state.photos[state.modalPhotoIndex];
  if (!current || current.id !== e.detail.photoId) return;
  const isFavorite = e.detail.isFavorite;

  const topFavBtn = document.querySelector(".modal-top-fav-btn");
  if (topFavBtn) {
    topFavBtn.classList.toggle("active", isFavorite);
    topFavBtn.innerHTML = isFavorite ? "★" : "☆";
    const title = isFavorite ? t("unfavorite_btn") : t("favorite_btn");
    topFavBtn.title = title;
    topFavBtn.setAttribute("aria-label", title);
  }

  const favActionBtn = document.querySelector(".modal-fav-btn");
  if (favActionBtn) {
    favActionBtn.classList.toggle("active", isFavorite);
    favActionBtn.innerHTML = isFavorite
      ? `⭐ ${t("unfavorite_btn")}`
      : `☆ ${t("favorite_btn")}`;
  }
});

let modalEventsInitialized = false;
export function setupModalEvents() {
  if (modalEventsInitialized) return;
  modalEventsInitialized = true;

  const modalClose = $("modal-close");
  const modalPrev = $("modal-prev");
  const modalNext = $("modal-next");
  const modalBackdrop = $("modal-backdrop");
  const modalImageContainer = $("modal-image-container");

  if (modalClose) {
    modalClose.addEventListener("click", function (e) {
      e.stopPropagation();
      closeModal();
    });
  }

  if (modalPrev) {
    modalPrev.addEventListener("click", function (e) {
      e.stopPropagation();
      prevModalPhoto();
    });
  }

  if (modalNext) {
    modalNext.addEventListener("click", function (e) {
      e.stopPropagation();
      nextModalPhoto();
    });
  }

  if (modalBackdrop) {
    modalBackdrop.addEventListener("click", function (e) {
      if (e.target === modalBackdrop || e.target === modalImageContainer) {
        closeModal();
      }
    });
  }

  document.addEventListener("keydown", function (e) {
    if (state.modalPhotoIndex < 0) return;
    if (e.key === "Escape") {
      closeModal();
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      prevModalPhoto();
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      nextModalPhoto();
    }
  });

  let wheelTimeout = null;
  if (modalImageContainer) {
    modalImageContainer.addEventListener("wheel", function (e) {
      if (state.modalPhotoIndex < 0) return;
      e.preventDefault();
      if (wheelTimeout) return;
      wheelTimeout = setTimeout(() => { wheelTimeout = null; }, 350);

      if (e.deltaY > 0 || e.deltaX > 0) {
        nextModalPhoto();
      } else if (e.deltaY < 0 || e.deltaX < 0) {
        prevModalPhoto();
      }
    }, { passive: false });

    let touchStartX = 0;
    let touchStartY = 0;
    modalImageContainer.addEventListener("touchstart", function (e) {
      if (e.touches && e.touches.length === 1) {
        touchStartX = e.touches[0].clientX;
        touchStartY = e.touches[0].clientY;
      }
    }, { passive: true });

    modalImageContainer.addEventListener("touchend", function (e) {
      if (state.modalPhotoIndex < 0) return;
      if (e.changedTouches && e.changedTouches.length === 1) {
        const touchEndX = e.changedTouches[0].clientX;
        const touchEndY = e.changedTouches[0].clientY;
        const deltaX = touchEndX - touchStartX;
        const deltaY = touchEndY - touchStartY;
        if (Math.abs(deltaX) > 40 && Math.abs(deltaX) > Math.abs(deltaY) * 1.2) {
          if (deltaX < 0) {
            nextModalPhoto();
          } else {
            prevModalPhoto();
          }
        }
      }
    }, { passive: true });
  }
}

