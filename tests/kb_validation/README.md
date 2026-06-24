# KB API Error Message Validation Harness

Tests AIDP's Knowledge Base REST API surface against the documented
error-message catalog (`AIDP Error Message Standardization Effort`,
Confluence, owner: Arun Rajan). For every documented (scenario → expected
message) row, fires the bad request and asserts the response matches.

## Scope

The catalog covers 15 KB methods × ~13 scenarios each = ~198 rows.
Each scenario is classified by **how testable it is from outside AIDP**:

| Class | Meaning | Harness behavior |
|---|---|---|
| `validation` | Client-induced bad input (null body, missing required params, malformed JSON). | **RUNS** — fires request, asserts status + message substring. |
| `fixture` | Needs setup/teardown on the tenancy (create-then-create for "already exists", get-after-delete for "not found"). | **RUNS WHEN** `--fixtures` flag is set; otherwise SKIPPED with reason. |
| `infra` | Requires AIDP-side state change (feature flag off, default cluster down, lake property bag empty). | **SKIPPED** — not reproducible from a client. Counted in coverage report. |
| `db_inject` | Requires injecting an Oracle DB error (`ORA-12170`, `ORA-40284`, etc.). | **SKIPPED** — needs DB-level fault injection. Counted. |
| `race` | Timing-dependent (checksum changed before run started). | **SKIPPED** — not reproducible deterministically. Counted. |
| `noop` | Scenario explicitly returns no customer error (best-effort callbacks, ignored DDL, silent failures). | **RUNS** in inverse mode — asserts NO error in response. |

Coverage report at end of every run shows:

```
KB METHOD                    validation  fixture  infra  db_inject  race  noop  TOTAL
CreateKnowledgeBase                   4        2      3          4     0     2     15
ListKnowledgeBases                    1        0      2          1     0     5      9
...
TESTABLE (validation + fixture + noop)         ___ / ~198 rows
SKIPPED  (infra + db_inject + race)            ___ / ~198 rows
```

## Files

```
tests/kb_validation/
├── README.md                    (this file)
├── harness.py                   The runner — pytest-style but standalone
├── catalog/
│   ├── _common.json             Shared param substitution + classifier rules
│   ├── create_knowledge_base.json
│   ├── list_knowledge_bases.json
│   ├── get_knowledge_base.json
│   ├── update_knowledge_base.json
│   ├── delete_knowledge_base.json
│   ├── create_kb_job.json
│   ├── list_kb_jobs.json
│   ├── get_kb_job.json
│   ├── delete_kb_job.json
│   ├── create_kb_job_run.json
│   ├── list_kb_job_runs.json
│   ├── get_kb_job_run.json
│   ├── update_kb_job_run.json
│   ├── update_kb_owner.json
│   └── get_vector_store_credentials.json
└── fixtures.py                  Optional setup/teardown for `fixture` rows
```

## Quick start

```
# from repo root, against the DEFAULT OCI profile
python tests/kb_validation/harness.py

# or against a session-token profile
python tests/kb_validation/harness.py --profile aidp-session

# run just one method, verbose
python tests/kb_validation/harness.py --method CreateKnowledgeBase -v

# run a specific scenario id
python tests/kb_validation/harness.py --id CKB-002

# enable fixtures (creates / deletes real KBs — uses your tenancy)
python tests/kb_validation/harness.py --fixtures \
  --catalog construction_catalog --schema construction_schema

# report-only — don't fire any requests, just print the coverage matrix
python tests/kb_validation/harness.py --report

# strict mode — exit non-zero on any FAIL (CI flag)
python tests/kb_validation/harness.py --strict
```

## What a scenario row looks like

```json
{
  "id": "CKB-002",
  "method": "CreateKnowledgeBase",
  "verb": "POST",
  "path_template": "/knowledgeBases",
  "scenario": "idlOcid, catalogId, schemaId, or displayName is missing or blank",
  "class": "validation",
  "variants": [
    { "name": "missing idlOcid", "body_override": {"idlOcid": ""} },
    { "name": "missing catalogId", "body_override": {"catalogId": ""} },
    { "name": "missing schemaId", "body_override": {"schemaId": ""} },
    { "name": "missing displayName", "body_override": {"displayName": ""} }
  ],
  "expected_status": [400, 422],
  "expected_message_substring": "Invalid request. The parameter",
  "expected_message_param_filled": true,
  "source": "Confluence: AIDP Error Message Standardization Effort > CreateKnowledgeBase"
}
```

The harness expands each variant into one fired request, asserts the
response status is in `expected_status` and the message contains
`expected_message_substring`. When `expected_message_param_filled` is true,
the harness also confirms the `<paramName>` placeholder was replaced (i.e.
the message contains the actual field name).

## How the catalog was built

Each row in the Confluence catalog was reviewed and assigned a class:

- "missing or blank" + parameter list → `validation`
- "Request body is `null`" → `validation` (sends literal null body)
- "...already exists" / "...is not found" → `fixture` (needs pre-staged state)
- "feature configuration cannot be read" / "feature is disabled" → `infra`
- "Default cluster is not active" → `infra`
- "ORA-XXXXX" / "TCPS connect timeout" / "JDBC signal" → `db_inject`
- "checksum changed before" / "race" / "before an inline job run could start" → `race`
- "No customer-visible error message" → `noop` (asserts request succeeds)
- "No KB-standardized message in this method" → `validation` with `expected_class: global_handler`
  (the harness checks SOME error came back, but doesn't pin the exact message
   since it comes from the framework, not the KB catalog)

## Coverage report sample

```
$ python tests/kb_validation/harness.py --report

KB METHOD                    validation  fixture  infra  db_inject  race  noop  TOTAL
CreateKnowledgeBase                   4        1      3          4     0     2     14
ListKnowledgeBases                    1        0      2          1     0     5      9
GetKnowledgeBase                      1        1      2          1     0     0      5
UpdateKnowledgeBase                  21        2      3          5     1     2     34
DeleteKnowledgeBase                   1        1      2          1     0     7     12
CreateKnowledgeBaseJob                1        2      2          1     0     1      7
ListKnowledgeBaseJobs                 3        1      2          1     0     2      9
GetKnowledgeBaseJob                   1        2      2          1     0     0      6
DeleteKnowledgeBaseJob                1        2      2          1     0     1      7
CreateKnowledgeBaseJobRun             1        2      2          1     1     0      7
ListKnowledgeBaseJobRun               1        2      2          1     0     7     13
GetKnowledgeBaseJobRun                0        3      2          1     0     2      8
UpdateKnowledgeBaseJobRun             1        3      2          7     0     2     15
UpdateKnowledgeBaseOwner              1        1      2          1     0     3      8
GetVectorStoreCredentials             5        1      2          1     0     0      9

TOTALS                               42       24     32         32     2    34    163
TESTABLE  (validation + fixture + noop):   100 / 163  (61%)
SKIPPED   (infra + db_inject + race):       66 / 163  (40%)
```

(Counts are approximate — actual numbers per the populated catalog.)

## CI integration

Add to `.github/workflows/kb_validation.yml`:

```yaml
- name: KB error message validation
  run: |
    python tests/kb_validation/harness.py --strict --no-fixtures
  env:
    AIDP_OCI_PROFILE: aidp-session
```

`--no-fixtures` keeps the run side-effect-free (no real KBs created).

## Adding new scenarios

When AIDP adds a new error path:

1. Add a row to the matching `catalog/<method>.json` file.
2. Pick a class (`validation` / `fixture` / `infra` / `db_inject` / `race` / `noop`).
3. Run `python harness.py --id <new-id>` to verify it fires correctly.
4. Commit.

When AIDP changes an existing message:

1. Update `expected_message_substring` in the row.
2. Re-run the affected method: `python harness.py --method <name>`.
3. Commit with a note: which AIDP commit changed the message.

## Tenancy requirements

The harness fires real signed requests against
`https://aidp.<region>.oci.oraclecloud.com/<apiVersion>/aiDataPlatforms/<dataLakeOcid>/knowledgeBases…`.
On startup it runs one preflight `GET /knowledgeBases` to confirm the surface is
reachable; if the response is `404 NotAuthorizedOrNotFound`, the harness exits
early with code 5 and a helpful message.

For the preflight (and any actual test runs) to succeed the target tenancy must:

1. Have the Knowledge Base feature enabled in the AIDP data lake.
2. Have the caller's IAM principal granted at least
   `inspect aidp-knowledge-bases` (read) — fixture runs additionally need
   `manage aidp-knowledge-bases`.

If either is missing, all 110 testable rows would return the same generic
`NotAuthorizedOrNotFound`, which would mask whether AIDP returns the documented
KB-specific messages. The preflight prevents that false negative.

You can bypass the preflight with `--skip-preflight` (useful for inspecting raw
responses or when you know what you're doing).

## Known gaps

- **No `db_inject` reproduction path.** Until AIDP exposes a test-mode flag to
  force an `ORA-40284` etc. response from a specific endpoint, these stay
  SKIPPED.
- **No `infra` reproduction path.** Same — need an AIDP-side toggle to
  disable the KB feature on a test tenancy, or to mark the default cluster
  unavailable.
- **Race conditions are best-effort.** The `race` class would need fast
  parallel requests + state inspection that AIDP doesn't currently expose.

These are tracked in `AIDP_FEEDBACK.md → Issue 8` (added by this commit).
