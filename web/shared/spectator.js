/* spectator.js — spectator identity (read-only).
 *
 * Spectator credentials never read or forward the host WebUI token. The spectator
 * credential lives only in sessionStorage["gamehub_spectator_<match_id>"]; the host
 * JWT takes no part in spectator requests (shared/api.js localToken reads the
 * spectator credential instead while in spectator mode).
 */
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

/** Validate the spectator-join response envelope: only { ok:true, data:{ credential, uid } }
 * with both fields non-empty strings is accepted. Server error envelopes, truncated
 * data and non-JSON bodies all throw, so undefined is never written into
 * sessionStorage (which would later send the literal "undefined" as a Bearer token).
 *
 * The server contract (host/spectator.py join) always emits `uid` as
 * `<match_id>:<hex digest>` and `credential` as a token_urlsafe string, so a numeric
 * uid is a shape violation, not an older format: it is rejected, never coerced. */
export function parseSpectatorJoin(payload) {
  const data = payload && typeof payload === "object" ? payload.data : null;
  const credential = data && typeof data.credential === "string" ? data.credential.trim() : "";
  const uid = data && typeof data.uid === "string" ? data.uid.trim() : "";
  if (!payload || payload.ok !== true || !credential || !uid) {
    const reason = payload && typeof payload.error === "string" ? payload.error.trim() : "";
    throw new Error(reason || text("观战邀请被拒绝", "Invitation rejected"));
  }
  return { credential, uid };
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
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    /* A non-JSON body counts as failure (reported uniformly below) */
  }
  if (!response.ok) {
    const reason = payload && typeof payload.error === "string" ? payload.error : "";
    throw new Error(reason || text("观战邀请被拒绝", "Invitation rejected"));
  }
  const { credential, uid } = parseSpectatorJoin(payload);
  sessionStorage.setItem(`gamehub_spectator_${matchId}`, credential);
  sessionStorage.setItem(`gamehub_spectator_uid_${matchId}`, uid);
}
