import { state } from './state.js';
import { t } from './i18n.js';

export const $ = (id) => document.getElementById(id);

// The database marks "no real capture date" with sentinel strings. Two
// generations exist: the current "-99"/"-00" month/day markers, and the
// oldest rows frozen at "…-01T12:00:00" — indistinguishable from a real
// photo taken at noon on the 1st, but the scanner never produced that
// shape for a genuine date. One shared guard keeps every formatter
// reacting to the same set, so old noon-frozen rows stop rendering as a
// real "January 1st, 12:00:00" and show as undetermined instead.
const UNDETERMINED_MARKERS = ["-99", "-00", "-01T12:00:00"];

export function isUndeterminedDate(iso) {
  if (!iso) return true;
  return UNDETERMINED_MARKERS.some(m => iso.includes(m));
}

const MONTH_NAMES_EN = ["January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December"];

// Label for a sentinel row: keep the parts that are real (the year, and
// the month when it is an actual month — the noon-frozen generation
// kept a real month) and mark the day undetermined. The -99/-00 markers
// carry no real month, so those render as year-only.
function undeterminedDateLabel(iso) {
  const parts = iso.split("T")[0].split("-");
  const year = parts[0];
  const month = parseInt(parts[1], 10);
  const realMonth = month >= 1 && month <= 12;
  if (state.language === "zh") {
    return realMonth ? `${year}年${month}月未确定日期` : `${year}年未确定日期`;
  }
  return realMonth
    ? `${MONTH_NAMES_EN[month - 1]} ${year} (Undetermined Date)`
    : `${year} (Undetermined Date)`;
}

export function formatDateLabel(dateStr) {
  if (!dateStr || dateStr === "Unknown Date" || dateStr === t("unknown_date")) return t("unknown_date");
  if (isUndeterminedDate(dateStr)) return undeterminedDateLabel(dateStr);
  try {
    // Callers normally pass date-only strings; strip any time part so a
    // full ISO value doesn't become "…T14:03:02T12:00:00" -> Invalid Date.
    const d = new Date(dateStr.split("T")[0] + "T12:00:00");
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
  if (isUndeterminedDate(isoStr)) return undeterminedDateLabel(isoStr);
  try {
    const d = new Date(isoStr);
    return d.toLocaleDateString(state.language === "zh" ? "zh-CN" : "en-US", { year: "numeric", month: "long", day: "numeric" });
  } catch (e) {
    return isoStr;
  }
}

export function formatTime(isoStr) {
  if (isUndeterminedDate(isoStr)) return "—";
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
