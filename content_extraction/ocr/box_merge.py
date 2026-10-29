def _valid_box(item):
    if not isinstance(item, dict) or not str(item.get("text") or "").strip():
        return None
    box = item.get("bounding_box")
    if not isinstance(box, dict):
        return None
    try:
        x = int(box["x"])
        y = int(box["y"])
        width = int(box["width"])
        height = int(box["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return {
        "text": str(item["text"]).strip(), "x": x, "y": y,
        "width": width, "height": height,
    }


def _same_line(line, item):
    top = max(line["top"], item["y"])
    bottom = min(line["bottom"], item["y"] + item["height"])
    overlap = max(0, bottom - top)
    minimum_height = min(line["height"], item["height"])
    centers_close = abs(line["center"] - (item["y"] + item["height"] / 2)) <= minimum_height * .55
    return centers_close or overlap >= minimum_height * .5


def merge_provider_text(result):
    items = [item for item in (_valid_box(raw) for raw in result.boxes) if item]
    if not items:
        return str(result.text or "").strip()
    lines = []
    for item in sorted(items, key=lambda value: (value["y"], value["x"])):
        target = next((line for line in lines if _same_line(line, item)), None)
        if target is None:
            target = {
                "items": [], "top": item["y"], "bottom": item["y"] + item["height"],
                "height": item["height"], "center": item["y"] + item["height"] / 2,
            }
            lines.append(target)
        target["items"].append(item)
        target["top"] = min(target["top"], item["y"])
        target["bottom"] = max(target["bottom"], item["y"] + item["height"])
        target["height"] = target["bottom"] - target["top"]
        target["center"] = (target["top"] + target["bottom"]) / 2
    lines.sort(key=lambda line: line["top"])
    return "\n".join(
        "".join(item["text"] for item in sorted(line["items"], key=lambda value: value["x"]))
        for line in lines
    )
