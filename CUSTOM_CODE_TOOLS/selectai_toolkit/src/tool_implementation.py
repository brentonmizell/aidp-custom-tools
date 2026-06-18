"""
AIDP Select AI Toolkit
======================
Two-tool pair for natural-language-to-SQL against Oracle Autonomous Database
via DBMS_CLOUD_AI (no MCP, no extra services).

  SelectAIProvisionTool  - creates / updates the Select AI profile + agent tool
                           once per (connection, schema, table list, model).
                           Idempotent via a sha256 hash stored in an audit
                           table (AIDP_NL2SQL_PROFILES, auto-created).
  NL2SQLTool             - per-turn worker. Runs
                              SELECT DBMS_CLOUD_AI.GENERATE(:prompt,:profile,:action)
                              FROM dual
                           for one of RUNSQL / SHOWSQL / NARRATE / EXPLAINSQL.
                           RUNSQL is guarded: the generated SQL is first
                           fetched via SHOWSQL, parsed, and rejected unless
                           its leading keyword (after comments/whitespace) is
                           SELECT or WITH.

Connection sourcing (preferred -> fallback):
  1. ``catalog_key`` runtime/conf -> aidp_io.get_connection_data(catalog_key)
     returns connectionProperties containing user.name + password + tns +
     wallet.content (base64 zip) + wallet.password. The wallet zip is
     materialized into a per-call temp dir and oracledb is opened with
     wallet_location pointing at it.
  2. Explicit ``conn_string`` + ``username`` + ``password`` (+ optional
     ``wallet_path`` already-unzipped directory) runtime params; same fields
     can live in conf as fallbacks.

Returns a structured envelope ``{"ok": true, "data": {...}, ...legacy}``
or ``{"ok": false, "error": "...", "error_type": "...", ...}``. Legacy
top-level fields are duplicated alongside the envelope so older agent code
reading them directly keeps working.

Debug Channel:
  Every meaningful step calls debug() with masked sensitive values; the final
  result is wrapped in DebugLog.embed() before return. If ``aidp_debug`` is
  not importable in the runtime (e.g., unit tests) the helpers degrade to
  no-ops without raising.

LangGraph version: 1.0.1 (caller-side; this file is pure tool code).
"""

import base64
import datetime
import hashlib
import io
import json
import os
import re
import tempfile
import zipfile

from aidputils.agents.tools.custom_tools.base import CustomToolBase
from .utils.config_utils import get_cfg, ok, fail

# --------------------------------------------------------------------------- #
# Debug Channel - graceful no-op fallback if the runtime doesn't inject it.
# --------------------------------------------------------------------------- #
try:
    from aidp_debug import debug, debug_warn, debug_error, DebugLog
except ImportError:  # pragma: no cover - debug shim only used outside runtime
    def debug(*args, **kwargs): pass
    def debug_warn(*args, **kwargs): pass
    def debug_error(*args, **kwargs): pass

    class DebugLog:
        @staticmethod
        def embed(result):
            return result


# --------------------------------------------------------------------------- #
# aidp_io import guard - the toolkit prefers the shared get_connection_data
# helper, but we never hard-fail when it's missing so local tests that pass
# explicit conn_string + username + password still work.
# --------------------------------------------------------------------------- #
try:
    from .utils.aidp_io import get_connection_data as _io_get_connection_data
except Exception:  # pragma: no cover - shared module missing entirely
    _io_get_connection_data = None


# --------------------------------------------------------------------------- #
# Constants - PL/SQL templates kept as module-level strings so they can be
# audited / linted in isolation. {tab} is substituted with the audit table
# name (default AIDP_NL2SQL_PROFILES). Everything else is bound by name.
# --------------------------------------------------------------------------- #

_AUDIT_DDL = """\
DECLARE
  e_exists EXCEPTION; PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE {tab} (
      PROFILE_NAME        VARCHAR2(128 CHAR) NOT NULL,
      TOOL_NAME           VARCHAR2(128 CHAR) NOT NULL,
      TARGET_SCHEMA       VARCHAR2(128 CHAR) NOT NULL,
      TARGET_TABLES_CSV   VARCHAR2(4000 CHAR) NOT NULL,
      LLM_MODEL_ID        VARCHAR2(256 CHAR) NOT NULL,
      PROVIDER            VARCHAR2(64  CHAR) NOT NULL,
      CREDENTIAL_NAME     VARCHAR2(128 CHAR),
      CONFIG_HASH         VARCHAR2(64  CHAR) NOT NULL,
      CONFIG_JSON         CLOB NOT NULL,
      CREATED_AT          TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
      UPDATED_AT          TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
      CONSTRAINT {tab}_PK PRIMARY KEY (PROFILE_NAME))
  ]';
EXCEPTION WHEN e_exists THEN NULL; END;
"""

_AUDIT_IX = """\
DECLARE
  e_exists EXCEPTION; PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
  EXECUTE IMMEDIATE 'CREATE UNIQUE INDEX {tab}_UX1 ON {tab} (PROFILE_NAME)';
EXCEPTION WHEN e_exists THEN NULL; END;
"""

_CREATE_PROFILE_PLSQL = """\
BEGIN
  DBMS_CLOUD_AI.CREATE_PROFILE(
    profile_name => :pn,
    attributes   => :attr,
    description  => :descr,
    status       => 'ENABLED');
END;"""

_DROP_PROFILE_PLSQL = """\
BEGIN
  BEGIN
    DBMS_CLOUD_AI.DROP_PROFILE(profile_name => :pn, force => TRUE);
  EXCEPTION WHEN OTHERS THEN NULL;
  END;
END;"""

_CREATE_AGENT_TOOL_PLSQL = """\
BEGIN
  DBMS_CLOUD_AI_AGENT.CREATE_TOOL(
    tool_name  => :tn,
    attributes => :attr);
END;"""

_DROP_AGENT_TOOL_PLSQL = """\
BEGIN
  BEGIN
    DBMS_CLOUD_AI_AGENT.DROP_TOOL(tool_name => :tn, force => TRUE);
  EXCEPTION WHEN OTHERS THEN NULL;
  END;
END;"""

_GENERATE_SQL = (
    "SELECT DBMS_CLOUD_AI.GENERATE("
    "prompt => :prompt, profile_name => :pn, action => :action) "
    "FROM DUAL"
)

# Allow leading line comments (--...), block comments (/* ... */ including
# hints like /*+ ... */), and arbitrary whitespace before the first keyword.
# DOTALL so /* ... */ can span lines.
_READ_ONLY_RE = re.compile(
    r"^\s*(?:(?:--[^\n]*\n)|(?:/\*.*?\*/)|\s)*\s*(select|with)\b",
    re.IGNORECASE | re.DOTALL,
)

# Per-spec hard cap on RUNSQL row materialization.
_HARD_ROW_CAP = 10000


# --------------------------------------------------------------------------- #
# Connection helper - opens an oracledb connection from either a catalog
# binding or explicit runtime params. Both call sites in this module funnel
# through here so wallet handling stays in one place.
# --------------------------------------------------------------------------- #
def _materialize_wallet(b64_zip, wallet_password=""):
    """Decode the base64 ADB wallet zip into a fresh temp dir.

    Patches ``sqlnet.ora`` so ``WALLET_LOCATION`` points at the extracted
    directory (the shipped sqlnet.ora uses ``?/network/admin`` which only
    resolves when copied into ``$ORACLE_HOME``).

    Returns the absolute path to the wallet directory. The directory is not
    cleaned up explicitly: oracledb keeps file handles open for the life of
    the connection, and the OS-level tempdir is collected on process exit.
    """
    if not b64_zip:
        raise ValueError("wallet content is empty")
    raw = base64.b64decode(b64_zip)
    wallet_dir = tempfile.mkdtemp(prefix="adb_wallet_")
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        zf.extractall(wallet_dir)

    sqlnet_path = os.path.join(wallet_dir, "sqlnet.ora")
    if os.path.exists(sqlnet_path):
        try:
            with open(sqlnet_path, "r", encoding="utf-8") as fh:
                txt = fh.read()
            txt = txt.replace("?/network/admin", wallet_dir.replace("\\", "/"))
            with open(sqlnet_path, "w", encoding="utf-8") as fh:
                fh.write(txt)
        except Exception as e:  # pragma: no cover - defensive
            debug_warn("sqlnet.ora patch failed (continuing)", error=str(e))

    debug("wallet materialized", wallet_dir=wallet_dir,
          has_password=bool(wallet_password))
    return wallet_dir


def _open_connection(catalog_key, conf, runtime_params, context_vars):
    """Return an ``oracledb.Connection``.

    Priority order:
      1. ``catalog_key`` (runtime or conf) + ``aidp_io.get_connection_data``.
         Expects ``connectionProperties`` containing ``user.name`` / ``username``,
         ``password``, ``tns``, ``wallet.content`` (base64 zip), and optionally
         ``wallet.password``.
      2. Explicit ``conn_string`` + ``username`` + ``password`` runtime params,
         with optional ``wallet_path`` pointing at an already-unzipped wallet.
      3. Same fields read from conf as a last resort.

    Raises ``ValueError`` if the inputs are insufficient. oracledb errors
    propagate unchanged so the caller can format them into a structured
    error envelope (preserving ORA- codes).
    """
    import oracledb  # lazy import - the runtime ships it but local tests may not

    rp = runtime_params or {}

    # ---- 1) AIDP catalog binding (preferred) ---- #
    if catalog_key and _io_get_connection_data is not None:
        debug("opening connection via aidp_io.get_connection_data",
              catalog_key=catalog_key)
        try:
            data = _io_get_connection_data(catalog_key, conf, context_vars)
        except TypeError:
            # Some aidp_io variants accept (catalog_key) alone.
            data = _io_get_connection_data(catalog_key)
        cp = (data or {}).get("connectionProperties") or {}
        user = cp.get("user.name") or cp.get("username") or cp.get("user")
        pw = cp.get("password")
        tns = cp.get("tns") or cp.get("dsn") or cp.get("conn_string")
        wallet_b64 = cp.get("wallet.content") or cp.get("walletContent") or ""
        wallet_pw = cp.get("wallet.password") or cp.get("walletPassword") or ""
        missing = [k for k, v in (("user", user), ("password", pw), ("tns", tns)) if not v]
        if missing:
            raise ValueError(
                f"aidp_io.get_connection_data returned incomplete payload (missing {missing}); "
                f"verify the catalog binding exposes ADB credentials")
        kwargs = dict(user=user, password=pw, dsn=tns)
        if wallet_b64:
            wallet_dir = _materialize_wallet(wallet_b64, wallet_pw)
            os.environ["TNS_ADMIN"] = wallet_dir
            kwargs.update(
                config_dir=wallet_dir,
                wallet_location=wallet_dir,
                wallet_password=wallet_pw or None,
            )
        else:
            debug_warn("catalog returned no wallet content; attempting walletless DSN")
        return oracledb.connect(**kwargs)

    # ---- 2 & 3) explicit runtime + conf fallback ---- #
    user = rp.get("username") or get_cfg(conf, "username", "")
    pw = rp.get("password") or get_cfg(conf, "password", "")
    dsn = rp.get("conn_string") or get_cfg(conf, "conn_string", "")
    wallet_path = rp.get("wallet_path") or get_cfg(conf, "wallet_path", "")
    if not (user and pw and dsn):
        raise ValueError(
            "supply catalog_key, or conn_string + username + password "
            "(+ optional wallet_path) in runtime params or conf")
    kwargs = dict(user=user, password=pw, dsn=dsn)
    if wallet_path:
        kwargs.update(config_dir=wallet_path, wallet_location=wallet_path)
    debug("opening connection via explicit DSN", dsn=dsn,
          has_wallet=bool(wallet_path))
    return oracledb.connect(**kwargs)


# --------------------------------------------------------------------------- #
# Hashing + attribute builders. Hash inputs are normalized (uppercased schema
# / table names, sorted tables) so equivalent configs always hash the same.
# --------------------------------------------------------------------------- #
def _normalize_tables(target_tables):
    """CSV (or list) -> sorted list of uppercase, stripped, non-empty names."""
    if isinstance(target_tables, (list, tuple)):
        raw = [str(t) for t in target_tables]
    else:
        raw = str(target_tables or "").split(",")
    return sorted({t.strip().upper() for t in raw if t and t.strip()})


def _compute_hash(profile_name, tool_name, target_schema, tables_list,
                  llm_model_id, provider, credential_name, comments):
    """sha256 hex digest of the canonical config tuple, plus the JSON payload
    that was hashed (so we can store both the hash AND the literal blob for
    debugging / migration). Matches the spec verbatim."""
    payload = json.dumps({
        "profile_name": profile_name,
        "tool_name": tool_name,
        "target_schema": (target_schema or "").upper(),
        "target_tables": tables_list,
        "llm_model_id": llm_model_id,
        "provider": provider,
        "credential_name": credential_name or "",
        "comments": comments or "",
    }, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return digest, payload


def _build_profile_attributes(provider, llm_model_id, target_schema,
                              tables_list, credential_name):
    """Build the JSON ``attributes`` blob DBMS_CLOUD_AI.CREATE_PROFILE expects.

    ``object_list`` is the per-table catalog Select AI uses to constrain
    prompts (huge prompts otherwise). ``comments`` is left as a string
    ``"true"`` to match the documented DBMS_CLOUD_AI parameter shape.
    """
    obj_list = [{"owner": (target_schema or "").upper(), "name": t}
                for t in tables_list]
    attr = {
        "provider": provider,
        "model": llm_model_id,
        "object_list": obj_list,
        "comments": "true",
    }
    if credential_name:
        attr["credential_name"] = credential_name
    return json.dumps(attr)


def _build_agent_tool_attributes(profile_name):
    """Attributes blob for DBMS_CLOUD_AI_AGENT.CREATE_TOOL. The agent tool is
    bound to the profile by name; one-to-one with provisioning."""
    return json.dumps({
        "tool_type": "SQL",
        "tool_params": {"profile_name": profile_name},
    })


def _derive_profile_name(rp, context_vars):
    """Derive a profile name when the caller didn't pass one.

    Precedence:
      1. explicit ``profile_name`` runtime param
      2. ``aidp_<workspace_key>_<node_key>`` from context_vars (lowercased,
         sanitized to [A-Za-z0-9_], truncated to 120 chars)
      3. None - caller must error
    """
    explicit = (rp.get("profile_name") or "").strip()
    if explicit:
        return explicit
    ctx = context_vars or {}
    ws = (ctx.get("workspace_key") or ctx.get("workspaceKey") or "").strip()
    nk = (ctx.get("node_key") or ctx.get("nodeKey") or "").strip()
    if ws and nk:
        raw = f"aidp_{ws}_{nk}"
        clean = re.sub(r"[^A-Za-z0-9_]", "_", raw)[:120]
        return clean.lower()
    return ""


def _is_read_only(sql_text):
    """True iff the SQL's first keyword (ignoring leading comments + whitespace)
    is SELECT or WITH. Used by NL2SQLTool to guard RUNSQL execution."""
    return bool(_READ_ONLY_RE.match(sql_text or ""))


def _coerce_value(v):
    """Convert oracledb values into JSON-safe Python primitives.

    * LOB -> str (full read; callers cap row count, not row size)
    * datetime / date -> ISO-8601 string
    * everything else -> unchanged
    """
    if v is None:
        return None
    # LOBs expose .read(); checking attribute is cheaper than importing oracledb.
    if hasattr(v, "read") and not isinstance(v, (str, bytes, bytearray)):
        try:
            val = v.read()
            if isinstance(val, bytes):
                try:
                    return val.decode("utf-8")
                except UnicodeDecodeError:
                    return base64.b64encode(val).decode("ascii")
            return val
        except Exception:  # pragma: no cover - defensive
            return str(v)
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            return base64.b64encode(bytes(v)).decode("ascii")
    return v


def _rows_to_json(cur, max_rows):
    """Materialize a cursor into ``(columns_lowercased, rows, truncated)``.

    Columns are lowercased to match the convention used elsewhere in the
    AIDP custom toolkits. Each row becomes a dict keyed by those columns.
    Hits the spec's hard cap (``_HARD_ROW_CAP``) regardless of caller request.
    """
    cap = min(int(max_rows or 0) or _HARD_ROW_CAP, _HARD_ROW_CAP)
    cols = [d[0].lower() for d in (cur.description or [])]
    rows = []
    truncated = False
    for i, raw in enumerate(cur):
        if i >= cap:
            truncated = True
            break
        rows.append({cols[j]: _coerce_value(v) for j, v in enumerate(raw)})
    return cols, rows, truncated


def _ora_error_envelope(e, **extra):
    """Format an oracledb.DatabaseError into a structured fail envelope.

    Extracts the ORA- code from the message when possible so the caller can
    branch on it (e.g. ORA-12154 invalid TNS, ORA-20000 from DBMS_CLOUD_AI).
    Non-oracledb exceptions go through with ``error_type`` set to the class
    name and the raw stringified message.
    """
    msg = str(e)
    err_type = type(e).__name__
    ora_code = None
    m = re.search(r"(ORA-\d{4,5})", msg)
    if m:
        ora_code = m.group(1)
    payload = fail(msg, error_type=err_type, **extra)
    if ora_code:
        payload["ora_code"] = ora_code
    return DebugLog.embed(payload)


# --------------------------------------------------------------------------- #
# Tool 1: SelectAIProvisionTool
# --------------------------------------------------------------------------- #
@CustomToolBase.register
class SelectAIProvisionTool(CustomToolBase):
    """Provision an Oracle ADB Select AI profile + agent tool, idempotently.

    Workflow:
      1. Resolve inputs (profile_name, schema, table list, model, credential).
         Profile name defaults to ``aidp_<workspace_key>_<node_key>`` when
         the caller didn't pass one and the runtime injected those context
         vars; otherwise raises.
      2. Open an oracledb connection (catalog binding preferred, explicit
         DSN fallback).
      3. Ensure the audit table + unique index exist (DDL is wrapped in
         PRAGMA EXCEPTION_INIT(-955) so race conditions are harmless).
      4. Compute a sha256 hash of the canonical config tuple and look it up
         in the audit table.
           * No row     -> CREATE_PROFILE + CREATE_TOOL + INSERT, return
                           ``action: created``.
           * Hash match -> return ``action: unchanged`` (no DDL).
           * Hash diff  -> DROP_PROFILE + DROP_TOOL + recreate + UPDATE,
                           return ``action: recreated``.
           * force_recreate=True forces the drop+recreate path regardless.

    Returns a structured envelope with the audited fields so the agent can
    confirm the profile name, hash, and table list it just bound.
    """

    @classmethod
    def _validate_config(cls, conf, runtime_params=None, **context_vars):
        # Real validation happens against the live ADB connection - we can't
        # tell from conf alone whether DBMS_CLOUD_AI is installed or whether
        # the credential exists. Keep this lenient so the tool can be saved
        # in the Flow Designer without a live connection.
        return None

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        rp = runtime_params or {}

        # ---- resolve inputs ---- #
        profile_name = _derive_profile_name(rp, context_vars)
        target_schema = (rp.get("target_schema") or "").strip()
        target_tables = rp.get("target_tables") or ""
        llm_model_id = (
            (rp.get("llm_model_id") or "").strip()
            or get_cfg(conf, "default_llm_model_id", "xai.grok-4")
        )
        provider = (
            (rp.get("provider") or "").strip()
            or get_cfg(conf, "default_provider", "oci")
        ).lower()
        credential_name = (
            (rp.get("credential_name") or "").strip()
            or get_cfg(conf, "default_credential_name", "")
        )
        comments = rp.get("comments") or ""
        force_recreate = bool(rp.get("force_recreate", False)) and \
            (str(rp.get("force_recreate")).strip().lower() not in ("false", "0", ""))
        audit_table = (get_cfg(conf, "audit_table_name", "AIDP_NL2SQL_PROFILES")
                       or "AIDP_NL2SQL_PROFILES")
        catalog_key = (
            (rp.get("catalog_key") or "").strip()
            or get_cfg(conf, "catalog_key", "")
        )

        # ---- required-field validation ---- #
        if not profile_name:
            return DebugLog.embed(fail(
                "profile_name is required (or supply workspace_key + node_key context vars)",
                error_type="ValueError"))
        if not target_schema:
            return DebugLog.embed(fail("target_schema is required",
                                       error_type="ValueError"))
        tables_list = _normalize_tables(target_tables)
        if not tables_list:
            return DebugLog.embed(fail(
                "target_tables must be a non-empty comma-separated list",
                error_type="ValueError"))
        if provider not in ("oci", "openai", "cohere", "azure"):
            return DebugLog.embed(fail(
                f"unsupported provider {provider!r}; "
                f"expected one of oci/openai/cohere/azure",
                error_type="ValueError"))

        tool_name = profile_name  # one-to-one by convention; documented in README.

        config_hash, config_json = _compute_hash(
            profile_name, tool_name, target_schema, tables_list,
            llm_model_id, provider, credential_name, comments,
        )

        debug("SelectAIProvisionTool start",
              profile=profile_name, hash=config_hash, schema=target_schema.upper(),
              table_count=len(tables_list), model=llm_model_id, provider=provider,
              has_credential=bool(credential_name), force=force_recreate,
              audit_table=audit_table, via_catalog=bool(catalog_key))

        # ---- open the connection ---- #
        try:
            conn = _open_connection(catalog_key, conf, rp, context_vars)
        except Exception as e:
            debug_error("SelectAIProvisionTool connect failure", error=str(e))
            return _ora_error_envelope(e, profile_name=profile_name)

        try:
            # ---- 1) ensure audit table + index exist ---- #
            try:
                with conn.cursor() as cur:
                    cur.execute(_AUDIT_DDL.format(tab=audit_table))
                    cur.execute(_AUDIT_IX.format(tab=audit_table))
                conn.commit()
            except Exception as e:
                debug_error("audit table bootstrap failed", error=str(e))
                return _ora_error_envelope(e, stage="audit_bootstrap",
                                           profile_name=profile_name)

            # ---- 2) hash lookup ---- #
            existing_hash = None
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT CONFIG_HASH FROM {audit_table} "
                        f"WHERE PROFILE_NAME = :pn",
                        pn=profile_name,
                    )
                    row = cur.fetchone()
                    existing_hash = row[0] if row else None
            except Exception as e:
                debug_error("audit lookup failed", error=str(e))
                return _ora_error_envelope(e, stage="audit_lookup",
                                           profile_name=profile_name)

            # ---- 3a) hash match -> unchanged ---- #
            if existing_hash == config_hash and not force_recreate:
                payload = {
                    "action": "unchanged",
                    "profile_name": profile_name,
                    "tool_name": tool_name,
                    "config_hash": config_hash,
                    "target_schema": target_schema.upper(),
                    "target_tables": tables_list,
                    "llm_model_id": llm_model_id,
                    "provider": provider,
                }
                debug("SelectAIProvisionTool unchanged", profile=profile_name)
                return DebugLog.embed(ok(payload, **payload))

            # ---- 3b) hash mismatch (or force) -> drop existing artifacts ---- #
            action_taken = "created"
            if existing_hash is not None or force_recreate:
                try:
                    with conn.cursor() as cur:
                        cur.execute(_DROP_AGENT_TOOL_PLSQL, tn=tool_name)
                        cur.execute(_DROP_PROFILE_PLSQL, pn=profile_name)
                    conn.commit()
                    action_taken = "recreated"
                    debug("dropped previous artifacts", profile=profile_name,
                          tool=tool_name)
                except Exception as e:
                    # Drops are wrapped in WHEN OTHERS NULL on the server side
                    # so this only fires on hard connection errors. Surface and
                    # bail; partial state will be visible in the audit table.
                    debug_error("drop failed", error=str(e))
                    return _ora_error_envelope(e, stage="drop_existing",
                                               profile_name=profile_name)

            # ---- 3c) create profile + agent tool ---- #
            profile_attr_json = _build_profile_attributes(
                provider, llm_model_id, target_schema, tables_list, credential_name,
            )
            tool_attr_json = _build_agent_tool_attributes(profile_name)
            descr = (comments.strip() if comments else
                     f"AIDP NL2SQL profile {profile_name}")

            try:
                with conn.cursor() as cur:
                    cur.execute(
                        _CREATE_PROFILE_PLSQL,
                        pn=profile_name, attr=profile_attr_json, descr=descr,
                    )
                    cur.execute(
                        _CREATE_AGENT_TOOL_PLSQL,
                        tn=tool_name, attr=tool_attr_json,
                    )
                conn.commit()
            except Exception as e:
                debug_error("CREATE_PROFILE / CREATE_TOOL failed",
                            error=str(e), profile=profile_name)
                # Roll back any partial half-created state best-effort.
                try:
                    with conn.cursor() as cur:
                        cur.execute(_DROP_AGENT_TOOL_PLSQL, tn=tool_name)
                        cur.execute(_DROP_PROFILE_PLSQL, pn=profile_name)
                    conn.commit()
                except Exception:
                    pass
                return _ora_error_envelope(e, stage="create_profile_or_tool",
                                           profile_name=profile_name)

            # ---- 4) upsert the audit row ---- #
            tables_csv = ",".join(tables_list)
            try:
                with conn.cursor() as cur:
                    if existing_hash is not None:
                        cur.execute(
                            f"UPDATE {audit_table} SET "
                            f"TOOL_NAME=:tn, TARGET_SCHEMA=:ts, "
                            f"TARGET_TABLES_CSV=:tt, LLM_MODEL_ID=:mid, "
                            f"PROVIDER=:pv, CREDENTIAL_NAME=:cn, "
                            f"CONFIG_HASH=:h, CONFIG_JSON=:cj, "
                            f"UPDATED_AT=SYSTIMESTAMP "
                            f"WHERE PROFILE_NAME=:pn",
                            tn=tool_name, ts=target_schema.upper(),
                            tt=tables_csv, mid=llm_model_id, pv=provider,
                            cn=credential_name or None,
                            h=config_hash, cj=config_json, pn=profile_name,
                        )
                    else:
                        cur.execute(
                            f"INSERT INTO {audit_table} "
                            f"(PROFILE_NAME,TOOL_NAME,TARGET_SCHEMA,"
                            f"TARGET_TABLES_CSV,LLM_MODEL_ID,PROVIDER,"
                            f"CREDENTIAL_NAME,CONFIG_HASH,CONFIG_JSON) "
                            f"VALUES (:pn,:tn,:ts,:tt,:mid,:pv,:cn,:h,:cj)",
                            pn=profile_name, tn=tool_name,
                            ts=target_schema.upper(), tt=tables_csv,
                            mid=llm_model_id, pv=provider,
                            cn=credential_name or None,
                            h=config_hash, cj=config_json,
                        )
                conn.commit()
            except Exception as e:
                debug_error("audit upsert failed", error=str(e))
                # The profile + tool exist server-side; the audit row didn't
                # land. Bubble up so the caller can retry / inspect.
                return _ora_error_envelope(e, stage="audit_upsert",
                                           profile_name=profile_name)

            payload = {
                "action": action_taken,
                "profile_name": profile_name,
                "tool_name": tool_name,
                "config_hash": config_hash,
                "target_schema": target_schema.upper(),
                "target_tables": tables_list,
                "llm_model_id": llm_model_id,
                "provider": provider,
                "credential_name": credential_name or "",
            }
            debug("SelectAIProvisionTool done",
                  profile=profile_name, action=action_taken)
            return DebugLog.embed(ok(payload, **payload))

        except Exception as e:  # pragma: no cover - top-level safety net
            debug_error("SelectAIProvisionTool unexpected failure", error=str(e))
            try:
                conn.rollback()
            except Exception:
                pass
            return _ora_error_envelope(e, profile_name=profile_name)
        finally:
            try:
                conn.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Tool 2: NL2SQLTool
# --------------------------------------------------------------------------- #
@CustomToolBase.register
class NL2SQLTool(CustomToolBase):
    """Answer a natural-language question via Oracle Select AI.

    Action handling:
      * ``RUNSQL``     - two-step: first SHOWSQL to retrieve the SQL the
                         model produced, validate it's read-only via
                         ``_is_read_only`` (SELECT / WITH only, comments
                         allowed before the keyword), then execute it on the
                         same connection and marshal rows into list[dict]
                         keyed by lowercased column names. LOBs are read
                         in full; datetimes ISO-formatted; bytes utf-8 /
                         base64 fallback. Capped at ``max_rows`` (conf
                         default 100) and a hard ceiling of 10000 regardless.
      * ``SHOWSQL``    - returns the generated SQL as ``result`` + ``sql``,
                         plus a ``read_only`` boolean for defense-in-depth.
      * ``NARRATE``    - returns the model's prose verbatim under ``result``.
      * ``EXPLAINSQL`` - returns annotated SQL under ``result`` + ``sql``,
                         plus ``read_only`` boolean.

    Read-only guarantee:
      ``RUNSQL`` rejects anything whose first non-comment keyword is not
      SELECT or WITH. The rejection uses ``error_type='ReadOnlyViolation'``
      and includes the generated SQL in the failure envelope so the agent
      can either ask the user to rephrase or surface the SQL for review.
    """

    @classmethod
    def _validate_config(cls, conf, runtime_params=None, **context_vars):
        # Lenient like the provision tool - real validation needs a live ADB.
        return None

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        rp = runtime_params or {}

        # ---- resolve inputs ---- #
        profile_name = (
            (rp.get("profile_name") or "").strip()
            or get_cfg(conf, "default_profile_name", "")
        )
        prompt = rp.get("prompt") or ""
        action = (
            (rp.get("action") or "").strip()
            or get_cfg(conf, "default_action", "RUNSQL")
        ).upper()
        max_rows = get_cfg(conf, "max_rows", 100)
        try:
            if rp.get("max_rows") is not None and str(rp.get("max_rows")).strip() != "":
                max_rows = int(rp.get("max_rows"))
        except (TypeError, ValueError):
            pass
        max_rows = min(max(int(max_rows or 100), 1), _HARD_ROW_CAP)
        catalog_key = (
            (rp.get("catalog_key") or "").strip()
            or get_cfg(conf, "catalog_key", "")
        )
        safety_check = bool(get_cfg(conf, "safety_check", True))

        # ---- validation ---- #
        if not profile_name:
            return DebugLog.embed(fail(
                "profile_name is required (or set default_profile_name in conf)",
                error_type="ValueError"))
        if not str(prompt).strip():
            return DebugLog.embed(fail("prompt is required",
                                       error_type="ValueError"))
        if action not in ("RUNSQL", "SHOWSQL", "NARRATE", "EXPLAINSQL"):
            return DebugLog.embed(fail(
                f"unknown action {action!r}; "
                f"expected RUNSQL / SHOWSQL / NARRATE / EXPLAINSQL",
                error_type="ValueError"))

        debug("NL2SQLTool start",
              profile=profile_name, action=action,
              prompt_len=len(str(prompt)), max_rows=max_rows,
              via_catalog=bool(catalog_key), safety_check=safety_check)

        # ---- open the connection ---- #
        try:
            conn = _open_connection(catalog_key, conf, rp, context_vars)
        except Exception as e:
            debug_error("NL2SQLTool connect failure", error=str(e))
            return _ora_error_envelope(e, profile_name=profile_name)

        try:
            # ---- RUNSQL: two-step with safety guard ---- #
            if action == "RUNSQL":
                generated_sql = ""
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            _GENERATE_SQL,
                            prompt=str(prompt), pn=profile_name, action="SHOWSQL",
                        )
                        row = cur.fetchone()
                        generated_sql = (_coerce_value(row[0]) if row else "") or ""
                except Exception as e:
                    debug_error("SHOWSQL pre-fetch failed", error=str(e))
                    return _ora_error_envelope(e, profile_name=profile_name,
                                               stage="showsql_prefetch")

                if safety_check and not _is_read_only(generated_sql):
                    debug_warn("NL2SQLTool refusing non-SELECT statement",
                               profile=profile_name, sql_preview=generated_sql[:200])
                    payload = fail(
                        "Refusing to execute non-SELECT statement. "
                        "Only SELECT / WITH statements are permitted.",
                        error_type="ReadOnlyViolation",
                        profile_name=profile_name,
                        generated_sql=generated_sql,
                    )
                    return DebugLog.embed(payload)

                try:
                    with conn.cursor() as cur:
                        cur.execute(generated_sql)
                        cols, rows, truncated = _rows_to_json(cur, max_rows)
                except Exception as e:
                    debug_error("RUNSQL execute failed", error=str(e))
                    return _ora_error_envelope(
                        e, profile_name=profile_name,
                        stage="runsql_execute", generated_sql=generated_sql)

                payload = {
                    "action": "RUNSQL",
                    "profile_name": profile_name,
                    "sql": generated_sql,
                    "columns": cols,
                    "row_count": len(rows),
                    "rows": rows,
                    "truncated": truncated,
                    "max_rows": max_rows,
                }
                debug("NL2SQLTool RUNSQL done",
                      profile=profile_name, row_count=len(rows),
                      truncated=truncated)
                return DebugLog.embed(ok(payload, **payload))

            # ---- SHOWSQL / NARRATE / EXPLAINSQL: single GENERATE call ---- #
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        _GENERATE_SQL,
                        prompt=str(prompt), pn=profile_name, action=action,
                    )
                    row = cur.fetchone()
                    text = (_coerce_value(row[0]) if row else "") or ""
            except Exception as e:
                debug_error(f"{action} GENERATE failed", error=str(e))
                return _ora_error_envelope(e, profile_name=profile_name,
                                           stage=f"{action.lower()}_generate")

            payload = {
                "action": action,
                "profile_name": profile_name,
                "result": text,
            }
            if action in ("SHOWSQL", "EXPLAINSQL"):
                payload["sql"] = text
                payload["read_only"] = _is_read_only(text)
            debug("NL2SQLTool done", profile=profile_name, action=action,
                  result_len=len(text or ""))
            return DebugLog.embed(ok(payload, **payload))

        except Exception as e:  # pragma: no cover - top-level safety net
            debug_error("NL2SQLTool unexpected failure", error=str(e))
            return _ora_error_envelope(e, profile_name=profile_name)
        finally:
            try:
                conn.close()
            except Exception:
                pass
