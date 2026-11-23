// YouTube URL -> 11-char video id. Same URL forms the gateway accepts
// (shared/vidnotes_common/youtube.py): watch?v=, youtu.be/, shorts/, embed/, live/.
const YT_ID = /^[A-Za-z0-9_-]{11}$/;

export function videoIdFromUrl(raw) {
  try {
    const u = new URL(raw);
    const host = u.hostname.replace(/^(www|m|music)\./, "");
    let id = null;
    if (host === "youtu.be") id = u.pathname.split("/")[1];
    else if (host === "youtube.com" || host === "youtube-nocookie.com") {
      if (u.pathname === "/watch") id = u.searchParams.get("v");
      else {
        const m = u.pathname.match(/^\/(?:shorts|embed|live|v)\/([^/?#]+)/);
        id = m && m[1];
      }
    }
    return id && YT_ID.test(id) ? id : null;
  } catch {
    return null;
  }
}

export const thumbUrl = (videoId) => `https://i.ytimg.com/vi/${videoId}/mqdefault.jpg`;
