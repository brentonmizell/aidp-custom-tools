# =============================================================================
#  AIDP CUSTOM TOOL  —  tool_implementation.py
# =============================================================================
#  This is the only required file. To make your own tool:
#
#    1. Rename the class  MyTool      -> something descriptive
#    2. Fill in          _execute_tool with your logic
#    3. Declare inputs   in tool_config.json -> tools[].schema
#    4. Put settings     in tool_config.json -> tools[].conf
#
#  Then run ./build.sh and upload the zip. That's the whole loop.
#
#  A second example (MyHttpTool) shows how to call an external API and how a
#  single package can hold more than one tool. Delete it if you don't need it.
# =============================================================================

from aidputils.agents.tools.custom_tools.base import CustomToolBase
from .utils.config_utils import get_cfg


@CustomToolBase.register          # <-- this line registers the tool. keep it.
class MyTool(CustomToolBase):
    """One line describing the tool (for humans).

    The text the *model* reads to decide when to call this tool lives in
    tool_config.json -> description. Keep that one about WHAT it does / WHEN
    to use it.
    """

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        # -- 1. Read what the model passed in (these match tool_config schema) --
        name = runtime_params.get("name", "World")

        # -- 2. Read your static settings (defined in tool_config -> conf) -------
        #    get_cfg handles two gotchas for you: it looks inside conf["conf"],
        #    and it converts "100" (a string from a {{template}}) into 100.
        greeting = get_cfg(conf, "greeting", "Hello")
        max_length = get_cfg(conf, "max_length", 100)   # comes back as an int

        # -- 3. Do the work. Wrap risky parts so YOU control the error message. --
        try:
            if not name or not str(name).strip():
                return {"error": "name is required"}      # <- error shape, see note

            message = f"{greeting}, {name}!"[:max_length]

            # -- 4. Return a dict. Add whatever keys are useful to the model. ----
            return {"message": message, "length": len(message)}

        except Exception as e:
            # ALWAYS return {"error": ...} on failure (not a success dict with an
            # error inside). The framework turns this into isError:true so the
            # model sees a real failure instead of guessing.
            return {"error": str(e)}


# -----------------------------------------------------------------------------
#  OPTIONAL SECOND TOOL — calling an external API.
#  Delete this whole block (and its entry in tool_config.json) if not needed.
# -----------------------------------------------------------------------------
@CustomToolBase.register
class MyHttpTool(CustomToolBase):
    """Example: fetch something from an HTTP API.

    Prefer the built-in cls._make_http_request over raw requests: it adds SSRF
    protection and applies the `auth` block from your config automatically.
    """

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        query = runtime_params.get("query", "")
        url = get_cfg(conf, "base_url", "https://httpbin.org/get")
        timeout = get_cfg(conf, "timeout", 30)

        try:
            resp = cls._make_http_request(
                method="GET",
                url=url,
                conf=conf,                                  # carries the auth block
                headers={"Accept": "application/json"},
                params={"q": query} if query else None,
                timeout=timeout,
            )
            return resp.json()
        except Exception as e:
            return {"error": str(e)}


# -----------------------------------------------------------------------------
#  OPTIONAL HOOKS — both are optional; delete if you don't use them.
#
#  def _validate_config(cls, conf, runtime_params=None, **context_vars):
#      # raise ValueError to stop before running, e.g. require a setting:
#      if not get_cfg(conf, "api_key", ""):
#          raise ValueError("api_key is required in tool config")
#
#  def _transform_response(cls, response):
#      # last-chance reshape before the result is returned
#      return response
# -----------------------------------------------------------------------------
