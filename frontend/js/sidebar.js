import { state } from './state.js';
import { api } from './api.js';
import { t } from './i18n.js';
import { formatMonthLabel } from './utils.js';
import { renderGallery, resetAndReload } from './gallery.js';

const sidebarTimeline = document.getElementById("sidebar-timeline");
const sidebarLocations = document.getElementById("sidebar-locations");
const dateFromInput = document.getElementById("date-from");
const dateToInput = document.getElementById("date-to");
const searchInput = document.getElementById("search-input");

let sidebarTimer = null;

export function closeMobileSidebar() {
  const sidebar = document.getElementById("sidebar");
  const sidebarOverlay = document.getElementById("sidebar-overlay");
  if (sidebar && sidebarOverlay) {
    sidebar.classList.remove("open");
    sidebarOverlay.classList.remove("show");
    if (sidebarTimer) clearTimeout(sidebarTimer);
  }
}

export function resetSidebarTimer() {
  if (sidebarTimer) clearTimeout(sidebarTimer);
  sidebarTimer = setTimeout(closeMobileSidebar, 5000);
}

export async function loadTimeline() {
  try {
    const data = await api(`/api/timeline?sort_order=${state.sortOrder || "desc"}`);
    state.timeline = data;
    renderTimeline();
  } catch (e) {
    // Silently fail
  }
}

export function renderTimeline() {
  if (!sidebarTimeline) return;
  sidebarTimeline.replaceChildren();

  const months = {};
  for (const item of state.timeline) {
    if (!item.date) continue;
    const month = item.date.substring(0, 7); 
    if (!months[month]) {
      months[month] = { count: 0, days: [] };
    }
    months[month].count += item.count;
    months[month].days.push(item);
  }

  const allItem = createSidebarItem(t("all_photos"), state.total, function () {
    state.dateFrom = null;
    state.dateTo = null;
    if (dateFromInput) dateFromInput.value = "";
    if (dateToInput) dateToInput.value = "";
    resetAndReload();
    resetSidebarTimer();
    setActiveSidebarItem(sidebarTimeline, allItem);
  });
  if (!state.dateFrom && !state.dateTo) {
      allItem.classList.add("active");
  }
  sidebarTimeline.appendChild(allItem);

  for (const [month, data] of Object.entries(months)) {
    const wrapper = document.createElement("div");
    
    const isExpanded = state.expandedMonths && state.expandedMonths.has(month);
    const label = formatMonthLabel(month);
    
    const item = createSidebarItem(label, data.count, function () {
      if (!state.expandedMonths) state.expandedMonths = new Set();
      if (state.expandedMonths.has(month)) {
        state.expandedMonths.delete(month);
      } else {
        state.expandedMonths.add(month);
      }
      
      const parts = month.split("-");
      const y = parseInt(parts[0], 10);
      const m = parseInt(parts[1], 10);
      const lastDay = new Date(y, m, 0).getDate();
      
      state.dateFrom = `${month}-01`;
      state.dateTo = `${month}-${lastDay}`;
      if (dateFromInput) dateFromInput.value = state.dateFrom;
      if (dateToInput) dateToInput.value = state.dateTo;
      resetAndReload();
      resetSidebarTimer();
      
      renderTimeline();
    });
    
    if (state.dateFrom === `${month}-01` && state.dateTo === `${month}-${new Date(parseInt(month.split("-")[0]), parseInt(month.split("-")[1]), 0).getDate()}`) {
        item.classList.add("active");
    }
    
    wrapper.appendChild(item);
    
    if (isExpanded) {
      const daysContainer = document.createElement("div");
      daysContainer.style.paddingLeft = "15px";
      
      const sortedDays = [...data.days].sort((a, b) => {
        const isAUndet = a.date.endsWith("-99") || a.date.endsWith("-00");
        const isBUndet = b.date.endsWith("-99") || b.date.endsWith("-00");
        if (isAUndet && !isBUndet) return state.sortOrder === "asc" ? 1 : -1;
        if (!isAUndet && isBUndet) return state.sortOrder === "asc" ? -1 : 1;
        return state.sortOrder === "asc" ? a.date.localeCompare(b.date) : b.date.localeCompare(a.date);
      });

      for (const day of sortedDays) {
          const isUndet = day.date.endsWith("-99") || day.date.endsWith("-00");
          const dayLabel = isUndet ? (state.language === "zh" ? "未确定日期" : "Undetermined Date") : day.date;
          const dayItem = createSidebarItem(dayLabel, day.count, function(e) {
             e.stopPropagation();
             state.dateFrom = day.date;
             state.dateTo = day.date;
             if (dateFromInput) dateFromInput.value = isUndet ? "" : day.date;
             if (dateToInput) dateToInput.value = isUndet ? "" : day.date;
             resetAndReload();
             resetSidebarTimer();
             renderTimeline();
          }, { indentLevel: 1 });
          if (state.dateFrom === day.date && state.dateTo === day.date) {
              dayItem.classList.add("active");
          }
          daysContainer.appendChild(dayItem);
      }
      wrapper.appendChild(daysContainer);
    }
    
    sidebarTimeline.appendChild(wrapper);
  }
}

export async function loadLocations() {
  try {
    const data = await api(`/api/locations?lang=${state.language || "zh"}`);
    state.locations = data;
    renderLocations();
  } catch (e) {
    // Silently fail
  }
}

// Picking a place in the sidebar owns the location filter; a leftover
// free-text search would silently narrow the result further, so every
// sidebar selection clears the search box and its state together.
function clearSearch() {
  state.searchQuery = "";
  if (searchInput) searchInput.value = "";
}

export function renderLocations() {
  if (!sidebarLocations) return;
  sidebarLocations.replaceChildren();

  const allItem = createSidebarItem(t("all_locations"), "", function () {
    state.activeLocation = null;
    state.activeCity = null;
    state.activeCountry = null;
    clearSearch();
    resetAndReload();
    setActiveSidebarItem(sidebarLocations, allItem);
  });
  if (!state.activeLocation && !state.activeCountry && !state.activeCity) {
    allItem.classList.add("active");
  }
  sidebarLocations.appendChild(allItem);

  const countries = {};
  for (const loc of state.locations) {
    if (!loc.location_name || !loc.display_location) continue;
    
    const partsEn = loc.location_name.split(", ");
    const countryEn = partsEn[partsEn.length - 1];
    const countryDisp = loc.display_country || (loc.display_location ? loc.display_location.split(", ").pop() : countryEn);

    if (!countries[countryEn]) {
      countries[countryEn] = { display: countryDisp, count: 0, cities: [] };
    }
    countries[countryEn].count += loc.count;
    countries[countryEn].cities.push(loc);
  }
  
  const sortedCountries = Object.entries(countries).sort((a,b)=>b[1].count-a[1].count);
  for (const [countryEn, data] of sortedCountries) {
    const wrapper = document.createElement("div");
    
    const isExpanded = state.expandedCountries && state.expandedCountries.has(countryEn);
    const item = createSidebarItem(data.display, data.count, function (e) {
      if (!state.expandedCountries) state.expandedCountries = new Set();
      if (state.expandedCountries.has(countryEn)) {
        state.expandedCountries.delete(countryEn);
      } else {
        state.expandedCountries.add(countryEn);
      }
      
      state.activeLocation = null;
      state.activeCity = null;
      state.activeCountry = countryEn;
      clearSearch();
      resetAndReload();
      resetSidebarTimer();
      
      renderLocations();
    }, { isExpandable: true, isExpanded: isExpanded });
    
    if (state.activeCountry === countryEn && !state.activeLocation && !state.activeCity) item.classList.add("active");
    wrapper.appendChild(item);
    
    if (isExpanded) {
      const citiesContainer = document.createElement("div");
      citiesContainer.style.paddingLeft = "15px";
      
      const groupedCities = {};
      function cleanCity(name) {
          if (!name) return name;
          let cleaned = name;
          if (cleaned.includes(" (")) cleaned = cleaned.split(" (")[0];
          if (cleaned.includes(" / ")) cleaned = cleaned.split(" / ")[0];
          return cleaned.trim();
      }

      for (const loc of data.cities) {
        const partsEn = loc.location_name.split(", ");
        const partsDisp = loc.display_location.split(", ");
        
        let cityEn = cleanCity(partsEn[0]);
        let cityDisp = loc.display_city || cleanCity(partsDisp[0]);
        if (state.language === "zh" && (cityEn.toLowerCase() === "zuerich" || cityEn === "Zurich")) cityDisp = "苏黎世";
        else if (state.language === "en" && (cityDisp.toLowerCase() === "zuerich" || cityDisp === "苏黎世")) cityDisp = "Zurich";
        
        if (!groupedCities[cityEn]) {
            groupedCities[cityEn] = { display: cityDisp, count: 0 };
        }
        groupedCities[cityEn].count += loc.count;
      }
      
      const sortedCities = Object.entries(groupedCities).sort((a,b)=>b[1].count - a[1].count);
      for (const [cityKey, cityData] of sortedCities) {
        const cityItem = createSidebarItem(cityData.display, cityData.count, function(e) {
           e.stopPropagation();
           state.activeLocation = null;
           state.activeCity = cityKey;
           state.activeCountry = countryEn;
           clearSearch();
           resetAndReload();
           resetSidebarTimer();
           renderLocations();
        });
        if (state.activeCity === cityKey) cityItem.classList.add("active");
        citiesContainer.appendChild(cityItem);
      }
      wrapper.appendChild(citiesContainer);
    }
    sidebarLocations.appendChild(wrapper);
  }
}

function createSidebarItem(label, count, onClick, options = {}) {
  const item = document.createElement("div");
  item.className = "sidebar-item";
  
  const { isExpandable = false, isExpanded = false, indentLevel = 0 } = options;

  if (indentLevel > 0) {
    item.style.paddingLeft = `calc(var(--space-md) + ${indentLevel * 24}px)`;
  }

  const leftWrap = document.createElement("div");
  leftWrap.className = "sidebar-item-label-wrap";

  if (isExpandable) {
    const icon = document.createElement("span");
    icon.className = "sidebar-item-icon";
    if (isExpanded) icon.classList.add("expanded");
    icon.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"></polyline></svg>`;
    leftWrap.appendChild(icon);
  }

  const labelEl = document.createElement("span");
  labelEl.textContent = label;
  leftWrap.appendChild(labelEl);

  item.appendChild(leftWrap);

  if (count !== "" && count != null) {
    const countEl = document.createElement("span");
    countEl.className = "sidebar-item-count";
    countEl.textContent = count.toString();
    item.appendChild(countEl);
  }

  item.addEventListener("click", onClick);
  return item;
}

export function setActiveSidebarItem(container, activeItem) {
  const items = container.querySelectorAll(".sidebar-item");
  items.forEach(function (it) { it.classList.remove("active"); });
  activeItem.classList.add("active");
}
