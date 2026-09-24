"""Device configuration file generator.

Serves a form that produces a valid CONFIG.TXT for a device in a given role,
prefilled with this installation's own connection settings, and imports an
existing file for editing.

The schema and the role templates under data/ are device-specific and are
vendored from the firmware repository that owns them. Everything else here is
generic: it knows about roles, keys, types and gates, and no key by name.
"""

__all__ = ["layout", "reader", "schema", "service"]
