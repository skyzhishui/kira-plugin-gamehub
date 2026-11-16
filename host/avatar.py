"""game_agent avatar resolution and upload handling.

The only character image in the plugin is the ``game_agent`` avatar:

- default: the bundled ``web/assets/agent-avatar.png`` (nori's avatar);
- override: an image uploaded from the WebUI, stored inside the plugin data
  directory (``<plugin_data>/agent_avatar.<ext>``).

Every other visual (banner, lobby background, stickers) is character-free.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
from pathlib import Path
from typing import Any

try:  # production: KiraAI host logging (AGENTS.md)
    from core.plugin import logger
except ImportError:  # standalone tests outside the host: a plain stdlib logger
    import logging

    logger = logging.getLogger(__name__)

# Bundled default avatar, relative to the plugin root.
DEFAULT_AVATAR_REL = "web/assets/agent-avatar.png"

# Uploaded avatars live next to the other plugin runtime state.
UPLOAD_STEM = "agent_avatar"

MAX_AVATAR_BYTES = 4 * 1024 * 1024

# data URL / mime -> file suffix
_MIME_SUFFIX = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

_DATA_URL_RE = re.compile(r"^data:(?P<mime>[\w.+-]+/[\w.+-]+)?(?P<b64>;base64)?,(?P<data>.*)$", re.S)


def _plugin_root(plugin: Any) -> Path:
    """Plugin root directory (the folder holding ``main.py``)."""
    module = getattr(plugin, "__module__", "") or ""
    if module:
        try:
            import importlib

            mod = importlib.import_module(module)
            file = getattr(mod, "__file__", None)
            if file:
                return Path(file).resolve().parent
        except Exception:  # noqa: BLE001 - fall through to the cwd guess
            pass
    return Path(__file__).resolve().parent.parent


def default_avatar_path(plugin: Any) -> Path:
    """Path of the bundled default avatar."""
    return _plugin_root(plugin) / DEFAULT_AVATAR_REL


def _plugin_id(plugin: Any) -> str:
    """Plugin id from the bundled manifest ("" when unavailable)."""
    import json

    manifest = _plugin_root(plugin) / "manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - manifest is optional here
        return ""
    return str(data.get("plugin_id") or "").strip()


def _data_dir(plugin: Any) -> Path | None:
    data_dir = getattr(plugin, "data_dir", None)
    if data_dir:
        return Path(data_dir)
    ctx = getattr(plugin, "ctx", None)
    getter = getattr(ctx, "get_plugin_data_dir", None)
    if callable(getter):
        try:
            resolved = getter()
        except Exception:  # noqa: BLE001 - avatar is cosmetic
            resolved = None
        if resolved:
            return Path(resolved)
    # ``ctx.get_plugin_data_dir()`` only resolves the caller module, so it is
    # useless from this helper. Fall back to the documented layout instead.
    plugin_id = _plugin_id(plugin)
    if not plugin_id:
        return None
    try:
        from core.utils.path_utils import get_data_path
    except ImportError:  # standalone tests outside the host
        return None
    path = get_data_path() / "plugin_data" / plugin_id
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return path


def uploaded_avatar_path(plugin: Any) -> Path | None:
    """Path of the uploaded avatar, or ``None`` when nothing was uploaded."""
    data_dir = _data_dir(plugin)
    if data_dir is None:
        return None
    for suffix in (".png", ".jpg", ".webp", ".gif"):
        candidate = data_dir / f"{UPLOAD_STEM}{suffix}"
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def avatar_path(plugin: Any) -> Path | None:
    """Effective avatar file: the upload when present, else the bundled default."""
    return uploaded_avatar_path(plugin) or default_avatar_path(plugin)


def avatar_url(plugin: Any) -> str:
    """URL the front end should use for the agent avatar.

    The bundled default is served by the plugin's static page folder, so the
    relative path keeps working without an extra request; an upload is served
    by the plugin API (the file lives outside the static folder).
    """
    uploaded = uploaded_avatar_path(plugin)
    if uploaded is None:
        return "./assets/agent-avatar.png"
    try:
        stamp = int(uploaded.stat().st_mtime)
    except OSError:
        stamp = 0
    return f"/api/plugin/kira-plugin-gamehub/agent-avatar?v={stamp}"


def decode_data_url(payload: str) -> tuple[bytes, str]:
    """Decode ``data:image/png;base64,...`` into ``(bytes, mime)``.

    The size cap is enforced on the base64 text **before** decoding too: a
    huge payload used to be fully decoded into memory only to be rejected
    afterwards (review P2). ``4 * ceil(N/3)`` is the encoded length of N
    bytes, so anything longer than ``4 * (MAX // 3 + 1)`` cannot decode into
    an accepted image.
    """
    raw = str(payload or "").strip()
    match = _DATA_URL_RE.match(raw)
    if not match:
        raise ValueError("头像数据格式不正确（需要 data URL）")
    mime = (match.group("mime") or "image/png").lower()
    data = match.group("data") or ""
    if not match.group("b64"):
        raise ValueError("头像数据格式不正确（需要 base64 编码）")
    if len(data) > 4 * (MAX_AVATAR_BYTES // 3 + 1):
        raise ValueError(f"头像文件过大（上限 {MAX_AVATAR_BYTES // 1024 // 1024}MB）")
    try:
        blob = base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("头像数据不是有效的 base64") from exc
    if not blob:
        raise ValueError("头像数据为空")
    if len(blob) > MAX_AVATAR_BYTES:
        raise ValueError(f"头像文件过大（上限 {MAX_AVATAR_BYTES // 1024 // 1024}MB）")
    if mime not in _MIME_SUFFIX:
        raise ValueError(f"不支持的头像格式：{mime}")
    return blob, mime


def save_avatar(plugin: Any, payload: str) -> dict[str, Any]:
    """Store an uploaded avatar and return its public payload."""
    blob, mime = decode_data_url(payload)
    suffix = _MIME_SUFFIX[mime]
    data_dir = _data_dir(plugin)
    if data_dir is None:
        raise ValueError("插件数据目录不可用")
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"头像保存失败：{exc}") from exc
    target = data_dir / f"{UPLOAD_STEM}{suffix}"
    try:
        target.write_bytes(blob)
    except OSError as exc:
        raise ValueError(f"头像保存失败：{exc}") from exc
    # Drop the other suffixes so exactly one upload is ever live.
    for other in (".png", ".jpg", ".webp", ".gif"):
        stale = data_dir / f"{UPLOAD_STEM}{other}"
        if stale != target:
            try:
                stale.unlink()
            except OSError:
                pass
    logger.info("gamehub: agent avatar updated (%s, %d bytes)", suffix, len(blob))
    return {"agent_avatar": avatar_url(plugin), "uploaded": True, "size": len(blob)}


def clear_avatar(plugin: Any) -> dict[str, Any]:
    """Remove the uploaded avatar and fall back to the bundled default."""
    data_dir = _data_dir(plugin)
    if data_dir is not None:
        for suffix in (".png", ".jpg", ".webp", ".gif"):
            stale = data_dir / f"{UPLOAD_STEM}{suffix}"
            try:
                if stale.is_file():
                    stale.unlink()
            except OSError:
                pass
    logger.info("gamehub: agent avatar reset to the bundled default")
    return {"agent_avatar": avatar_url(plugin), "uploaded": False, "size": 0}


# ----------------------------------------------------------------------
#  Host agent avatar (the active persona's selfie reference image)
# ----------------------------------------------------------------------
async def host_avatar_path(plugin: Any) -> Path | None:
    """Active persona's selfie reference image, resolved through the host API.

    ``ctx.persona_mgr.get_reference_image()`` is the host's public accessor: it
    takes the *active* persona when no id is given, validates the recorded path
    (``data/selfie_refs/<persona_id>.<ext>``) and returns ``None`` when the
    persona has no reference image — so the UI never has to guess a path or
    fall back to another character's picture.
    """
    ctx = getattr(plugin, "ctx", None)
    manager = getattr(ctx, "persona_mgr", None)
    getter = getattr(manager, "get_reference_image", None)
    if not callable(getter):
        return None
    try:
        path = getter()
        if asyncio.iscoroutine(path) or isinstance(path, asyncio.Future):
            path = await path
    except Exception:  # noqa: BLE001 - a missing avatar is cosmetic
        logger.debug("gamehub: host avatar lookup failed", exc_info=True)
        return None
    if not path:
        return None
    candidate = Path(path)
    try:
        if not candidate.is_file():
            return None
    except OSError:
        return None
    return candidate


async def host_avatar_url(plugin: Any) -> str:
    """URL of the host agent avatar, or ``""`` when the host has none.

    An empty string is meaningful: the front end then renders the seat's name
    initial instead of borrowing the ``game_agent`` avatar.
    """
    path = await host_avatar_path(plugin)
    if path is None:
        return ""
    try:
        stamp = int(path.stat().st_mtime)
    except OSError:
        stamp = 0
    return f"/api/plugin/kira-plugin-gamehub/host-avatar?v={stamp}"
