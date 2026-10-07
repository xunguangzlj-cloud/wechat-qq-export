"""将标题或会话显示名转换成可在 Windows 使用的导出文件名。"""

import re
from pathlib import Path


def export_stem(name, fallback="未命名会话"):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or fallback).strip()).rstrip(" .")
    value = value or fallback
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                *(f"COM{i}" for i in "123456789¹²³"), *(f"LPT{i}" for i in "123456789¹²³")}
    if value.split(".", 1)[0].upper() in reserved or value.casefold() == ".export-state":
        value = "_" + value
    # 以 UTF-16 单元限制长度，保留完整 emoji，并为安装目录和会话目录留出路径空间。
    result, units = "", 0
    for character in value:
        size = len(character.encode("utf-16-le", errors="surrogatepass")) // 2
        if units + size > 80:
            break
        result += character
        units += size
    return result.rstrip(" .") or fallback


def export_paths(directory, name, fallback="未命名会话"):
    stem = export_stem(name, fallback)
    return {kind: Path(directory) / f"{stem}.{kind}" for kind in ("txt", "md", "json")}
