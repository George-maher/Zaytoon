"""Project scaffolding and package-manager tasks.

``create_project`` is the interesting one: it is the multi-step flow from the
product spec ("Create a React project called AI Dashboard") reduced to a single
tool call. It does not shell out to an interactive generator — those prompt for
input the assistant cannot answer, and they fail in a non-TTY subprocess. It
writes the files directly, which is deterministic, inspectable, and produces
something that actually builds.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from typing import Any, Dict, List

from tools.base import BaseTool, ToolResult, ToolValidationError, _prop
from tools.sandbox import resolve_workspace

logger = logging.getLogger("Zaytoon.Tools.Coding")

MAX_OUTPUT_CHARS = 20_000
TIMEOUT_S = 300  # npm install is legitimately slow

#: A directory name has to survive being a path segment on every platform.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")

#: Reserved on Windows, unusable as a directory name.
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

#: Supported templates, mapped to a slug used in the project name.
TEMPLATES = {
    "node": "node",
    "javascript": "node",
    "python": "python",
    "react": "react",
    "typescript": "react",
    "html": "html",
    "static": "html",
    "empty": "empty",
}


class CreateProject(BaseTool):
    permission = "confirm"

    def name(self) -> str:
        return "create_project"

    def aliases(self) -> List[str]:
        return ["scaffold", "new_project", "init_project"]

    def description(self) -> str:
        return (
            "Create a new project directory from a template. Templates: "
            "react, node, python, html, empty. Writes real files; does not "
            "run an interactive generator. Requires confirmation."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": _prop("string", "Project name, e.g. 'AI Dashboard'."),
                "template": _prop(
                    "string",
                    f"One of: {', '.join(sorted(TEMPLATES))}.",
                    default="empty",
                ),
                "path": _prop(
                    "string", "Parent directory inside the workspace.", default="."
                ),
            },
            "required": ["name"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ToolValidationError("'name' is required")
        _check_project_name(name)
        template = params.get("template", "empty")
        if template not in TEMPLATES:
            raise ToolValidationError(
                f"Unknown template {template!r}. Available: {', '.join(sorted(TEMPLATES))}"
            )

    def risk_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"path": params.get("path", ".")}

    async def execute(
        self, name: str = "", template: str = "empty", path: str = ".", **_: Any
    ) -> ToolResult:
        name = _check_project_name(name)
        slug = _slugify(name)
        parent = resolve_workspace(path or ".")
        if not parent:
            return ToolResult.denied(f"'{path}' is outside the workspace")
        target = os.path.join(parent, slug)

        if await _in_executor(os.path.exists, target):
            return ToolResult.fail(
                f"{slug} already exists. Choose another name or delete it first."
            )

        builder = _BUILDERS[TEMPLATES.get(template, "empty")]
        try:
            written = await _in_executor(builder, target, name)
        except OSError as e:
            return ToolResult.fail(f"Could not create project: {e}")

        listing = "\n".join(f"  {p}" for p in written)
        return ToolResult.ok(
            f"Created {template} project '{name}' at {slug}/ ({len(written)} files)\n"
            f"{listing}\n\nNext: cd {slug}"
            + (" && npm install" if TEMPLATES.get(template) in ("react", "node") else ""),
            {"name": name, "slug": slug, "path": slug, "template": template, "files": written},
        )


def _check_project_name(name: str) -> str:
    cleaned = str(name).strip()
    if not cleaned:
        raise ToolValidationError("'name' is required")
    if not _SAFE_NAME.match(cleaned):
        raise ToolValidationError(
            f"Project name may only contain letters, digits, spaces, dots, "
            f"dashes and underscores, and must start with a letter or digit: {name!r}"
        )
    if cleaned.lower() in _RESERVED:
        raise ToolValidationError(f"{name!r} is a reserved device name on Windows")
    return cleaned


def _slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-._").lower()
    return slug or "project"


async def _in_executor(fn, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fn, *args)


def _write(path: str, content: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def _build_empty(target: str, name: str) -> List[str]:
    _write(os.path.join(target, "README.md"), f"# {name}\n\nCreated by Zaytoon.\n")
    _write(os.path.join(target, ".gitignore"), "node_modules/\n__pycache__/\n*.log\n.env\n")
    return ["README.md", ".gitignore"]


def _build_html(target: str, name: str) -> List[str]:
    _write(
        os.path.join(target, "index.html"),
        f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{name}</title>
    <link rel="stylesheet" href="style.css" />
  </head>
  <body>
    <main>
      <h1>{name}</h1>
      <p>Edit index.html to get started.</p>
    </main>
    <script src="main.js"></script>
  </body>
</html>
""",
    )
    _write(
        os.path.join(target, "style.css"),
        ":root { color-scheme: dark; }\n"
        "body { font-family: system-ui, sans-serif; margin: 0; "
        "display: grid; place-items: center; min-height: 100vh; "
        "background: #0a0a0f; color: #e5e5e5; }\n"
        "main { text-align: center; }\n",
    )
    _write(
        os.path.join(target, "main.js"),
        'console.log("' + name.replace('"', '\\"') + ' loaded");\n',
    )
    _write(os.path.join(target, "README.md"), f"# {name}\n\nOpen `index.html`.\n")
    return ["index.html", "style.css", "main.js", "README.md"]


def _build_node(target: str, name: str) -> List[str]:
    slug = os.path.basename(target)
    _write(
        os.path.join(target, "package.json"),
        '{\n  "name": "%s",\n  "version": "0.1.0",\n  "private": true,\n'
        '  "type": "commonjs",\n  "main": "index.js",\n'
        '  "scripts": {\n    "start": "node index.js",\n    "test": "node --test"\n  }\n}\n'
        % slug,
    )
    _write(
        os.path.join(target, "index.js"),
        f'console.log("{name} running");\n\n'
        "module.exports = { name: %r };\n" % name,
    )
    _write(os.path.join(target, ".gitignore"), "node_modules/\n.env\n*.log\n")
    _write(os.path.join(target, "README.md"), f"# {name}\n\n```\nnpm install\nnpm start\n```\n")
    return ["package.json", "index.js", ".gitignore", "README.md"]


def _build_python(target: str, name: str) -> List[str]:
    module = os.path.basename(target).replace("-", "_")
    _write(os.path.join(target, f"{module}.py"), f'"""{name}."""\n\n\ndef main() -> None:\n    print("{name}")\n\n\nif __name__ == "__main__":\n    main()\n')
    _write(
        os.path.join(target, "pyproject.toml"),
        '[project]\nname = "%s"\nversion = "0.1.0"\n'
        'requires-python = ">=3.9"\ndependencies = []\n' % module.replace("_", "-"),
    )
    _write(os.path.join(target, "README.md"), f"# {name}\n\n```\npython {module}.py\n```\n")
    _write(os.path.join(target, ".gitignore"), "__pycache__/\n*.py[cod]\n.venv/\n.env\n")
    return [f"{module}.py", "pyproject.toml", "README.md", ".gitignore"]


def _build_react(target: str, name: str) -> List[str]:
    slug = os.path.basename(target)
    _write(
        os.path.join(target, "package.json"),
        '{\n  "name": "%s",\n  "version": "0.1.0",\n  "private": true,\n'
        '  "type": "module",\n  "scripts": {\n'
        '    "dev": "vite",\n    "build": "vite build",\n'
        '    "preview": "vite preview"\n  },\n'
        '  "dependencies": {\n    "react": "^18.2.0",\n'
        '    "react-dom": "^18.2.0"\n  },\n'
        '  "devDependencies": {\n    "@vitejs/plugin-react": "^4.2.1",\n'
        '    "vite": "^5.0.10"\n  }\n}\n' % slug,
    )
    _write(
        os.path.join(target, "index.html"),
        f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>{name}</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
  </body>
</html>
""",
    )
    _write(os.path.join(target, "vite.config.js"), "import { defineConfig } from 'vite'\nimport react from '@vitejs/plugin-react'\n\nexport default defineConfig({\n  plugins: [react()],\n})\n")
    _write(
        os.path.join(target, "src", "main.jsx"),
        'import React from "react";\n'
        'import { createRoot } from "react-dom/client";\n'
        'import App from "./App.jsx";\n'
        'import "./index.css";\n\n'
        'createRoot(document.getElementById("root")).render(\n'
        "  <React.StrictMode>\n    <App />\n  </React.StrictMode>,\n);\n",
    )
    _write(
        os.path.join(target, "src", "App.jsx"),
        "export default function App() {\n"
        "  return (\n"
        '    <main className="app">\n'
        f"      <h1>{name}</h1>\n"
        "      <p>Edit src/App.jsx to get started.</p>\n"
        "    </main>\n"
        "  );\n"
        "}\n",
    )
    _write(
        os.path.join(target, "src", "index.css"),
        ":root { color-scheme: dark; font-family: system-ui, sans-serif; }\n"
        "body { margin: 0; background: #0a0a0f; color: #e5e5e5; }\n"
        ".app { min-height: 100vh; display: grid; place-items: center; }\n",
    )
    _write(os.path.join(target, ".gitignore"), "node_modules/\ndist/\n.env\n*.log\n")
    _write(os.path.join(target, "README.md"), f"# {name}\n\n```\nnpm install\nnpm run dev\n```\n")
    return [
        "package.json", "index.html", "vite.config.js",
        "src/main.jsx", "src/App.jsx", "src/index.css", ".gitignore", "README.md",
    ]


_BUILDERS = {
    "empty": _build_empty,
    "html": _build_html,
    "node": _build_node,
    "python": _build_python,
    "react": _build_react,
}


class RunNpm(BaseTool):
    """Run an npm script. Confirmation-required: it executes project code."""

    permission = "confirm"

    def name(self) -> str:
        return "run_npm"

    def aliases(self) -> List[str]:
        return ["npm_install", "npm", "install_package", "run_node_command"]

    def description(self) -> str:
        return (
            "Run an npm script in a project inside the workspace, e.g. "
            "'install' or 'run dev'. Requires confirmation."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "script": _prop("string", "npm script name, e.g. install, run dev, test."),
                "path": _prop("string", "Project directory.", default="."),
            },
            "required": ["script"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        script = params.get("script")
        if not isinstance(script, str) or not script.strip():
            raise ToolValidationError("'script' is required")
        if not re.fullmatch(r"[A-Za-z0-9:._ -]{1,80}", script):
            raise ToolValidationError(f"Unsupported npm script: {script!r}")

    def risk_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"path": params.get("path", ".")}

    async def execute(self, script: str = "", path: str = ".", **_: Any) -> ToolResult:
        npm = shutil.which("npm")
        if npm is None:
            return ToolResult.fail("npm is not installed or not on PATH.")
        root = resolve_workspace(path or ".")
        if not root:
            return ToolResult.denied(f"'{path}' is outside the workspace")
        if not await _in_executor(os.path.isfile, os.path.join(root, "package.json")):
            return ToolResult.fail(f"No package.json in {path or '.'}.")

        argv = [npm, "run", script.strip()]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,  # noqa: S603 - fixed argv, npm resolved via which()
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=root,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_S)
        except asyncio.TimeoutError:
            return ToolResult.fail(f"npm run {script} timed out after {TIMEOUT_S}s")
        except OSError as e:
            return ToolResult.fail(f"Could not run npm: {e}")

        stdout = out.decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARS]
        stderr = err.decode("utf-8", errors="replace")[:4000]
        if proc.returncode == 0:
            return ToolResult.ok(stdout.strip() or f"npm run {script} completed.")
        return ToolResult(
            success=False, output=stdout, error=stderr.strip() or f"exit {proc.returncode}"
        )


class RunTests(BaseTool):
    """Detect and run a project's test suite."""

    permission = "confirm"

    def name(self) -> str:
        return "run_tests"

    def aliases(self) -> List[str]:
        return ["run_test", "test", "run_build", "run_linter"]

    def description(self) -> str:
        return (
            "Run a project's test suite, detected from its manifest "
            "(npm test, pytest, or go test). Requires confirmation."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": _prop("string", "Project directory.", default="."),
                "framework": _prop(
                    "string", "Force a runner: npm, pytest or go.", default="auto"
                ),
            },
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        framework = params.get("framework", "auto")
        if framework not in ("auto", "npm", "pytest", "go"):
            raise ToolValidationError(
                f"Unknown framework {framework!r}. Use auto, npm, pytest or go."
            )

    def risk_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"path": params.get("path", ".")}

    async def execute(
        self, path: str = ".", framework: str = "auto", **_: Any
    ) -> ToolResult:
        root = resolve_workspace(path or ".")
        if not root:
            return ToolResult.denied(f"'{path}' is outside the workspace")

        chosen = framework
        if chosen == "auto":
            if await _in_executor(os.path.isfile, os.path.join(root, "package.json")):
                chosen = "npm"
            elif await _in_executor(
                os.path.isfile, os.path.join(root, "pyproject.toml")
            ) or await _in_executor(os.path.isfile, os.path.join(root, "pytest.ini")):
                chosen = "pytest"
            elif await _in_executor(os.path.isfile, os.path.join(root, "go.mod")):
                chosen = "go"
            else:
                return ToolResult.fail(
                    f"No recognisable test setup in {path or '.'}. "
                    "Expected package.json, pyproject.toml/pytest.ini or go.mod."
                )

        runner = {"npm": "npm", "pytest": "python", "go": "go"}[chosen]
        argv = {
            "npm": [runner, "test"],
            "pytest": [runner, "-m", "pytest", "-q"],
            "go": [runner, "test", "./..."],
        }[chosen]
        if shutil.which(runner) is None:
            return ToolResult.fail(f"'{runner}' is not installed or not on PATH.")

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,  # noqa: S603 - fixed argv
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=root,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_S)
        except asyncio.TimeoutError:
            return ToolResult.fail(f"Tests timed out after {TIMEOUT_S}s")
        except OSError as e:
            return ToolResult.fail(f"Could not run tests: {e}")

        stdout = out.decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARS]
        stderr = err.decode("utf-8", errors="replace")[:4000]
        if proc.returncode == 0:
            return ToolResult.ok(
                stdout.strip() or "All tests passed.", {"framework": chosen}
            )
        return ToolResult(
            success=False, output=stdout,
            error=stderr.strip() or stdout.strip() or f"exit {proc.returncode}",
            data={"framework": chosen, "exit_code": proc.returncode},
        )


def build_tools() -> List[BaseTool]:
    return [CreateProject(), RunNpm(), RunTests()]
