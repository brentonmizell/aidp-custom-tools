"""Shared AIDP data-lake discovery — the whole-Master-catalog walk, so any
tool can let an agent find catalogs -> schemas -> tables/volumes/KBs and the
file locations, without duplicating the key/surface handling.

Self-contained: builds its own OCI signer (from the standard 5-key credential
via credential_resolver) and does the walk over HTTP. One entry point:

    map_data_lake(conf, get_cfg, only_catalog="") -> dict

The result carries a nested `catalogs` tree, a `summary` count, and a `text`
tree. Volumes/KBs include a `location` (/Volumes/<catalog>/<schema>/<volume>)
so an agent can hand the user a file path.

Key/surface rules baked in (see AIDP_KEY_MODEL.md):
  - catalogKey  = the catalog NAME (its `key`; the hex is `catalogGuid`)
  - schemaKey   = the schema's DOTTED key catalog.schema
  - /knowledgeBases is served on 20240831/dataLakes (404s on aiDataPlatforms)
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

_JOB_KB_TERMINAL = None  # placeholder to keep linters quiet; unused


def _volume_location(volume_key: str) -> str:
    """catalog.schema.volume -> /Volumes/catalog/schema/volume (the path AIDP
    reports for a volume, e.g. in a KB source `location`)."""
    parts = str(volume_key or "").split(".")
    return "/Volumes/" + "/".join(parts) if len(parts) >= 1 and parts[0] else ""


def _render_text(tree: list, counts: dict) -> str:
    lines = [f"{counts['catalogs']} catalogs | {counts['schemas']} schemas | "
             f"{counts['tables']} tables | {counts['volumes']} volumes | "
             f"{counts['knowledge_bases']} KBs", ""]
    for c in tree:
        lines.append(f"* {c['catalog_key']} [{c.get('type') or '?'}]")
        for s in c.get("schemas", []):
            lines.append(f"  - {s.get('schema_name') or s.get('schema_key')}")
            for label, key in (("tables", "tables"), ("volumes", "volumes"),
                               ("KBs", "knowledge_bases")):
                items = s.get(key, [])
                if items:
                    names = ", ".join(i.get("name") or i.get("key") for i in items)
                    lines.append(f"      {label} ({len(items)}): {names}")
            if s.get("errors"):
                lines.append(f"      ! errors: {s['errors']}")
    return "\n".join(lines)


def build_client(conf: Any, get_cfg: Callable) -> Tuple[Optional[Any], str, str, Optional[str]]:
    """Resolve (signer, base, kb_base, error) from the standard 5-key
    credential (conf.credential_name): signer + data_lake_ocid + inferred
    region from the bundle. base is the aiDataPlatforms surface; kb_base is
    the dataLakes surface used for /knowledgeBases."""
    try:
        from .credential_resolver import (
            resolve_oci_signer, enrich_conf_from_bundle, resolve_region)
    except ImportError as ex:  # pragma: no cover
        return None, "", "", f"credential_resolver missing: {ex}"

    conf = enrich_conf_from_bundle(conf)
    cred = get_cfg(conf, "credential_name", "")
    signer, _meta, err = resolve_oci_signer(cred)
    if err:
        return None, "", "", err
    if signer is None:
        return None, "", "", ("no credential resolved — set conf.credential_name "
                              "to your 5-key AIDP credential")
    lake = str(get_cfg(conf, "data_lake_ocid", "")).strip()
    if not lake:
        return None, "", "", ("data_lake_ocid not found on the credential bundle "
                              "or conf")
    region = resolve_region(ocid=lake, conf_region=get_cfg(conf, "region", ""))
    api_version = str(get_cfg(conf, "api_version", "20260430")).strip() or "20260430"
    service_path = str(get_cfg(conf, "service_path", "aiDataPlatforms")).strip() or "aiDataPlatforms"
    host = f"https://aidp.{region}.oci.oraclecloud.com"
    base = f"{host}/{api_version}/{service_path}/{lake}"
    kb_base = f"{host}/20240831/dataLakes/{lake}"
    return signer, base, kb_base, None


def map_data_lake(conf: Any, get_cfg: Callable, only_catalog: str = "",
                  timeout: int = 30) -> Dict[str, Any]:
    """Walk catalogs -> schemas -> tables/volumes/KBs. Returns
    {ok, summary, text, catalogs, ...} or {ok: False, error}."""
    import requests
    from urllib.parse import quote

    signer, base, kb_base, err = build_client(conf, get_cfg)
    if err:
        return {"ok": False, "error": err, "error_type": "CredentialStoreError"}

    only_catalog = str(only_catalog or "").strip()

    def gi(url):
        try:
            r = requests.get(url, auth=signer, timeout=timeout,
                             headers={"Accept": "application/json"})
            r.raise_for_status()
            body = r.json()
            return body.get("items", body if isinstance(body, list) else []), None
        except requests.HTTPError as e:
            st = e.response.status_code if e.response is not None else "?"
            return [], f"HTTP {st}"
        except Exception as e:
            return [], type(e).__name__

    def get_kbs(cat_q, s_dotted, s_plain):
        for b, sk in ((kb_base, s_plain), (kb_base, s_dotted),
                      (base, s_plain), (base, s_dotted)):
            items, e = gi(f"{b}/knowledgeBases?catalogKey={cat_q}"
                          f"&schemaKey={quote(sk, safe='')}&limit=1000")
            if e is None:
                return items, None
        return [], "HTTP 404"

    catalogs, cat_err = gi(f"{base}/catalogs")
    if cat_err:
        return {"ok": False, "error": f"list catalogs failed: {cat_err}",
                "error_type": "HTTPError"}

    counts = {"catalogs": 0, "schemas": 0, "tables": 0, "volumes": 0,
              "knowledge_bases": 0}
    tree = []
    for c in catalogs:
        c_name = c.get("key") or c.get("displayName")
        if only_catalog and c_name != only_catalog \
                and c.get("displayName") != only_catalog:
            continue
        counts["catalogs"] += 1
        c_q = quote(c_name, safe="")
        schemas, s_err = gi(f"{base}/schemas?catalogKey={c_q}")
        c_node = {"catalog_key": c_name,
                  "type": c.get("catalogType") or c.get("type"),
                  "schemas": [], "error": s_err}
        for s in schemas:
            counts["schemas"] += 1
            s_key = s.get("key") or s.get("displayName")   # dotted
            s_plain = s.get("displayName") or s_key.split(".")[-1]
            s_q = quote(s_key, safe="")
            tbls, t_err = gi(f"{base}/tables?catalogKey={c_q}&schemaKey={s_q}")
            vols, v_err = gi(f"{base}/volumes?catalogKey={c_q}&schemaKey={s_q}")
            kbs, k_err = get_kbs(c_q, s_key, s_plain)
            counts["tables"] += len(tbls)
            counts["volumes"] += len(vols)
            counts["knowledge_bases"] += len(kbs)
            c_node["schemas"].append({
                "schema_key": s.get("key"),
                "schema_name": s.get("displayName"),
                "tables": [{"key": t.get("key"),
                            "name": t.get("displayName") or t.get("name")}
                           for t in tbls],
                "volumes": [{"key": v.get("key"),
                             "name": v.get("displayName") or v.get("name"),
                             "location": _volume_location(v.get("key", ""))}
                            for v in vols],
                "knowledge_bases": [{"key": k.get("key"),
                                     "name": k.get("displayName") or k.get("name")}
                                    for k in kbs],
                "errors": {kk: e for kk, e in
                           (("tables", t_err), ("volumes", v_err), ("kbs", k_err))
                           if e},
            })
        tree.append(c_node)

    return {
        "ok": True,
        "operation": "map",
        "scope": only_catalog or "(all catalogs)",
        "summary": counts,
        "text": _render_text(tree, counts),
        "catalogs": tree,
    }
