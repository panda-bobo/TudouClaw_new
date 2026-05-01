# take_screenshot

Capture the screen and return a local image path. Wraps the bound `screen-capture` MCP.

## Prerequisites

Agent must have the `screen-capture` MCP bound (not optional).

## 工作流（3 步，不要跳步）

1. **Call `skill(take_screenshot, …)`** with the desired region.
2. **Verify the output** with the script in 质量门 below. A black/empty/permission-denied screenshot is the most common silent failure — pixel-only inspection won't catch it.
3. **Surface the path to the user** (or feed it directly into the next tool — read_file, attach to email, etc). Don't claim "screenshot taken" without the path.

## Canonical call

```python
skill(take_screenshot, {})          # full screen (default)
skill(take_screenshot, {"region": "full"})
skill(take_screenshot, {"region": "window"})     # frontmost window
skill(take_screenshot, {"region": "selection"})  # user-drawn rectangle
```

## Returns

```python
{
    "image_path": "/abs/path/to/screenshot.png",
    "width":      2560,
    "height":     1440
}
```

The returned path is an absolute file on disk — you can pass it to `read_file`, attach it to an email, or inspect the image directly.

## Field notes

- **region**: one of `full` / `window` / `selection`. Default is `full`.
  - `window` captures whichever app is frontmost when the skill fires.
  - `selection` opens an interactive selector — the user draws a rectangle. Blocks until they finish.

## 质量门（声明完成前必须通过）

Run this on the skill's return value. Any non-empty `problems` list = the capture failed silently — STOP, surface the issue, and do NOT claim success.

```python
import os

def validate_screenshot(result):
    problems = []
    image_path = (result or {}).get("image_path") or ""
    width = int((result or {}).get("width") or 0)
    height = int((result or {}).get("height") or 0)

    if not image_path:
        problems.append("no image_path returned by skill")
    elif not os.path.isfile(image_path):
        problems.append(f"image_path does not exist on disk: {image_path}")
    else:
        size = os.path.getsize(image_path)
        # Black/empty PNG of a normal display still compresses > 1KB.
        # Anything smaller is almost certainly a capture failure.
        if size < 1024:
            problems.append(f"image suspiciously small ({size}B) — likely capture failed / blank screen")

    if width and width < 100:
        problems.append(f"image width too small ({width}px) — likely capture failed")
    if height and height < 100:
        problems.append(f"image height too small ({height}px) — likely capture failed")

    return problems

problems = validate_screenshot(result)
if problems:
    print("❌ take_screenshot QA failed:")
    for p in problems: print("  -", p)
    raise SystemExit(1)
print(f"✓ screenshot OK — {result['width']}x{result['height']} at {result['image_path']}")
```

**Why this gate exists:** macOS Screen Recording permission errors and display-sleep often produce a 0x0 PNG or a tiny black image. The skill returns "successfully" because the MCP didn't raise — only post-validation catches the bad output.

## Failure modes

| Symptom | Fix |
|---|---|
| `No screen-capture MCP bound` | Bind it in the admin UI |
| Permission denied (macOS) | User needs to grant Screen Recording permission in System Settings → Privacy & Security |
| Empty / black image | Usually display sleep; ask the user to wake the screen and retry |
| QA gate `image suspiciously small` | Almost always permission denied OR display sleeping. Check System Settings → Privacy & Security → Screen Recording, and confirm the user's display is awake. |
