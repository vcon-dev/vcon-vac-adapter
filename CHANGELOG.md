# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial scaffold generated from [vcon-adapter-template](https://github.com/vcon-dev/vcon-adapter-template).
- Party `type` (`"bot"`/`"person"`) and `org` per draft-ietf-vcon-vcon-core-04 §4.2.11/§4.2.12: every agent party (primary model, Claude Code sub-agents, bridged tool/services) is now typed `"bot"`, with `org` set to the source-derived provider when known; the human user party is typed `"person"` only where the source makes that unambiguous (Claude Code, direct Anthropic/OpenAI Responses API traces), left untyped for OpenAI Agents SDK and OTel sources.
