# Contributing

## Spec Compliance Checklist

**Every PR that touches vCon construction MUST tick these boxes.** Based on [vcon-speckit](https://github.com/vcon-dev/vcon-speckit) non-negotiables.

**Always use the `vcon` library helpers** (`Vcon.add_party`, `add_dialog`, `add_attachment`, `add_analysis`, `add_tag`) rather than writing to `vcon_dict[...]` directly. The lib (>= 0.9.4) is spec-correct out of the box.

### Top-level vCon
- [ ] `vcon` syntax param is exactly `"0.4.0"` (NOT `0.0.1`, NOT `0.0.2`)
- [ ] `uuid` is a v4 UUID string
- [ ] All timestamps are ISO-8601 with timezone (`Z` or explicit offset)
- [ ] No `group: []` or `redacted: {}` left over from `Vcon.build_new()`
- [ ] `subject` written via `v.vcon_dict["subject"]` (no setter exists on the lib)

### Analysis objects
- [ ] Use `Vcon.add_analysis(type, dialog, vendor, body, encoding, schema, product, ...)` — the lib produces spec-correct output and enforces `vendor` as a required kwarg
- [ ] Field name is `schema`, NEVER `schema_version` (the lib's kwarg is already `schema`)
- [ ] `body` is always a string. If JSON, paired with `encoding="json"`
- [ ] WTF transcripts go in `analysis[]` (NOT `attachments[]`)
- [ ] Transcript analysis has `schema=<WTF draft URL>`, `encoding="json"`, `vendor="<provider>"`, `product="<model>"`

### Attachment objects
- [ ] Use `Vcon.add_attachment(purpose, body, encoding, party, dialog, ...)` — the lib produces spec-correct output as of v0.9.2
- [ ] Field name is `purpose`, NEVER `type` (the lib's keyword arg is already `purpose`, so this happens automatically)
- [ ] Always pass `party` and `dialog` (use `0, 0` for vCon-level attachments)
- [ ] For JSON-bodied attachments: `encoding="json"` with a JSON-string `body` — the lib validates it parses

### Tags attachment
- [ ] Use `Vcon.add_tag(name, value)` directly. The lib (>=0.9.3) writes `party`/`dialog` correctly.

### External media
- [ ] `url` AND `content_hash` both present
- [ ] `content_hash` formatted as `sha512-<base64url-of-digest>` (NOT hex)

### Extension naming traps
- [ ] Use `amended` (NOT `appended` — that's a legacy vcon-mcp column name)
- [ ] Use `critical` (NOT `must_support` — also legacy)
- [ ] Any extension used is listed in top-level `extensions[]`

### Lawful basis (if recording consent is tracked)
- [ ] `lawful_basis` attachment uses `type: "lawful_basis"` (extension-defined exception — see speckit)
- [ ] `"lawful_basis"` added to top-level `extensions[]`
- [ ] For synthetic test data: use `lawful_basis: "legitimate_interests"` + `proof_mechanism: external_system`

### Synthetic test data
- [ ] Parties marked with `validation: "synthetic"`
- [ ] `purpose: "synthetic_data_consent"` attachment present

## Dev Workflow

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
pytest
ruff check .
mypy src/
```

## Releasing

1. Update `CHANGELOG.md` with a new version section
2. Bump version in `pyproject.toml`
3. Tag: `git tag vX.Y.Z && git push --tags`
4. CI publishes to PyPI on tag push (if configured)
