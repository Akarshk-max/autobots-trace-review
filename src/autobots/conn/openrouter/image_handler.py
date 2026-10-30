import os
from typing import Any
from urllib.parse import urlparse

# Common image extensions we recognize. Lowercased, no leading dot.
_IMAGE_EXTENSIONS: set[str] = {
    "jpg",
    "jpeg",
    "png",
    "gif",
    "webp",
    "bmp",
    "svg",
    "tif",
    "tiff",
    "heic",
    "heif",
}

# MIME types that signal an image when present in a data URI.
_IMAGE_MIME_PREFIXES: tuple[str, ...] = ("image/",)


def _looks_like_image_url(url: str) -> bool:
    """Return True if the URL points to an image (by extension or data URI)."""
    if not isinstance(url, str) or not url.strip():
        return False
    url = url.strip()
    parsed = urlparse(url)

    # Data URI: data:image/png;base64,... or data:image/jpeg;...
    if parsed.scheme == "data":
        head = url.split(",", 1)[0].lower()
        return any(prefix in head for prefix in _IMAGE_MIME_PREFIXES)

    # Otherwise inspect the path's extension.
    path = parsed.path or ""
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    if ext in _IMAGE_EXTENSIONS:
        return True

    return False


def detect_image_urls(urls: list[str] | None) -> list[str]:
    """
    Filter a list of URLs down to only image URLs.

    Order is preserved, duplicates are removed.
    Returns an empty list if `urls` is None or empty.
    """
    if not urls:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if not isinstance(u, str):
            continue
        key = u.strip()
        if not key or key in seen:
            continue
        if _looks_like_image_url(key):
            seen.add(key)
            out.append(key)
    return out


def build_multimodal_user_message(
    text: str,
    image_urls: list[str],
    detail: str = "auto",
) -> dict[str, Any]:
    """
    Build an OpenAI/OpenRouter-shaped multimodal user message.

    The returned dict has shape:
        {"role": "user", "content": [<text-block>, <image-block>, ...]}

    Rules:
    - If both text and images: text comes first, then images.
    - If only images: content is just the image blocks (no empty text block).
    - If only text (and no images): content is just the string (not a list).
    - If neither: content is an empty string.

    `detail` defaults to "auto" (model decides); can be "low" or "high".
    """
    image_blocks: list[dict[str, Any]] = [
        {
            "type": "image_url",
            "image_url": {"url": u, "detail": detail},
        }
        for u in image_urls
    ]

    if image_blocks and text:
        content: Any = [{"type": "text", "text": text}, *image_blocks]
    elif image_blocks:
        content = image_blocks
    else:
        content = text

    return {"role": "user", "content": content}


def extract_text_from_message(message: dict[str, Any]) -> str:
    """
    Extract the plain text from a user/assistant message.

    Handles three shapes:
    - {"content": "..."} -> returns the string
    - {"content": [{"type": "text", "text": "..."}, ...]} -> concatenates text blocks
    - {"content": [{"type": "image_url", ...}, ...]} -> returns "" if no text block
    - {"content": None} or missing -> returns ""
    """
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                block_type = block.get("type")
                if block_type == "text":
                    txt = block.get("text")
                    if isinstance(txt, str):
                        parts.append(txt)
        return "\n".join(parts)
    return ""