# Testing — genai_toolkit

Upload **genai_toolkit.zip** in the workspace file browser, add a Custom Tool node, set the Tool Class Name, then use the Test panel with the values below. Mirror the Display Name / Description / Input Schema from the package's `tool_config.json`.

> Prerequisite: Live OCI GenAI. Set compartment_id; RP needs GenAI access.

## RubricScorerTool
LLM-as-judge: score text against a rubric.

Config to set:
- compartment_id = <your compartment OCID>

**Test 1: Score the answer**

| Field | Value |
|-------|-------|
| `candidate` | `(paste scorer_candidate.txt)` |
| `rubric` | `(paste scorer_rubric.txt)` |

Expected: JSON verdict: overall_score, criteria, rationale, pass.

## SummarizerTool
Summarize long text (map-reduce).

Config to set:
- compartment_id = <your compartment OCID>

**Test 1: Summarize**

| Field | Value |
|-------|-------|
| `text` | `(paste long_document.txt)` |
| `instruction` | `one short paragraph` |

Expected: a summary string.

## Mock files (in this folder's mock_files/)
- `scorer_candidate.txt`
- `scorer_rubric.txt`
- `long_document.txt`

---
Pass = the documented keys come back; any failure returns `{"error": ...}`. If the agent later says tool use is "forbidden", start a fresh chat session.