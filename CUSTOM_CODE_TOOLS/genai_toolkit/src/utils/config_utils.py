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


def derive_endpoint(region, default_endpoint=None):
    if region and isinstance(region, str) and region.strip():
        return f"https://inference.generativeai.{region.strip()}.oci.oraclecloud.com"
    return default_endpoint or "https://inference.generativeai.us-chicago-1.oci.oraclecloud.com"


def derive_model_provider(model_id, default="generic"):
    if not model_id or not isinstance(model_id, str):
        return default
    mid = model_id.strip().lower()
    if mid.startswith("cohere."):
        return "cohere"
    return "generic"


def resolve_oci_conf(conf):
    model_id = get_cfg(conf, "model_id", "cohere.command-a-03-2025")
    region = get_cfg(conf, "region", "")
    explicit_endpoint = get_cfg(conf, "endpoint", "")
    if region:
        endpoint = derive_endpoint(region, explicit_endpoint or None)
    else:
        endpoint = explicit_endpoint or derive_endpoint(None)
    explicit_provider = get_cfg(conf, "model_provider", "")
    if explicit_provider:
        model_provider = explicit_provider
    else:
        model_provider = derive_model_provider(model_id)
    return {
        "model_id": model_id,
        "model_provider": model_provider,
        "region": region,
        "endpoint": endpoint,
        "compartment_id": get_cfg(conf, "compartment_id", ""),
        "auth_type": get_cfg(conf, "auth_type", "resource_principal"),
        "auth_profile": get_cfg(conf, "auth_profile", "DEFAULT"),
    }
