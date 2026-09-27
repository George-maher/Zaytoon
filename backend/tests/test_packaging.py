"""Packaging sanity checks.

These exist because a malformed ``requirements.txt`` does not fail locally: the
virtualenv already has every package installed from an earlier file, so the
problem only surfaces in a clean environment — i.e. a Docker build.

Concretely, a Python-style ``\"\"\"docstring\"\"\"`` header was once committed at
the top of ``requirements.txt``. pip parsed the first line as a requirement and
the backend image failed to build. Docker is a first-class run path, so the
files it depends on are validated here instead.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
REQUIREMENTS = BACKEND / "requirements.txt"
REQUIREMENTS_DEV = BACKEND / "requirements-dev.txt"

#: Markers that mean "this is not a requirements file at all".
FORBIDDEN_PREFIXES = ('"""', "'''")


def requirement_lines(path: Path):
    """Yield ``(line_no, text)`` for every non-comment, non-option entry."""
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        yield i, line


class TestRequirementsFilesParse:
    @pytest.mark.parametrize("path", [REQUIREMENTS, REQUIREMENTS_DEV])
    def test_file_exists_and_is_not_empty(self, path):
        assert path.exists(), f"{path.name} is missing"
        assert path.read_text(encoding="utf-8").strip(), f"{path.name} is empty"

    @pytest.mark.parametrize("path", [REQUIREMENTS, REQUIREMENTS_DEV])
    def test_no_python_docstring_headers(self, path):
        """A triple-quoted block is parsed as a requirement and breaks the build."""
        for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = raw.strip()
            for bad in FORBIDDEN_PREFIXES:
                assert not stripped.startswith(bad), (
                    f"{path.name}:{i} starts with {bad!r} — pip will try to "
                    f"install it as a package. Use '#' comments instead."
                )

    @pytest.mark.parametrize("path", [REQUIREMENTS, REQUIREMENTS_DEV])
    def test_every_entry_is_a_valid_requirement(self, path):
        from packaging.requirements import InvalidRequirement, Requirement

        for line_no, line in requirement_lines(path):
            try:
                Requirement(line)
            except InvalidRequirement as e:
                pytest.fail(f"{path.name}:{line_no} invalid requirement {line!r}: {e}")

    @pytest.mark.parametrize("path", [REQUIREMENTS, REQUIREMENTS_DEV])
    def test_no_local_paths_or_urls(self, path):
        """Editable/local installs break the container build."""
        for line_no, line in requirement_lines(path):
            assert not line.startswith(("-e", "--editable", "file:", ".")), (
                f"{path.name}:{line_no} uses a local path: {line!r}"
            )
            assert "://" not in line or line.startswith("git+"), (
                f"{path.name}:{line_no} pins a remote URL: {line!r}"
            )

    def test_no_duplicate_packages(self):
        names = []
        for line_no, line in requirement_lines(REQUIREMENTS):
            names.append(re.split(r"[<>=!~\[; ]", line, 1)[0].lower())
        dupes = {n for n in names if names.count(n) > 1}
        assert not dupes, f"duplicate entries in requirements.txt: {dupes}"


class TestRuntimeDependenciesPresent:
    """The app must not be missing a runtime import that only Docker exercises."""

    REQUIRED = {
        "fastapi": "REST + WebSocket framework",
        "uvicorn": "ASGI server",
        "httpx": "OpenRouter client",
        "websockets": "WebSocket transport for /ws",
        "faster-whisper": "speech-to-text",
        "edge-tts": "text-to-speech",
        "aiosqlite": "SQLite via MemoryAgent",
        "python-dotenv": "config loading from .env",
        "ddgs": "web search tool",
    }

    def names(self):
        return {
            re.split(r"[<>=!~\[; ]", line, 1)[0].lower()
            for _n, line in requirement_lines(REQUIREMENTS)
        }

    @pytest.mark.parametrize("package", sorted(REQUIRED))
    def test_declared(self, package):
        assert package in self.names(), (
            f"{package} ({REQUIRED[package]}) is not in requirements.txt"
        )

    def test_dev_requirements_include_the_runtime_file(self):
        content = REQUIREMENTS_DEV.read_text(encoding="utf-8")
        assert "-r requirements.txt" in content

    def test_pytest_is_dev_only(self):
        """pytest must not ship in the production image."""
        assert "pytest" not in self.names(), (
            "pytest belongs in requirements-dev.txt, not requirements.txt"
        )


class TestPipCanResolveRequirements:
    @pytest.mark.slow
    def test_pip_dry_run_succeeds(self):
        """The exact resolution the Dockerfile performs, without installing.

        Skipped automatically when the machine has no network, since pip needs
        to reach the index to resolve.
        """
        result = subprocess.run(
            [
                sys.executable, "-m", "pip", "install",
                "--dry-run", "--quiet", "--disable-pip-version-check",
                "-r", str(REQUIREMENTS),
            ],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0 and "connection" in result.stderr.lower():
            pytest.skip("no network access for pip resolution")
        assert result.returncode == 0, (
            f"pip could not resolve requirements.txt:\n{result.stderr[-2000:]}"
        )


class TestDockerAssetsPresent:
    def test_dockerfile_and_compose_exist(self):
        repo = BACKEND.parent
        assert (repo / "docker-compose.yml").exists()
        assert (BACKEND / "Dockerfile").exists()
        assert (repo / "frontend" / "Dockerfile").exists()

    def test_dockerignore_excludes_venv_and_env(self):
        """Copying a 500 MB venv or baking the API key into the image is wrong."""
        patterns = (BACKEND / ".dockerignore").read_text(encoding="utf-8")
        for needed in (".venv", ".env", "__pycache__"):
            assert needed in patterns, f".dockerignore must exclude {needed}"

    def test_compose_passes_env_file_and_publishes_the_api(self):
        compose = (BACKEND.parent / "docker-compose.yml").read_text(encoding="utf-8")
        assert "env_file" in compose, "backend/.env must be injected via env_file"
        assert "8000:8000" in compose
        assert "BACKEND_HOST=0.0.0.0" in compose, (
            "the container must bind 0.0.0.0, not the 127.0.0.1 default"
        )

    def test_compose_has_no_local_model_service(self):
        """Local model execution was removed; no such service may reappear."""
        compose = (BACKEND.parent / "docker-compose.yml").read_text(encoding="utf-8").lower()
        assert "ollama" not in compose
