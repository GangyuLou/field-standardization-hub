from __future__ import annotations

import io
import re
import zipfile
from typing import Any

import pandas as pd

# Excel 里常见的“筛选”记录（autoFilter/customFilters）常带中文条件，
# openpyxl 解析这类节点时会直接抛 ValueError，导致整本工作簿读不出来。
_AUTOFILTER_RE = re.compile(rb"<autoFilter\b[^>]*/>|<autoFilter\b.*?</autoFilter>", re.S)


class WorkbookReadError(RuntimeError):
    """Excel/CSV 无法解析时抛出，携带可展示给用户的原因。"""


def sanitize_xlsx(data: bytes) -> bytes:
    """去掉工作表 XML 中的自动筛选节点，其余内容原样保留。"""
    try:
        source = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise WorkbookReadError("文件不是有效的 xlsx（zip）格式，请另存为 .xlsx 后重试") from exc

    buffer = io.BytesIO()
    with source, zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            payload = source.read(item.filename)
            if item.filename.startswith("xl/worksheets/") and item.filename.endswith(".xml"):
                payload = _AUTOFILTER_RE.sub(b"", payload)
            target.writestr(item, payload)
    return buffer.getvalue()


def read_excel(data: bytes, **kwargs: Any) -> pd.DataFrame:
    try:
        return pd.read_excel(io.BytesIO(data), engine="openpyxl", **kwargs)
    except Exception:
        cleaned = sanitize_xlsx(data)
        try:
            return pd.read_excel(io.BytesIO(cleaned), engine="openpyxl", **kwargs)
        except Exception as exc:  # noqa: BLE001 - 统一转成可展示错误
            raise WorkbookReadError(f"读取 Excel 失败：{exc}") from exc


def excel_file(data: bytes) -> pd.ExcelFile:
    try:
        return pd.ExcelFile(io.BytesIO(data), engine="openpyxl")
    except Exception:
        cleaned = sanitize_xlsx(data)
        try:
            return pd.ExcelFile(io.BytesIO(cleaned), engine="openpyxl")
        except Exception as exc:  # noqa: BLE001
            raise WorkbookReadError(f"读取 Excel 失败：{exc}") from exc


def read_csv(data: bytes) -> pd.DataFrame:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return pd.read_csv(io.BytesIO(data), dtype=object, encoding=encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
        except Exception as exc:  # noqa: BLE001
            raise WorkbookReadError(f"读取 CSV 失败：{exc}") from exc
    raise WorkbookReadError(f"CSV 编码无法识别：{last_error}")


def read_table(data: bytes, filename: str, **kwargs: Any) -> pd.DataFrame:
    if (filename or "").lower().endswith(".csv"):
        return read_csv(data)
    return read_excel(data, **kwargs)
