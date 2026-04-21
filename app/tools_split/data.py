"""Data-processing tools — datetime_calc / json_process / text_process.

All three are pure stdlib transformations with no hub / sandbox
dependencies, so they cluster together and form the smallest, cheapest
test of the per-category split.
"""
from __future__ import annotations

import base64
import hashlib
import json as _json
import re
import urllib.parse
from datetime import datetime, timedelta
from typing import Any

from ..defaults import MAX_JSON_RESULT_CHARS


# Each action caps its result length so a single tool call never
# floods the agent's context window. The 10 kB cap below is a
# deliberate upper bound across most text transforms.
_TEXT_OUTPUT_CAP_CHARS = 10000

# text_process.extract stops at this many matches.
_EXTRACT_MAX_MATCHES = 200

# text_process.split caps how many pieces it shows.
_SPLIT_MAX_PIECES = 100


# ── datetime_calc ────────────────────────────────────────────────────

def _tool_datetime_calc(action: str, date: str = "", date2: str = "",
                        days: int = 0, hours: int = 0, minutes: int = 0,
                        timezone: str = "", format: str = "",
                        **_: Any) -> str:
    """Perform date/time calculations."""
    import zoneinfo

    def _parse_date(s: str) -> datetime:
        """Try multiple date formats."""
        for fmt in [
            "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
            "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d",
            "%m/%d/%Y", "%d-%m-%Y",
        ]:
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        raise ValueError(f"Cannot parse date: {s}")

    def _get_tz(tz_name: str):
        if not tz_name:
            return None
        try:
            return zoneinfo.ZoneInfo(tz_name)
        except Exception:
            return None

    try:
        if action == "now":
            tz = _get_tz(timezone)
            now = datetime.now(tz)
            fmt = format or "%Y-%m-%d %H:%M:%S %Z"
            return (f"Current time: {now.strftime(fmt)}\n"
                    f"Timezone: {timezone or 'local'}\n"
                    f"ISO: {now.isoformat()}")

        if action == "diff":
            d1 = _parse_date(date)
            d2 = _parse_date(date2)
            delta = d2 - d1
            total_secs = int(delta.total_seconds())
            abs_secs = abs(total_secs)
            d = abs_secs // 86400
            h = (abs_secs % 86400) // 3600
            m = (abs_secs % 3600) // 60
            sign = "-" if total_secs < 0 else ""
            return (f"Date 1: {d1}\nDate 2: {d2}\n"
                    f"Difference: {sign}{d} days, {h} hours, {m} minutes\n"
                    f"Total days: {delta.days}\n"
                    f"Total seconds: {total_secs}")

        if action == "add":
            d = _parse_date(date)
            delta = timedelta(days=days, hours=hours, minutes=minutes)
            result = d + delta
            fmt = format or "%Y-%m-%d %H:%M:%S"
            return (f"Original: {d.strftime(fmt)}\n"
                    f"Added: {days}d {hours}h {minutes}m\n"
                    f"Result: {result.strftime(fmt)}\n"
                    f"ISO: {result.isoformat()}")

        if action == "format":
            d = _parse_date(date)
            fmt = format or "%Y-%m-%d %H:%M:%S"
            return f"Formatted: {d.strftime(fmt)}\nISO: {d.isoformat()}"

        if action == "convert":
            d = _parse_date(date)
            tz = _get_tz(timezone)
            if tz is None:
                return f"Error: Unknown timezone: {timezone}"
            if d.tzinfo is None:
                d = d.replace(tzinfo=zoneinfo.ZoneInfo("UTC"))
            converted = d.astimezone(tz)
            fmt = format or "%Y-%m-%d %H:%M:%S %Z"
            return (f"Original: {d.strftime(fmt)}\n"
                    f"Converted to {timezone}: {converted.strftime(fmt)}\n"
                    f"ISO: {converted.isoformat()}")

        return f"Error: Unknown action '{action}'. Use: now, diff, add, format, convert"
    except Exception as e:
        return f"Error: {e}"


# ── json_process ─────────────────────────────────────────────────────

def _tool_json_process(action: str, data: str, path: str = "",
                       data2: str = "", **_: Any) -> str:
    """Process JSON data."""
    def _load(s: str):
        """Load JSON from string or file path."""
        s = s.strip()
        if s.startswith("/") or s.startswith("./"):
            try:
                with open(s, "r", encoding="utf-8") as f:
                    return _json.load(f)
            except Exception as e:
                raise ValueError(f"Failed to read file {s}: {e}")
        return _json.loads(s)

    def _extract(obj, path_str: str):
        """Simple JSONPath-like extraction: 'a.b[0].c'"""
        parts = re.split(r'\.|\[(\d+)\]', path_str)
        parts = [p for p in parts if p is not None and p != '']
        for p in parts:
            if isinstance(obj, dict):
                obj = obj[p]
            elif isinstance(obj, list):
                obj = obj[int(p)]
            else:
                raise KeyError(f"Cannot navigate '{p}' in {type(obj).__name__}")
        return obj

    try:
        if action == "parse":
            obj = _load(data)
            formatted = _json.dumps(obj, indent=2, ensure_ascii=False)
            return f"Valid JSON ({type(obj).__name__}):\n{formatted[:_TEXT_OUTPUT_CAP_CHARS]}"

        if action == "extract":
            obj = _load(data)
            result = _extract(obj, path)
            if isinstance(result, (dict, list)):
                return _json.dumps(result, indent=2,
                                   ensure_ascii=False)[:MAX_JSON_RESULT_CHARS]
            return str(result)

        if action == "keys":
            obj = _load(data)
            if isinstance(obj, dict):
                return (f"Keys ({len(obj)}): "
                        + ", ".join(str(k) for k in obj.keys()))
            if isinstance(obj, list):
                return f"Array with {len(obj)} items"
            return f"Type: {type(obj).__name__}, Value: {str(obj)[:200]}"

        if action == "flatten":
            obj = _load(data)
            flat = {}

            def _flatten(o, prefix=""):
                if isinstance(o, dict):
                    for k, v in o.items():
                        _flatten(v, f"{prefix}{k}.")
                elif isinstance(o, list):
                    for i, v in enumerate(o):
                        _flatten(v, f"{prefix}{i}.")
                else:
                    flat[prefix.rstrip(".")] = o
            _flatten(obj)
            return _json.dumps(flat, indent=2,
                               ensure_ascii=False)[:_TEXT_OUTPUT_CAP_CHARS]

        if action == "to_csv":
            obj = _load(data)
            if not isinstance(obj, list) or not obj:
                return "Error: Input must be a non-empty JSON array of objects"
            headers = list(obj[0].keys()) if isinstance(obj[0], dict) else []
            if not headers:
                return "Error: Array items must be objects"
            lines = [",".join(headers)]
            for item in obj:
                vals = [str(item.get(h, "")).replace(",", ";").replace("\n", " ")
                        for h in headers]
                lines.append(",".join(vals))
            return "\n".join(lines)

        if action == "from_csv":
            lines = data.strip().splitlines()
            if len(lines) < 2:
                return "Error: CSV must have at least header + 1 data row"
            headers = [h.strip() for h in lines[0].split(",")]
            result = []
            for line in lines[1:]:
                vals = [v.strip() for v in line.split(",")]
                result.append(dict(zip(headers, vals)))
            return _json.dumps(result, indent=2,
                               ensure_ascii=False)[:_TEXT_OUTPUT_CAP_CHARS]

        if action == "merge":
            obj1 = _load(data)
            obj2 = _load(data2)
            if isinstance(obj1, dict) and isinstance(obj2, dict):
                merged = {**obj1, **obj2}
            elif isinstance(obj1, list) and isinstance(obj2, list):
                merged = obj1 + obj2
            else:
                return "Error: Both inputs must be same type (both objects or both arrays)"
            return _json.dumps(merged, indent=2,
                               ensure_ascii=False)[:_TEXT_OUTPUT_CAP_CHARS]

        if action == "count":
            obj = _load(data)
            if isinstance(obj, list):
                return f"Array: {len(obj)} items"
            if isinstance(obj, dict):
                return f"Object: {len(obj)} keys"
            return f"Type: {type(obj).__name__}"

        return (f"Error: Unknown action '{action}'. "
                "Use: parse, extract, keys, flatten, to_csv, from_csv, merge, count")
    except Exception as e:
        return f"Error: {e}"


# ── text_process ─────────────────────────────────────────────────────

def _tool_text_process(action: str, text: str, pattern: str = "",
                       replacement: str = "", n: int = 10,
                       algorithm: str = "sha256", delimiter: str = "\n",
                       **_: Any) -> str:
    """Process and transform text."""
    try:
        if action == "count":
            lines = text.splitlines()
            words = text.split()
            chars = len(text)
            return f"Lines: {len(lines)}\nWords: {len(words)}\nCharacters: {chars}"

        if action == "replace":
            if not pattern:
                return "Error: 'pattern' required for replace"
            result = re.sub(pattern, replacement, text)
            count = len(re.findall(pattern, text))
            return f"Replaced {count} occurrences.\n\n{result[:_TEXT_OUTPUT_CAP_CHARS]}"

        if action == "extract":
            if not pattern:
                return "Error: 'pattern' required for extract"
            matches = re.findall(pattern, text)
            if not matches:
                return "No matches found."
            return (f"Found {len(matches)} matches:\n"
                    + "\n".join(str(m) for m in matches[:_EXTRACT_MAX_MATCHES]))

        if action == "sort":
            lines = text.splitlines()
            sorted_lines = sorted(lines)
            return "\n".join(sorted_lines)

        if action == "dedup":
            lines = text.splitlines()
            seen = set()
            unique = []
            for line in lines:
                if line not in seen:
                    seen.add(line)
                    unique.append(line)
            removed = len(lines) - len(unique)
            return (f"Removed {removed} duplicates "
                    f"({len(unique)} unique lines):\n\n"
                    + "\n".join(unique))

        if action == "base64_encode":
            encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
            return encoded

        if action == "base64_decode":
            decoded = base64.b64decode(text).decode("utf-8", errors="replace")
            return decoded

        if action == "url_encode":
            return urllib.parse.quote(text, safe="")

        if action == "url_decode":
            return urllib.parse.unquote(text)

        if action == "hash":
            algo = algorithm.lower()
            if algo == "md5":
                h = hashlib.md5(text.encode("utf-8")).hexdigest()
            elif algo == "sha1":
                h = hashlib.sha1(text.encode("utf-8")).hexdigest()
            elif algo == "sha256":
                h = hashlib.sha256(text.encode("utf-8")).hexdigest()
            else:
                return f"Error: Unknown algorithm '{algo}'. Use: md5, sha1, sha256"
            return f"{algo}: {h}"

        if action == "head":
            lines = text.splitlines()
            k = max(1, min(n, len(lines)))
            return "\n".join(lines[:k])

        if action == "tail":
            lines = text.splitlines()
            k = max(1, min(n, len(lines)))
            return "\n".join(lines[-k:])

        if action == "split":
            parts = text.split(delimiter)
            return (f"Split into {len(parts)} parts:\n"
                    + "\n---\n".join(parts[:_SPLIT_MAX_PIECES]))

        return (f"Error: Unknown action '{action}'. "
                "Use: count, replace, extract, sort, dedup, "
                "base64_encode, base64_decode, url_encode, url_decode, hash, head, tail, split")
    except Exception as e:
        return f"Error: {e}"
