import { state } from './state.js';
import { t } from './i18n.js';

export const $ = (id) => document.getElementById(id);

export function formatDateLabel(dateStr) {
  if (!dateStr || dateStr === "Unknown Date" || dateStr === t("unknown_date")) return t("unknown_date");
  if (dateStr.endsWith("-99") || dateStr.includes("-99") || dateStr.endsWith("-00") || dateStr.includes("-00")) {
    const parts = dateStr.split("T")[0].split("-");
    const year = parts[0];
    const month = parseInt(parts[1], 10);
    const monthFormatted = month < 10 ? '0' + month : month;
    if (state.language === "zh") {
      return `${year}年${monthFormatted}月未确定日期`;
    } else {
      const monthNames = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
      const monthName = monthNames[month - 1] || `${month}`;
      return `${monthName} ${year} (Undetermined Date)`;
    }
  }
  try {
    const d = new Date(dateStr + "T12:00:00");
    return d.toLocaleDateString(state.language === "zh" ? "zh-CN" : "en-US", {
      year: "numeric", month: "long", day: "numeric", weekday: "long"
    });
  } catch (e) {
    return dateStr;
  }
}

export function formatMonthLabel(monthStr) {
  try {
    const d = new Date(monthStr + "-01T12:00:00");
    return d.toLocaleDateString(state.language === "zh" ? "zh-CN" : "en-US", { year: "numeric", month: "long" });
  } catch (e) {
    return monthStr;
  }
}

export function formatDate(isoStr) {
  if (!isoStr) return "—";
  if (isoStr.includes("-99") || isoStr.includes("-00")) {
    const parts = isoStr.split("T")[0].split("-");
    const year = parts[0];
    const month = parseInt(parts[1], 10);
    const monthFormatted = month < 10 ? '0' + month : month;
    if (state.language === "zh") {
      return `${year}年${monthFormatted}月未确定日期`;
    } else {
      const monthNames = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
      const monthName = monthNames[month - 1] || `${month}`;
      return `${monthName} ${year} (Undetermined Date)`;
    }
  }
  try {
    const d = new Date(isoStr);
    return d.toLocaleDateString(state.language === "zh" ? "zh-CN" : "en-US", { year: "numeric", month: "long", day: "numeric" });
  } catch (e) {
    return isoStr;
  }
}

export function formatTime(isoStr) {
  if (!isoStr || isoStr.includes("-99") || isoStr.includes("-00")) return "—";
  try {
    const d = new Date(isoStr);
    return d.toLocaleTimeString(state.language === "zh" ? "zh-CN" : "en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch (e) {
    return isoStr;
  }
}

export function formatFileSize(bytes) {
  if (!bytes) return "—";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

export function formatDuration(seconds) {
  if (!seconds) return "—";
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  if (m > 0) return m + ":" + s.toString().padStart(2, "0");
  return s + "s";
}

export function updateStats() {
  const sidebarStats = $("sidebar-stats");
  if (sidebarStats) {
    sidebarStats.textContent = state.total + " " + t("stats_photos") + " · " +
      state.locations.length + " " + t("stats_locations");
  }
}

export function createEmptyState(icon, text, subtext) {
  const el = document.createElement("div");
  el.className = "empty-state";

  const iconEl = document.createElement("div");
  iconEl.className = "empty-state-icon";
  iconEl.textContent = icon;
  el.appendChild(iconEl);

  const textEl = document.createElement("div");
  textEl.className = "empty-state-text";
  textEl.textContent = text;
  el.appendChild(textEl);

  const subEl = document.createElement("div");
  subEl.className = "empty-state-sub";
  subEl.textContent = subtext;
  el.appendChild(subEl);

  return el;
}

// The header toggle pills are div-based switches (role="switch"). This is
// the single place that keeps their visual .active class and their
// aria-checked attribute in agreement, so keyboard and screen-reader users
// see the same state as everyone else.
export function setSwitchState(el, on) {
  if (!el) return;
  el.classList.toggle("active", !!on);
  el.setAttribute("aria-checked", String(!!on));
}
