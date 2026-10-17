// Shared frontend constants. Kept dependency-free so any module can
// import it without creating cycles.

// File types the UI treats as videos everywhere: card badges, modal
// rendering, slideshow wait-for-video logic. One source of truth so a
// new container format only has to be added in one place.
export const VIDEO_TYPES = new Set(["MOV", "MP4", "3GP"]);
