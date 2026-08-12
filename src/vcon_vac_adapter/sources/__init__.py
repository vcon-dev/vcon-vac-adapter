"""Platform-specific session parsers.

Each module exposes `parse(input) -> Session` for one platform, normalizing
native record formats into the IR. The CLI / daemon front-ends dispatch on
`config.source.platform` to the right module.
"""
