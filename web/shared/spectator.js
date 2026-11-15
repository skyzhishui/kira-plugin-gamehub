/* Spectator credentials never read or forward the host WebUI token. */
import { uiLanguage } from "./i18n.js";

const text = (zh, en) => uiLanguage() === "en" ? en : zh;

export const spectatorMode = () => typeof document !== "undefined" && document.body?.dataset.spectator === "true";

export function spectatorCredential() {
  try {
    return sessionStorage.getItem(`gamehub_spectator_${new URLSearchParams(location.search).get("id")}`) || "";
  } catch {
    return "";
  }
}

export async function enterSpectator(matchId) {
  const fragment = new URLSearchParams(location.hash.slice(1));
  const token = fragment.get("invite") || sessionStorage.getItem(`gamehub_invite_${matchId}`) || "";
  if (!token) throw new Error(text("请使用有效的观战邀请链接。", "Open a valid spectator invitation."));
  sessionStorage.setItem(`gamehub_invite_${matchId}`, token);
  history.replaceState(null, "", location.pathname + location.search);
  if (spectatorCredential()) {
    const check = await fetch(`/api/plugin/kira-plugin-gamehub/matches/${encodeURIComponent(matchId)}`, {
      headers: { Authorization: `Bearer ${spectatorCredential()}` },
    });
    if (check.ok) return;
    sessionStorage.removeItem(`gamehub_spectator_${matchId}`);
  }
  const name = window.prompt(text("设置本局临时昵称（1–24 字）", "Temporary nickname for this match (1–24 characters)"), "");
  if (!name?.trim()) throw new Error(text("未设置观战昵称", "Nickname required"));
  const response = await fetch("/spectator/join", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ match_id: matchId, token, name: name.trim() }),
  });
  const payload = await response.json();
  if (!response.ok || !payload.ok) throw new Error(payload.error || "Invitation rejected");
  sessionStorage.setItem(`gamehub_spectator_${matchId}`, payload.data.credential);
  sessionStorage.setItem(`gamehub_spectator_uid_${matchId}`, payload.data.uid);
}
