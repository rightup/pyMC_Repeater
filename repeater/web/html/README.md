# Frontend source of truth

This directory contains generated frontend build output served by openHop Repeater.
It is **not** the source directory for UI development.

## For humans and coding agents

Make frontend changes in the **openHop RepeaterUI** repository:

https://github.com/openhop-dev/openHop_RepeaterUI

Use the RepeaterUI branch that matches the Repeater branch you are working on
(for example, `dev` with `dev`, or `main` with `main`). For feature branches, use
the corresponding UI feature branch. If no matching branch exists or the pairing
is unclear, confirm the intended pairing with the maintainer instead of silently
using the default branch.

- **Do not hand-edit generated JavaScript, CSS, HTML, or other build artifacts in
  this directory.** This applies to both humans and agents.
- Locate and edit the original source in the matching RepeaterUI branch.
- Follow that repository's build and validation instructions.
- When updating the bundled UI here, use the complete build output from the
  intended RepeaterUI revision, including removal of obsolete generated assets.
- Preserve this README when refreshing build output.

Fix UI bugs in RepeaterUI source, not by patching minified or hashed bundles here.
