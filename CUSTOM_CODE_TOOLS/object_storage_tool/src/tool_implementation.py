"""
Object Storage Tool
====================
List, read, write, or delete objects in an OCI Object Storage bucket using
resource-principal auth. Use to read or persist artifacts (files, exports,
intermediate results) an agent produces. No keys handled; auth is OCI RP.
"""

import base64
import time

from aidputils.agents.tools.custom_tools.base import CustomToolBase
from .utils.config_utils import get_cfg

# Debug channel (with no-op fallback when runtime doesn't inject it).
try:
    from aidp_debug import debug, debug_warn, debug_error, DebugLog  # type: ignore
except ImportError:  # pragma: no cover
    def debug(*args, **kwargs): pass
    def debug_warn(*args, **kwargs): pass
    def debug_error(*args, **kwargs): pass
    class DebugLog:  # type: ignore
        @staticmethod
        def embed(result):
            return result


def _ok(data, **extra):
    out = {"ok": True, "data": data}
    out.update(extra)
    return out


def _err(error, error_type="ToolError", **extra):
    out = {"ok": False, "error": str(error), "error_type": error_type}
    out.update(extra)
    return out


def _is_retryable(exc) -> bool:
    """Decide if an OCI/network error is worth retrying."""
    try:
        import oci
        if isinstance(exc, oci.exceptions.ServiceError):
            return exc.status in (408, 429, 500, 502, 503, 504)
    except Exception:
        pass
    # Network / transport errors
    name = type(exc).__name__.lower()
    return any(s in name for s in ("timeout", "connection", "transient"))


def _with_retry(fn, max_retries: int, op_name: str):
    """Run fn() with bounded retries on transient errors."""
    attempt = 0
    delay = 0.5
    while True:
        try:
            return fn()
        except Exception as e:
            if attempt >= max_retries or not _is_retryable(e):
                raise
            debug_warn(f"object_storage:{op_name} transient error, retry {attempt + 1}/{max_retries}: {e}")
            time.sleep(delay)
            attempt += 1
            delay = min(delay * 2, 4.0)


@CustomToolBase.register
class ObjectStorageTool(CustomToolBase):
    """List, read, write, or delete objects in an OCI Object Storage bucket
    (resource-principal auth). Use to read or persist files and artifacts an
    agent works with."""

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        op = (runtime_params.get("operation", "") or "").lower()
        name = runtime_params.get("name", "")
        content = runtime_params.get("content", "")

        bucket = get_cfg(conf, "bucket", "")
        namespace = get_cfg(conf, "namespace", "")
        region = get_cfg(conf, "region", "")
        prefix = get_cfg(conf, "prefix", "")
        max_keys = get_cfg(conf, "max_keys", 200)
        max_bytes = get_cfg(conf, "max_bytes", 1048576)  # 1 MiB default cap
        max_retries = get_cfg(conf, "max_retries", 3)

        debug(f"object_storage op={op!r} bucket={bucket!r} ns={namespace!r} region={region!r} name={name!r}")

        if not bucket:
            debug_error("object_storage: bucket missing in tool config")
            result = _err("bucket is required in tool config", "ConfigError")
            return DebugLog.embed(result)

        if op not in ("list", "get", "put", "delete"):
            debug_error(f"object_storage: invalid operation {op!r}")
            result = _err("operation must be one of: list, get, put, delete", "ValidationError")
            return DebugLog.embed(result)

        try:
            import oci
            # Auth resolution:
            #   1. conf.credential_name -> aidputils.secrets bundle -> Signer
            #   2. else fall through to resource principal (existing behavior)
            signer = None
            try:
                from .utils.credential_resolver import resolve_oci_signer
                cred_name = get_cfg(conf, "credential_name", "")
                signer, _meta, cred_err = resolve_oci_signer(cred_name)
                if cred_err:
                    debug_error(f"object_storage: credential_name='{cred_name}' failed: {cred_err}")
                    result = _err(cred_err, "CredentialStoreError")
                    return DebugLog.embed(result)
            except ImportError:
                pass
            if signer is None:
                signer = oci.auth.signers.get_resource_principals_signer()
                debug("object_storage: using resource principal signer (credential_name not set)")
            else:
                debug("object_storage: using Credential Store signer")
            client_kwargs = {"config": {}, "signer": signer}
            if region:
                client_kwargs["config"] = {"region": region}
            client = oci.object_storage.ObjectStorageClient(**client_kwargs)

            if not namespace:
                namespace = _with_retry(
                    lambda: client.get_namespace().data,
                    max_retries,
                    "get_namespace",
                )
                debug(f"object_storage: auto-detected namespace={namespace!r}")

            if op == "list":
                resp = _with_retry(
                    lambda: client.list_objects(
                        namespace, bucket,
                        prefix=(runtime_params.get("name") or prefix) or None,
                        limit=int(max_keys),
                    ),
                    max_retries,
                    "list_objects",
                )
                objs = [{"name": o.name, "size": o.size} for o in resp.data.objects]
                data = {"count": len(objs), "objects": objs}
                result = _ok(data, count=len(objs), objects=objs)
                return DebugLog.embed(result)

            if op == "get":
                if not name:
                    debug_error("object_storage:get missing name")
                    return DebugLog.embed(_err("name is required for get", "ValidationError"))

                # First check size via head_object so we don't blindly load huge blobs.
                head = _with_retry(
                    lambda: client.head_object(namespace, bucket, name),
                    max_retries,
                    "head_object",
                )
                content_length = None
                try:
                    content_length = int(head.headers.get("content-length", 0) or 0)
                except Exception:
                    content_length = None
                if content_length is not None and content_length > int(max_bytes):
                    debug_warn(
                        f"object_storage:get object {name!r} size={content_length} exceeds max_bytes={max_bytes}; "
                        f"reading first {max_bytes} bytes"
                    )

                # Bound the read with an explicit Range request when we have a cap.
                # max_bytes is the inclusive upper bound on bytes returned.
                read_to = int(max_bytes) - 1
                range_header = f"bytes=0-{read_to}"
                resp = _with_retry(
                    lambda: client.get_object(namespace, bucket, name, range=range_header),
                    max_retries,
                    "get_object",
                )

                # Stream into a bounded buffer to enforce cap even if server ignores range.
                buf = bytearray()
                limit = int(max_bytes)
                # resp.data.raw is the urllib3 response; resp.data.content also works
                # but loads everything. Prefer iter_content if available.
                truncated = False
                stream = getattr(resp.data, "iter_content", None)
                if callable(stream):
                    for chunk in stream(chunk_size=65536):
                        if not chunk:
                            continue
                        remaining = limit - len(buf)
                        if remaining <= 0:
                            truncated = True
                            break
                        if len(chunk) > remaining:
                            buf.extend(chunk[:remaining])
                            truncated = True
                            break
                        buf.extend(chunk)
                else:
                    raw = resp.data.content
                    if len(raw) > limit:
                        buf.extend(raw[:limit])
                        truncated = True
                    else:
                        buf.extend(raw)
                if content_length is not None and content_length > limit:
                    truncated = True

                raw_bytes = bytes(buf)
                try:
                    text = raw_bytes.decode("utf-8")
                    data = {
                        "name": name,
                        "content": text,
                        "bytes": len(raw_bytes),
                        "total_bytes": content_length,
                        "truncated": truncated,
                        "max_bytes": int(max_bytes),
                        "binary": False,
                    }
                    # Legacy top-level fields preserved.
                    return DebugLog.embed(_ok(
                        data,
                        name=name,
                        content=text,
                        bytes=len(raw_bytes),
                        truncated=truncated,
                    ))
                except UnicodeDecodeError:
                    b64 = base64.b64encode(raw_bytes).decode()
                    data = {
                        "name": name,
                        "content_base64": b64,
                        "bytes": len(raw_bytes),
                        "total_bytes": content_length,
                        "truncated": truncated,
                        "max_bytes": int(max_bytes),
                        "binary": True,
                    }
                    return DebugLog.embed(_ok(
                        data,
                        name=name,
                        content_base64=b64,
                        bytes=len(raw_bytes),
                        binary=True,
                        truncated=truncated,
                    ))

            if op == "put":
                if not name:
                    debug_error("object_storage:put missing name")
                    return DebugLog.embed(_err("name is required for put", "ValidationError"))
                body = content.encode("utf-8") if isinstance(content, str) else (content or b"")
                if len(body) > int(max_bytes):
                    debug_error(f"object_storage:put body {len(body)}B exceeds max_bytes={max_bytes}")
                    return DebugLog.embed(_err(
                        f"content size {len(body)} exceeds max_bytes {max_bytes}",
                        "PayloadTooLarge",
                        max_bytes=int(max_bytes),
                    ))
                _with_retry(
                    lambda: client.put_object(namespace, bucket, name, body),
                    max_retries,
                    "put_object",
                )
                data = {"name": name, "written_bytes": len(body)}
                return DebugLog.embed(_ok(data, name=name, written_bytes=len(body)))

            if op == "delete":
                if not name:
                    debug_error("object_storage:delete missing name")
                    return DebugLog.embed(_err("name is required for delete", "ValidationError"))
                _with_retry(
                    lambda: client.delete_object(namespace, bucket, name),
                    max_retries,
                    "delete_object",
                )
                data = {"name": name, "deleted": True}
                return DebugLog.embed(_ok(data, name=name, deleted=True))

            # Unreachable due to early validation, but kept defensive.
            return DebugLog.embed(_err("operation must be one of: list, get, put, delete", "ValidationError"))
        except Exception as e:
            debug_error(f"object_storage:{op} failed: {type(e).__name__}: {e}")
            return DebugLog.embed(_err(e, type(e).__name__))
