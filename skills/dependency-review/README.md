## dependency-review

Parses lockfiles and manifests in the workspace and reports supply-chain smell: unpinned versions, plaintext registries, direct git sources and install-script hooks. Never contacts a registry, never runs a package manager.

Drop-in files in `tools/` are registered automatically when the skill is
enabled. Every tool here is read-only by construction: the shipped-skill test
(`backend/tests/test_shipped_skills.py`) fails if one imports `subprocess`,
`socket`, a `w`/`a` file mode, `shutil`, or anything else that could act on
the machine or the network.
