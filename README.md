# Custom Tools for AIDP Agent Flows

A curated set of Custom Code tools for AIDP (Oracle AI Data Platform) Agent
Flows, plus a template and a per-tool testing kit.

## What's here

- **INDEX.md** — the catalog: every package, its tools, dependencies, and where
  each maps on the roadmap. Start here.
- **CUSTOM_CODE_TEMPLATE/** — a ready-to-edit starting point for building your
  own tool. Copy it, change four things, run build.sh, upload.
- **CUSTOM_CODE_TOOLS/** — one folder per tool package. Each folder has:
    - `README.md` — what the package's tools do.
    - `<package>.zip` — the actual uploadable code.
    - `TESTING_INSTRUCTIONS.md` — what to upload, configure, and enter in the
      AgentFlow Test panel, with expected results.
    - `mock_files/` — sample data to paste or upload for the tests.

## How to use a tool

1. Open the tool's folder under CUSTOM_CODE_TOOLS/.
2. Upload its `<package>.zip` in the workspace file browser.
3. Add a Custom Tool node, set the Tool Class Name, and mirror the Display
   Name / Description / Input Schema / Config from the package's tool_config.json.
4. Follow `TESTING_INSTRUCTIONS.md` to test, using the files in `mock_files/`.
5. Deploy when it works.

## Build your own

See CUSTOM_CODE_TEMPLATE/docs/README.md for the 60-second quickstart. The
template encodes the AIDP conventions and the common gotchas (config unwrap,
type coercion, the error shape, the model-facing description).

## Notes

- Live-service tools (genai, web, email, object storage, AIDP catalog) need
  their backend reachable; their testing sheets list the prerequisites.
- Test pods have no network. Tools with pip dependencies install at deploy;
  bundle wheels for offline test pods.
