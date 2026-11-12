"""Сжатые копии картинок: превью для плиток и просмотра, лёгкие картинки внутри письма.

Нужна библиотека Pillow. Без неё всё работает как раньше — отдаём оригинал.
Готовые копии лежат на диске (папка thumbs рядом с базой): вложение письма не меняется, и второй раз
сжимать не нужно.
"""
from __future__ import annotations

import hashlib
import io
import os
import threading
from typing import Optional, Tuple

try:
    from PIL import Image, ImageOps
    from PIL import features as _features
    AVAILABLE = True
    WEBP = bool(_features.check("webp"))
except Exception:  # noqa: BLE001 — Pillow не установлен или собран без нужных кодеков
    AVAILABLE = False
    WEBP = False

# размер: (длинная сторона в пикселях, качество)
SIZES = {
    "thumb": (480, 72),    # плитка в разделе «Вложения» и значок в письме, с запасом для экранов Retina
    "view": (2048, 82),    # просмотр и картинки внутри письма
}
SMALL_ENOUGH = 200 * 1024   # картинку меньше и не крупнее нужного размера отдаём как есть
MAX_SOURCE = 40 * 1024 * 1024
MAX_PIXELS = 80_000_000     # больше — скорее всего «бомба», не разжимаем
CACHE_LIMIT = 500 * 1024 * 1024
PRUNE_EVERY = 200           # проверять размер папки раз в столько новых файлов

_lock = threading.Lock()
_written = 0


def mime() -> str:
    return "image/webp" if WEBP else "image/jpeg"


def shrink(data: bytes, size: str) -> Optional[bytes]:
    """Сжатая копия или None — сжимать незачем (уже маленькая) или не получилось (формат, повреждённый файл)."""
    if not AVAILABLE or size not in SIZES or not data or len(data) > MAX_SOURCE:
        return None
    side, quality = SIZES[size]
    try:
        img = Image.open(io.BytesIO(data))
        if img.width * img.height > MAX_PIXELS:
            return None
        if len(data) <= SMALL_ENOUGH and max(img.size) <= side:
            return None
        img.draft("RGB", (side, side))  # JPEG разжимается сразу уменьшенным — в разы быстрее
        img = ImageOps.exif_transpose(img)  # фото с телефона: поворот из EXIF
        img.thumbnail((side, side), Image.LANCZOS)
        alpha = img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)
        out = io.BytesIO()
        if WEBP:
            img = img.convert("RGBA" if alpha else "RGB")
            img.save(out, "WEBP", quality=quality, method=4)
        else:
            if alpha:  # у JPEG нет прозрачности: подкладываем белый фон, как у письма
                bg = Image.new("RGB", img.size, (255, 255, 255))
                rgba = img.convert("RGBA")
                bg.paste(rgba, mask=rgba.split()[-1])
                img = bg
            img.convert("RGB").save(out, "JPEG", quality=quality, optimize=True, progressive=True)
        result = out.getvalue()
    except Exception:  # noqa: BLE001 — HEIC без плагина, обрезанный файл и т. п.
        return None
    return result if len(result) < len(data) else None  # маленькая картинка и так легче копии


class Store:
    """Папка с готовыми копиями. Ключ — письмо Exchange, вложение и размер: номер письма в базе
    после удаления может достаться другому письму, а адрес письма в Exchange — нет."""

    def __init__(self, folder: str):
        self.folder = folder

    def _path(self, key: str, size: str) -> str:
        name = hashlib.sha256(key.encode("utf-8")).hexdigest()[:40]
        return os.path.join(self.folder, "{}-{}.{}".format(name, size, "webp" if WEBP else "jpg"))

    def get(self, key: str, size: str, load) -> Tuple[Optional[bytes], bool]:
        """(байты копии или None, найдено ли в папке). load() — оригинал, зовём только если копии ещё нет.
        Пустой файл в папке значит «сжимать незачем»: второй раз оригинал не разбираем."""
        path = self._path(key, size)
        try:
            with open(path, "rb") as f:
                data = f.read()
            return (data or None), True
        except OSError:
            pass
        small = shrink(load(), size)
        self._save(path, small or b"")
        return small, False

    def _save(self, path: str, data: bytes) -> None:
        global _written
        try:
            os.makedirs(self.folder, mode=0o700, exist_ok=True)
            tmp = path + ".tmp{}".format(threading.get_ident())
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except OSError:
            return
        with _lock:
            _written += 1
            due = _written % PRUNE_EVERY == 0
        if due:
            self.prune()

    def prune(self, limit: int = CACHE_LIMIT) -> None:
        """Папка больше лимита — удаляем самые давно открытые копии."""
        try:
            files = []
            for e in os.scandir(self.folder):
                if e.is_file():
                    st = e.stat()
                    files.append((st.st_atime or st.st_mtime, st.st_size, e.path))
        except OSError:
            return
        total = sum(f[1] for f in files)
        for _, sz, path in sorted(files):
            if total <= limit * 0.8:
                break
            try:
                os.remove(path)
                total -= sz
            except OSError:
                pass


__all__ = ["AVAILABLE", "SIZES", "Store", "shrink", "mime"]
