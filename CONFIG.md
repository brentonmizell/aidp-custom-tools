# Custom Tools — Config & Build

This doc explains how `build_with_config.py` auto-fills tool `conf` defaults
at build time, what you have to fill in yourself, and exactly where in the
AIDP console (or your local files) to find each value.

## TL;DR

```
# Easiest path — interactive wizard that does init + configure + build:
python setup.py

# Non-interactive (assumes ~/.aidp + ~/.oci already exist):
python setup.py build
```

`setup.py` is the recommended entry point. It walks you through generating
the configs below if missing, then prompts you for the workspace-specific
values (catalog / schema / volume / KB / etc.) per tool, then rebuilds the
zips. See `README.md` for the full subcommand list.

`build_with_config.py` is the old, non-interactive build (equivalent to
`python setup.py build`).

Then `git add . && git commit && git push`, or upload the rebuilt zips
directly via the AIDP console / VS Code extension.

## What the build script auto-fills

The script walks every `CUSTOM_CODE_TOOLS/<pkg>/src/tool_config.json` and,
for any conf key listed here, replaces the value if (a) it's empty or (b)
you passed `--force`.

| Conf key | Filled from | Notes |
|---|---|---|
| `region` | `aidp-deploy.config.json → region` | e.g. `us-ashburn-1` |
| `data_lake_ocid` | `aidp-deploy.config.json → dataLakeOcid` | OCID of your AIDP data lake |
| `workspace_id` | `aidp-deploy.config.json → workspaceId` | Workspace UUID |
| `api_version` | `aidp-deploy.config.json → apiVersion` | e.g. `20260430` |
| `tenancy_ocid` | `~/.oci/config → tenancy` | OCID of your OCI tenancy |
| `user_ocid` | `~/.oci/config → user` | OCID of the API-key user |
| `fingerprint` | `~/.oci/config → fingerprint` | API-key fingerprint |

By default the script picks the `DEFAULT` profile from `~/.oci/config`. Override with
`--profile <name>` or the env var `AIDP_OCI_PROFILE`.

## What you have to fill yourself — and where to find each value

These are intentionally **never** auto-filled because they're either
workspace-specific (you'd be picking from many) or sensitive.

### Catalog / schema / volume / table / KB names

These vary per agent flow. The `_uiHints` sidecar (see `UI_HINTS_SPEC.md`)
turns them into dropdowns inside the AIDP Flow Designer VS Code extension.
Inside the **AIDP console**, they're text inputs — here's where to find
each value:

| Field | Where in the AIDP console |
|---|---|
| `catalog` | **Catalogs** → pick a Standard catalog → copy the display name |
| `schema` | **Catalogs** → `<catalog>` → **Schemas** → copy the display name |
| `volume` | **Catalogs** → `<catalog>` → `<schema>` → **Volumes** → copy the name |
| `volume_key` | Same screen as `volume` — click the volume → URL contains the key |
| `kb_key` | **Knowledge Bases** → click a KB → URL contains the key (`<catalog>.<schema>.<kbName>`) |
| `job_key` | Inside a KB → **Ingestion jobs** → copy the job key |
| `table_key` | **Catalogs** → `<catalog>` → `<schema>` → **Tables** → click table → URL contains key |
| `catalog_key` | Same as `catalog` but the UUID, visible at the bottom of the catalog detail |
| `schema_key` | Same as `schema` but the UUID, visible at the bottom of the schema detail |

### Secrets

Never committed; the build script skips these entirely.

| Field | Where to put it |
|---|---|
| `private_key_content` | Read at runtime from a secret store / env var (do NOT paste into `tool_config.json`) |
| `pass_phrase` | Same |
| `webhook_url` | Same (the URL is effectively a bearer token) |
| `smtp_password`, `imap_password` | Same |

### OCI-specific (genai_toolkit, object_storage_tool)

| Field | Where to find it |
|---|---|
| `compartment_id` | **OCI Console** → Identity & Security → Compartments → copy the OCID |
| `model_id` | Use one of the IDs from `.aidp/knowledge/oci-genai-models.md` (in the extension repo) or **OCI Console** → AI → Generative AI → Models |
| `bucket` | **OCI Console** → Storage → Object Storage → Buckets → copy the name |
| `namespace` | **OCI Console** → top-right menu → Tenancy info → Namespace (auto-detected at runtime if blank) |

### Workflow / notebook (python_runner_tool)

| Field | Where to find it |
|---|---|
| `cluster_key` | **AIDP Console** → Compute → click cluster → URL contains the key |
| `notebook_path` | **AIDP Console** → Workspace tree → right-click notebook → copy path (e.g. `/Workspace/Notebooks/foo.ipynb`) |

## Generating the configs

### `~/.aidp/aidp-deploy.config.json`

The AIDP Flow Designer VS Code extension (and Jacques's `aidp-deploy`)
creates this when you run **AIDP: Connect to Workspace**. If you don't have
it, create it manually:

```json
{
  "region": "us-ashburn-1",
  "apiVersion": "20260430",
  "dataLakeOcid": "ocid1.aidataplatform.oc1.iad.<yourocid>",
  "workspaceId": "<workspace-uuid>",
  "consoleBaseUrl": "https://<your-workspace>.datalake.oci.oraclecloud.com",
  "workspaceDisplayName": "<your-ws-name>"
}
```

You can find these values in the AIDP console: top-right menu → Workspace info.

### `~/.oci/config`

Standard OCI CLI config. If you don't have one, run:

```
oci setup config
```

…and follow the prompts. The script reads the `[DEFAULT]` profile by
default; pass `--profile <name>` if you use another.

## API version & resource path (potential future migration)

AIDP exposes two REST surfaces depending on tenancy:

| `apiVersion` | URL resource segment (`service_path`) | Status |
|---|---|---|
| `20240831` | `dataLakes` | Currently live on every tenancy I've seen |
| `20260430` | `aiDataPlatforms` | Future shape; documented in the public Oracle SDK |

A tool deployed with the wrong combination returns 404 on every request:
```
GET /20260430/aiDataPlatforms/<lake>/volumes/<key>/...
->  404 NotAuthorizedOrNotFound
```
…even when the OCID is right and auth is fine. AIDP just doesn't have the
endpoint on your tenancy yet.

`setup.py build` (and `build_with_config.py`) handle this automatically:

- `api_version` is **always** overwritten with the value in
  `~/.aidp/aidp-deploy.config.json` — even if the tool's `tool_config.json`
  has a different default.
- `service_path` is **derived** from `api_version` via a small lookup table
  (`API_VERSION_TO_SERVICE_PATH` in both scripts).

**If you're building a NEW custom tool**, don't hard-code `api_version` or
`service_path` to a literal — leave them as `""` in `tool_config.json` and
let the build inject them. That way the tool follows the platform when
AIDP migrates to `20260430` and you don't have to re-edit every package.

**If AIDP migrates your tenancy to 20260430**:
1. Edit `~/.aidp/aidp-deploy.config.json`, set `"apiVersion": "20260430"`.
2. Run `python setup.py build`.
3. `service_path` flips to `aiDataPlatforms` automatically; every zip is
   rebuilt; re-upload via the AIDP console.

That's it — no per-tool editing.

## Build script flags

```
python build_with_config.py [--profile NAME] [--aidp-config PATH]
                            [--force] [--dry-run] [--no-zip]
```

- `--profile` — pick an OCI config profile (or set `$AIDP_OCI_PROFILE`).
- `--aidp-config` — point at a non-default AIDP config file.
- `--force` — overwrite existing non-empty conf values (default: only fill empties).
- `--dry-run` — print what would change; don't write or rebuild zips.
- `--no-zip` — fill values; skip the zip rebuild.

## What's in a built package

Each `CUSTOM_CODE_TOOLS/<pkg>/<pkg>.zip` contains:

```
tool_config.json       (tool metadata + conf + schema + _uiHints)
tool_implementation.py (registered @tool classes)
requirements.txt       (pip deps installed at deploy)
README.md
utils/                 (shared helpers — config_utils.py, etc.)
```

Upload via the AIDP console (**Tools** → **Upload custom tool**) or via the
AIDP Flow Designer VS Code extension's tool-package register command.
