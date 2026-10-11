## file-audit

Hashes files, checks permissions and file-type structure (exec bits, world-writable, setuid, PEM certificates, symlinks leaving the workspace). Reads and computes only; it changes nothing.

Drop-in files in `tools/` are registered automatically when the skill is
enabled. Every tool here is read-only by construction: the shipped-skill test
(`backend/tests/test_shipped_skills.py`) fails if one imports `subprocess`,
`socket`, a `w`/`a` file mode, `shutil`, or anything else that could act on
the machine or the network.
