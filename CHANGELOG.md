# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- Require `vcon>=0.10.0`, the first vcon-lib release that writes draft-ietf-vcon-vcon-core-04 attachment defaults and raw JSON bodies, and omits empty `meta`/`metadata`.

### Added
- Initial scaffold generated from [vcon-adapter-template](https://github.com/vcon-dev/vcon-adapter-template).
- Party `type` (`"bot"`/`"person"`) and `org` per draft-ietf-vcon-vcon-core-04 §4.2.11/§4.2.12: every agent party (primary model, Claude Code sub-agents, bridged tool/services) is now typed `"bot"`, with `org` set to the source-derived provider when known; the human user party is typed `"person"` only where the source makes that unambiguous (Claude Code, direct Anthropic/OpenAI Responses API traces), left untyped for OpenAI Agents SDK and OTel sources.
- `agent_trace` analysis entries now carry `attachment` (draft-ietf-vcon-vcon-core-04
  §4.5.3), linking each trace to the `agent_file_change`/`agent_environment`
  attachments it was derived from, when any apply. Indices are resolved from the
  attachments actually present in the vCon, computed before the `lawful_basis`
  attachment is appended, so a later-appended `lawful_basis` never shifts them.
