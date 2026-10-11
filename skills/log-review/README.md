## log-review

Reads log files already on disk and reports authentication failures, exposed secrets, and crash signatures with file and line evidence. Read-only: no scanners, no network, nothing is executed.

Drop-in files in `tools/` are registered automatically when the skill is
enabled. Every tool here is read-only by construction: the shipped-skill test
(`backend/tests/test_shipped_skills.py`) fails if one imports `subprocess`,
`socket`, a `w`/`a` file mode, `shutil`, or anything else that could act on
the machine or the network.
