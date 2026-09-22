"""GTM Call List Sidecar -- an isolated, flag-gated call-list builder.

It reads the core's approved company x campaign snapshot and the production
contact/suppression registries, and never writes to them. Its own state lives in
a separate store (SQLite by default). Nothing in ``tgtc_core`` imports this
package, so with the sidecar off the core is byte-identical.

Kill switch: ``TGTC_CALL_LIST_SIDECAR`` -- anything but ``1`` means off (default).
"""
