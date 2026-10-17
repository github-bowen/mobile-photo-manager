import { api } from './api.js';

export async function toggleFavorite(photo) {
  // Toggle favorite state on the server, then broadcast the result so every
  // view (gallery card, modal buttons) updates itself from one place instead
  // of each caller maintaining its own UI-sync path.
  try {
    const res = await api(`/api/photos/${photo.id}/favorite`, { method: "POST" });
    if (res && res.success) {
      photo.is_favorite = res.is_favorite ? 1 : 0;
      document.dispatchEvent(new CustomEvent("favorite-changed", {
        detail: { photoId: photo.id, isFavorite: !!photo.is_favorite }
      }));
      return !!photo.is_favorite;
    }
  } catch (err) {
    console.error("Failed to toggle favorite:", err);
  }
  return null;
}
