# AIDP Custom Code Tool — Feedback & Feature Requests

**Author**: Brenton Mizell (brenton.mizell@oracle.com)
**Repo**: https://github.com/brentonmizell/aidp-custom-tools (private)
**Status**: Open — needs AIDP product/engineering input

A consolidated ticket covering four persistent gaps surfaced while
building a curated set of 11 Custom Code tool packages (~22 tools).
Each section names the gap, lists the workarounds already in place,
and proposes a concrete ask of the AIDP team.

---

## Issue 1 — Test panel ignores all JSON Schema metadata

### Current behavior

The AIDP Test panel for any Custom Code tool renders only:

- The field **name** (e.g. `from_format`)
- A hardcoded generic placeholder: `Enter`

Concretely, the panel for `ConvertFileTool` shows:

```
from_format    [Enter]
to_format      [Enter]
content        [Enter]
input_path     [Enter]
output_path    [Enter]
source_uri     [Enter]
dest_uri       [Enter]
```

What's in the schema and NOT rendered:

| JSON Schema field | Example value populated | Visible in Test panel? |
|---|---|---|
| `description` | "URI like `master:cat.sch.vol:/path` or `workspace:/path`" | ❌ No |
| `examples` | `["master:sales.raw.uploads:/q1.csv", "workspace:/exports/q1.parquet"]` | ❌ No |
| `default` | `"master:sales_catalog.raw.uploads:/q1_orders.csv"` | ❌ No |
| `enum` | `["csv", "json", "parquet", "xlsx", "tsv", "txt"]` | ⚠️ Partially (renders as text input, not select) |

### Impact

A developer testing the tool has no idea what shape any field expects.
They have to read the source `tool_config.json` to discover that
`from_format` accepts `csv | json | parquet | xlsx | tsv | txt`, or that
`source_uri` is a URI with a specific format. Internal users can do
this; customers won't.

Worse: the **agent** that calls the tool at runtime DOES see the full
schema (descriptions, examples, defaults) and uses them well. So the
tool works correctly when the agent invokes it but fails the
"can a human exercise this from the console" bar.

### Workarounds in place

- **Agent-directive descriptions** (~600 chars each, with "When to use /
  How to discover inputs / How to communicate / Example" sections) on
  every tool. Invisible to humans in the panel; the LLM reads them.
- **JSON Schema `examples` arrays** with 2-3 realistic values on every
  string field. Invisible to humans.
- **JSON Schema `default` values** on 87 fields — at minimum the first
  example as a default. Invisible to humans (confirmed: the input still
  says "Enter").
- **`_confDescriptions` sidecar** that the VS Code extension UI reads.
  Stripped from the zip (see Issue 4 below).
- A markdown `CONFIG.md` in the repo mirroring every field with a
  "where to find this value" hint. Helpful, but requires the developer
  to leave the AIDP console.

### Ask

In the Test panel, render at minimum these JSON Schema fields per input:

1. **`description`** — show as helper text below the field label.
2. **`default`** — pre-populate as the initial input value (user clears or edits).
3. **`examples`** — render as click-to-fill chips above/below the input.
4. **`enum`** — render as a `<select>` instead of a free-text input.

Even just rendering `description` and pre-populating `default` would
remove ~90% of the "what do I put here?" friction. The metadata is
already populated correctly across the curated tool set — just
currently invisible.

---

## Issue 2 — No native catalog-backed pickers in the Test panel

### Current behavior

To call `CatalogFileTool` in the Test panel, the developer has to
manually type:

- `volume_key` (the dot-form key, e.g. `construction_catalog.construction_schema.construction_documents`)
- OR a catalog/schema/volume name triple
- AND the file `path` within the volume

There's no way to **browse** the live catalog from the Test panel. The
developer has to open another browser tab, navigate AIDP Console →
Catalogs → click around to find names, copy them back into the Test
panel. Mistakes (volume_key spelled wrong, wrong dot-form) are
reported as the generic `404 NotAuthorizedOrNotFound` which doesn't
distinguish auth failures from name-resolution failures.

This makes tool development feel hostile — the developer's first three
attempts will 404, and only the fourth one (after Console
cross-reference) succeeds.

### Workarounds in place

The repo defines a sidecar contract called `_uiHints` that lives next
to each tool's `conf` and `schema` blocks. It declares each field's
UI intent:

```json
"_uiHints": {
  "conf": {
    "catalog": { "kind": "dropdown", "source": "catalogs" },
    "schema":  { "kind": "dropdown", "source": "schemas",  "dependsOn": "catalog" },
    "volume":  { "kind": "dropdown", "source": "volumes",  "dependsOn": "schema" },
    "auth_mode": { "kind": "enum", "values": ["resource_principal", "user_principal", "instance_principal"] },
    "private_key_content": { "kind": "secret" }
  },
  "schema": { ... }
}
```

Proposed sources: `regions`, `dataLakes`, `catalogs`, `schemas` (deps
catalog), `tables` (deps schema), `volumes` (deps schema),
`knowledgeBases`, `jobs` (deps kb), `workspaces`, `clusters` (deps
workspace), `notebookFiles` (deps workspace), `compartments`,
`ociGenAiModels` (deps region), `ociBuckets`.

The full spec is in [UI_HINTS_SPEC.md](UI_HINTS_SPEC.md). The VS Code
extension (AIDP Flow Designer) consumes this to render cascading
catalog-backed dropdowns.

**Critical**: `_uiHints` must be stripped from the deployed zip
because AIDP's `CustomToolEntry` Pydantic model rejects unknown
top-level keys with `ValidationError`. See Issue 4 below.

### Ask

Native catalog-backed pickers in the Test panel. Two paths to evaluate:

**Path A — Adopt the `_uiHints` contract verbatim.**
These hints already exist in every tool config in the curated set.
AIDP would read them and render the pickers using existing AIDP REST
endpoints (which are known to exist because the catalog browser pages
already call them).

**Path B — AIDP defines its own contract.**
Whatever schema AIDP prefers (json-schema annotations, OpenAPI
extensions, a separate metadata file), as long as it's documented and
the per-source list is at least: catalogs, schemas, volumes, tables,
KBs, KB jobs, workspaces, clusters, compartments, OCI GenAI models,
OCI buckets, OCI regions.

Either way, the developer should not need to leave the Test panel to
discover identifiers.

---

## Issue 3 — Resource principal not available in the Test panel; testing requires hardcoding a PEM key

### Current behavior

Custom tools that call AIDP REST endpoints (or any OCI service) need
to authenticate. The standard auth modes are:

- `resource_principal` — injected by AIDP cluster at runtime. Default.
- `user_principal` — needs tenancy_ocid / user_ocid / fingerprint / private_key_content in conf.
- `instance_principal` — OCI instance metadata.

A tool deployed to an agent flow runs on AIDP compute → resource_principal works automatically.

**A tool clicked in the Test panel does NOT have a resource principal.**
Every call goes out unsigned and AIDP returns `404 NotAuthorizedOrNotFound`.
There's no warning in the Test panel that this will happen; the
developer just sees 404s on the first invocation and assumes the
URL or volume key is wrong.

To make Test panel testing work today, the developer must:

1. Open `~/.oci/config`, find the `[DEFAULT]` profile.
2. Open the file at `key_file=` (the PEM private key).
3. Paste **the entire PEM** into the tool's `private_key_content`
   conf field.
4. Paste `tenancy`, `user`, `fingerprint` from `~/.oci/config` into
   the matching conf fields.
5. Set `auth_mode` to `user_principal`.

JR's reference tool takes this path — hardcoded directly in the
Python source. It works, but committing a PEM key to source / a zip
is a real security problem.

### Workarounds in place

A build-time credential injector (`python setup.py build --test-creds`)
reads `~/.oci/config`, finds the PEM, and patches the zip output ONLY
(source files on disk are never modified). The developer rebuilds with
`--test-creds`, uploads the credentialed zip for Test panel testing,
then rebuilds clean for production deploy.

This works but it's a workaround — the zip artifact becomes sensitive
and must be treated as a secret. The build emits a loud warning and a
`.gitignore` rule covers the credentialed zips.

### Ask

A **"Use AIDP Workbench Session" auth mode** for the Test panel.

When the developer clicks Test on a tool, they're already authenticated
to the AIDP Workbench (they're inside the Workbench UI). The Test
panel should use that session's identity to sign the tool's outgoing
requests automatically — no PEM, no conf editing, no `--test-creds`
flag.

Concretely:

1. Add an auth mode like `workbench_session` (or `inherit_session`, name
   TBD) that, when selected in conf, tells AIDP's tool runner to use
   the current session's principal.
2. Make it the **default** for Test panel invocations — even if the
   tool's conf says `resource_principal`, the Test panel transparently
   substitutes the session principal.
3. Optionally surface a banner in the Test panel: *"Using your workbench
   session for auth. Deployed flows will use the conf-declared
   auth_mode."*

This single change would eliminate ~80% of the Test panel friction.
JR's tool would work without any hardcoded credentials. The
`--test-creds` build flag could be deprecated.

---

## Issue 4 — `CustomToolEntry` Pydantic model rejects unknown top-level keys

### Current behavior

Symptom: agent calls any tool → `error.type=ValidationError`, `Output:
No data available`. The tool's `_execute_tool` never runs.

Root cause: AIDP's `CustomToolEntry` model declares exactly six
fields (`tool_class_name`, `display_name`, `description`, `version`,
`config`, `input_schema`). Strict Pydantic validation rejects unknown
top-level keys on each tool entry. The sidecar keys (`_uiHints`,
`_confDescriptions`) trigger ValidationError on tool load.

### Workarounds in place

Build-time sanitizer: `python setup.py build` reads `tool_config.json`
in-memory, strips `_uiHints` / `_confDescriptions` before writing to
the zip. Source files on disk keep them so the VS Code extension can
still consume the metadata. Per build output:

```
[stripped sidecars] aidp_catalog_toolkit.zip (...)
[stripped sidecars] compute_toolkit.zip (...)
... × 11
```

### Ask

Make `CustomToolEntry` ignore unknown top-level keys instead of
rejecting them. Pydantic v2 with `extra='ignore'` (the default in many
configs) would solve this. Then sidecar metadata becomes a forward-
compatible extension mechanism — third-party tooling, future AIDP UI
work, and anyone else can ship rich metadata that older runtimes
silently ignore.

This is a one-line config change in AIDP's deserializer.

---

## Summary of asks (prioritized)

| Priority | Ask | Effort | Impact |
|---|---|---|---|
| **P0** | Test panel: render `description` and use `default` as initial input value | Low | Removes ~90% of "what do I put here?" friction |
| **P0** | New `workbench_session` auth mode that auto-uses the developer's AIDP session in the Test panel | Medium | Eliminates need to embed PEM keys for testing |
| **P1** | `CustomToolEntry` ignores unknown top-level keys (Pydantic `extra='ignore'`) | Low | Unblocks any sidecar metadata pattern |
| **P1** | Test panel: render `examples` array as click-to-fill hints | Low | Reduces typos |
| **P1** | Test panel: render `enum` fields as `<select>` | Low | Prevents invalid values |
| **P2** | Native catalog-backed dropdowns in the Test panel (catalog, schema, volume, table, KB pickers) | Higher | Removes need to leave Test panel to discover identifiers. A contract (`_uiHints`) already exists in the curated repo for AIDP to adopt or replace |

---

## Reference material in the repo

- [UI_HINTS_SPEC.md](UI_HINTS_SPEC.md) — full sidecar contract (proposal for Path A in Issue 2).
- [CONFIG.md](CONFIG.md) — what auto-fills and where to find each value the developer has to fill.
- [aidp_io/aidp_io.py](aidp_io/aidp_io.py) — the consolidated file IO helper used by every tool; shows the exact AIDP REST endpoints depended on.
- `setup.py` — the wizard / build script. `--test-creds` is the secret-injection workaround.

## Reference data

- Curated tool count: 11 packages, 22 tools.
- Each tool's tool_config.json has been populated with: `description` (agent-directive, ~600 chars), per-field `description` with examples, JSON Schema `examples` arrays (151 total), JSON Schema `default` values (87 added). All invisible in the Test panel today.
- The `--test-creds` workaround has shipped to production tenants; it can be retired once the workbench_session auth mode lands.
