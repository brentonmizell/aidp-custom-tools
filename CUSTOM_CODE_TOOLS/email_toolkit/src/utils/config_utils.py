"""Shared config helper. Unwraps conf["conf"] and coerces to the default's type
so template-stringified values (e.g. "30" from a {{variable}}) don't crash
comparisons or arithmetic."""


def get_cfg(conf, key, default):
    inner = conf.get("conf") if isinstance(conf, dict) else None
    if isinstance(inner, dict) and key in inner:
        value = inner[key]
    elif isinstance(conf, dict) and key in conf:
        value = conf[key]
    else:
        return default
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    return value


def as_rows(value):
    """Coerce common shapes into a list of dict rows.
    Accepts: list[dict]; a JSON string of either; or a dict with a 'rows'/'data'
    /'items' key. Returns (rows, error_or_None)."""
    import json
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return None, "input is a string but not valid JSON"
    if isinstance(value, dict):
        for k in ("rows", "data", "items", "records"):
            if isinstance(value.get(k), list):
                value = value[k]
                break
    if not isinstance(value, list):
        return None, "input must be a list of records (or JSON encoding one)"
    if value and not all(isinstance(r, dict) for r in value):
        return None, "every record must be an object/dict"
    return value, None
