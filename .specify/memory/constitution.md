<!--
Sync Impact Report
- Version change: (template, unversioned) -> 1.0.0
- Modified principles: none (initial ratification; all principles newly authored)
- Added sections: Core Principles I-V, Compatibility & Legal Constraints, Contribution Workflow, Governance
- Removed sections: none
- Deferred items:
  - TODO(CI): CI is scheduled for Phase 0 of the first spec; until then pytest is run locally.
  - TODO(TESTS): no tests exist yet; Principle IV assumes pytest suites are added from Phase 0.
-->
# smart-panel Constitution

## Core Principles

### I. Evidence-Based Protocol Fidelity

Every statement about the panel's wire protocol MUST be traceable to a USB capture or a recorded
hardware test. `handoff/panel_protocol_handoff.md` is the source of truth for protocol facts, and
any change to `panel_driver/protocol.py` or `panel_driver/device.py` MUST update it in the same
pull request. Unknown or unverified behavior (for example header bytes 12-13) MUST be labeled as
such in code comments and docs, never presented as settled.

Rationale: the protocol is reverse-engineered. Guesses that harden into "facts" are the main way
this project could mislead the next contributor or damage hardware.

### II. Hardware-Safe by Default

The library MUST NOT leave a panel in a worse state than it found it. Specifically:

- The init sequence (CRTDIS then CRTLIG) MUST be sent before the first frame of any session.
- Values written to the device MUST be validated at the library boundary (for example brightness
  0-100, frame size within the uint16 length field).
- Every `dev.write` MUST account for the hidapi report-ID quirk (`b"\x00"` prefix, 1025 bytes).
- Experimental commands, or any byte sequence not seen in a vendor capture, MUST be opt-in,
  clearly marked, and MUST NOT run from default code paths.

Rationale: a malformed stream has already been observed to hang the panel's USB controller until
it is power-cycled, and the firmware is not recoverable by users.

### III. Layered and Minimal

`panel_driver` is a transport library: protocol framing, device I/O, orientation, and the frame
loop. Rendering (stats, clocks, animations) MUST live in a separate layer that depends on the
transport, never the reverse. The transport's runtime dependencies MUST stay minimal (currently
`hid` and `Pillow`); heavier dependencies such as `psutil` belong to the rendering layer. New
abstractions MUST be justified by a concrete second use, not anticipated ones.

Rationale: contributors will want to build many different displays on one stable transport, and
small, dependency-light code is easier to review and port.

### IV. Testable Without Hardware

Pure logic (packet building, header encoding, rotation, size limits) MUST have automated tests
written with pytest that run with no device attached, and bug fixes to that logic MUST include a
regression test. Tests that need the physical panel MUST be clearly separated (for example behind
a pytest marker) and skipped by default so anyone can run the suite. Continuous integration is
set up in Phase 0; until it exists, contributors MUST run `pytest` locally before merging.
Changes to the hardware-facing path MUST be accompanied by a note in the pull
request saying what hardware, OS, and duration were used to verify them.

Rationale: most contributors will not own the panel; the suite must still protect the protocol
code, and hardware verification must be reported honestly rather than assumed.

### V. Cross-Platform and Reproducible

macOS and Linux (including Raspberry Pi OS) are first-class targets. Platform-specific behavior
MUST be isolated in `device.py` (or an equivalent single module) and documented, with Linux setup
steps such as udev rules kept in the README. Supported Python versions MUST match
`requires-python` in `pyproject.toml`, and the project MUST be installable and runnable from a
fresh checkout using documented commands.

Rationale: the quirks that already exist (report ID handling, permissions) show that "works on my
machine" is the default failure mode for this kind of driver.

## Compatibility & Legal Constraints

- The project is licensed under the MIT License (`LICENSE`). Contributions MUST be original work
  or under a license compatible with MIT, and are accepted under MIT. Vendor software, firmware,
  and installers MUST NOT be committed or redistributed.
- USB captures committed to the repository MUST be filtered to the panel's own traffic.
  Unrelated devices on the capture host (webcams, fingerprint readers, Bluetooth adapters)
  MUST be removed, and captures MUST be reviewed for personal data before being committed.
- Support for additional devices or firmware variants MUST be added alongside, not in place of,
  the verified HOTSPOTEK `0x5548:0x1011` behavior, with its own captured evidence per Principle I.
- Reverse engineering MUST be limited to hardware the contributor owns, for interoperability.

## Contribution Workflow

- Non-trivial features start as a Spec Kit specification (`/speckit-specify`), followed by plan
  and tasks. Small fixes and documentation changes MAY skip this and go straight to a pull
  request.
- All changes land through pull requests to `main`; direct pushes are reserved for maintainers
  fixing a broken `main`.
- A pull request MUST describe what changed and why, state how it was tested (automated tests
  and, where relevant, hardware as described in Principle IV), and link any protocol evidence.
- At least one maintainer review is required before merge; while the project has a single
  maintainer, that maintainer may review and merge their own pull requests. Reviewers MUST check
  the change against this constitution; deviations MUST be justified in the pull request
  description.
- All participants MUST follow the Code of Conduct (`CODE_OF_CONDUCT.md`).
- Documentation MUST be updated with the behavior it describes, in the same pull request.

## Governance

This constitution supersedes other project practices where they conflict. Amendments are made by
pull request that edits this file, states the rationale, and receives approval from at least one
maintainer. Changes that alter or remove a principle require a migration note for affected
in-flight work.

Versioning follows semantic versioning: MAJOR for removing or redefining a principle in a
backward-incompatible way, MINOR for adding a principle or materially expanding guidance, PATCH
for clarifications and wording. Compliance is reviewed at pull request time (see Contribution
Workflow) and revisited whenever the supported hardware, platforms, or dependencies change.
Runtime guidance for AI coding agents lives in `CLAUDE.md`.

**Version**: 1.0.0 | **Ratified**: 2026-10-06 | **Last Amended**: 2026-10-06
