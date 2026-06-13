"""
Config helpers shared across tools in this package.

The big one is get_cfg, which solves two recurring footguns:

1. Your settings live under conf["conf"], not at the top level of the dict the
   framework hands you. get_cfg looks in the nested dict first, then falls back
   to the top level, then to your default.

2. Config values that pass through {{template}} substitution arrive as STRINGS
   even when you wrote a number. Comparing len(x) > max_lines then crashes with
   "TypeError: '>' not supported between instances of 'int' and 'str'".
   get_cfg coerces the value to the type of the default you pass in.
"""


def get_cfg(conf, key, default):
    """Read a config value from either conf[key] or conf["conf"][key],
    coercing it to the type of `default` (int/float/bool/str)."""
    inner = conf.get("conf") if isinstance(conf, dict) else None
    if isinstance(inner, dict) and key in inner:
        value = inner[key]
    elif isinstance(conf, dict) and key in conf:
        value = conf[key]
    else:
        return default

    # Coerce to the default's type so template-stringified values behave.
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
