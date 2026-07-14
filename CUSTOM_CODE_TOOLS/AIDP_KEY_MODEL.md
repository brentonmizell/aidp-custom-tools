# AIDP resource key model

How catalogs, schemas, tables, volumes, and knowledge bases are identified in
the AIDP data-plane REST API. Confirmed from live `GET /catalogs`,
`/schemas`, `/volumes`, `/tables`, `/knowledgeBases` responses (Jul 2026,
`construction_catalog` demo lake) plus the console's own network calls.

## The one trap

**The console "Details → Key" for a catalog shows the `catalogGuid` (hex),
but the API's `key` field — and every `catalogKey` filter — is the catalog
NAME.** Passing the hex GUID as `catalogKey` returns empty / 404, or 500
`"checking the sourceType of Catalog"`. Use the name.

```json
// GET /catalogs  → each item:
{ "key": "construction_catalog",              // <-- use THIS as catalogKey
  "catalogGuid": "b8650413b08946f6a7fd003b542fe188",  // hex, shown as "Key" in console Details
  "catalogType": "INTERNAL", "displayName": "construction_catalog" }
```

## Key + filter reference

| Resource | Resource `key` (the item's own key) | Filter params to LIST it |
|---|---|---|
| Catalog | **name** — `construction_catalog` | — (top level; `GET /catalogs`) |
| Schema | dotted — `construction_catalog.construction_schema` | `catalogKey=construction_catalog` |
| Table | dotted — `construction_catalog.construction_schema.silver_retail_sales_enriched` | `catalogKey=construction_catalog&schemaKey=construction_catalog.construction_schema` |
| Volume | dotted — `construction_catalog.construction_schema.construction_documents` | `catalogKey=construction_catalog&schemaKey=construction_catalog.construction_schema` |
| KB | **hex** — `33076ca9d9fd49388562aefa48715caa` | `catalogKey=construction_catalog&schemaKey=construction_catalog.construction_schema` |

The consistent rule: **each filter param takes the PARENT resource's own
`key`.** `catalogKey` = the catalog's key (which is its name);
`schemaKey` = the schema's key (the DOTTED `catalog.schema`). Using the
plain schema name as `schemaKey` returns HTTP 400/404 — confirmed by an
`op=map` walk where every plain-name sub-call failed. (Responses *echo*
`schemaKey` as the plain name, but the request must send the dotted key.)

Notes:
- **Resource keys** double as the direct-by-key GET path: schemas/tables/
  volumes use the dotted `catalog.schema[.name]` path; KBs use a hex GUID.
- A **volume file path** references the volume by its dotted key:
  `GET /volumes/{catalog.schema.volume}/files?path=/`.

## Endpoints (data-lake scoped)

```
GET /catalogs
GET /catalogs/{catalogName}
GET /schemas?catalogKey={catalogName}
GET /tables?catalogKey={catalogName}&schemaKey={catalogName.schemaName}
GET /volumes?catalogKey={catalogName}&schemaKey={catalogName.schemaName}
GET /volumes/{volumeKey}/files?path={path}         # volumeKey = catalog.schema.volume
GET /knowledgeBases?catalogKey={catalogName}&schemaKey={catalogName.schemaName}
GET /knowledgeBases/{kbKey}                          # kbKey = hex GUID
```

Base:
`https://aidp.<region>.oci.oraclecloud.com/<apiVersion>/<scope>/<dataLakeOcid>/…`

Two surfaces on the same lake — and they are NOT interchangeable per
endpoint:
- `20260430/aiDataPlatforms` — serves catalogs, schemas, tables, volumes,
  volume files. whoami / list_catalogs / list_files all work here.
- `20240831/dataLakes` — used by the AIDP web console. **`/knowledgeBases`
  is served ONLY here** — it 404s on `20260430/aiDataPlatforms` regardless
  of schemaKey format. Catalogs/schemas/tables/volumes also work here.

So `credential_store_auth_sample` and `map` hit the newer surface for
everything except KBs, and fall back to `20240831/dataLakes` for
`/knowledgeBases` and `/knowledgeBases/{kbKey}`.

Observed KB quirk: the KB list also accepts the PLAIN schema name as
`schemaKey` (the response echoes it that way); `map` tries plain first then
dotted on the dataLakes surface.

If a filtered list still misbehaves, the usual cause is a wrong `catalogKey`
(hex GUID instead of the name) — not the surface.

## Why the wired tools already do this right

`aidp_io` builds `catalogKey` from `cat['key']` taken from the `/catalogs`
response — which is the **name** — so `aidp_catalog_toolkit`, the RAG tools,
and anything else going through `aidp_io` use the correct identifier
automatically. Hand-entering the hex GUID from the console is the only way to
hit the trap; when you discover keys via the API (list → copy `key`), you get
the right value.
