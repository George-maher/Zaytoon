"""Install the skills this repository ships, into the live skills root.

Explicit and offline by design: a skill's instructions are injected into the
system prompt, so shipping a skill is not the same as switching it on. Nothing
here enables anything unless ``--enable`` is passed, and nothing downloads.

    python scripts/install_shipped_skills.py --list
    python scripts/install_shipped_skills.py            # copy, disabled
    python scripts/install_shipped_skills.py --enable   # and switch them on
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Shipped skill sources, relative to the repository root.
SKILLS_SOURCE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "skills",
)


def available() -> list[str]:
    if not os.path.isdir(SKILLS_SOURCE):
        return []
    return sorted(
        name
        for name in os.listdir(SKILLS_SOURCE)
        if os.path.isfile(os.path.join(SKILLS_SOURCE, name, "skill.json"))
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="only show what is shippable")
    parser.add_argument("--only", default="", help="install one skill by name")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing copy")
    parser.add_argument(
        "--enable", action="store_true",
        help="switch installed skills on (they inject instructions into the model)",
    )
    args = parser.parse_args(argv)

    from core.skills import SkillExists, install, set_enabled, skills_root, sync_to_db
    from core.skills.manifest import ManifestError, load

    names = available()
    if args.only:
        names = [name for name in names if name == args.only.strip()]
        if not names:
            print(f"no shipped skill named {args.only!r}", file=sys.stderr)
            return 2

    if args.list:
        for name in names:
            try:
                manifest = load(os.path.join(SKILLS_SOURCE, name))
            except ManifestError as exc:
                print(f"  {name}: INVALID — {exc}")
                continue
            tools = os.path.join(SKILLS_SOURCE, name, "tools")
            modules = [f for f in os.listdir(tools) if f.endswith(".py")] if os.path.isdir(tools) else []
            # Counted by module, and labelled that way: one file can return two
            # tools from `build_tools()`, and a "--list" that understates what is
            # about to be registered is not worth trusting for anything else.
            print(
                f"  {name}: {manifest.description[:60]} "
                f"[{len(modules)} tool module(s), permissions: "
                f"{', '.join(manifest.permissions) or 'none'}]"
            )
        return 0

    import asyncio

    async def _run() -> int:
        import logging

        logging.getLogger("Zaytoon.Core").setLevel(logging.WARNING)
        root = skills_root()
        failures = 0
        installed: list[str] = []
        for name in names:
            source = os.path.join(SKILLS_SOURCE, name)
            try:
                manifest = install(source, root=root, overwrite=args.overwrite)
            except SkillExists:
                print(f"  {name}: already installed (use --overwrite to replace it)")
                continue
            except (ManifestError, OSError) as exc:
                print(f"  {name}: FAILED — {exc}", file=sys.stderr)
                failures += 1
                continue
            installed.append(manifest["name"])
            print(f"  {name}: installed, enabled={manifest.get('enabled')}")
        if not installed:
            return 1 if failures else 0
        await sync_to_db(root)
        if args.enable:
            for name in installed:
                ok = await set_enabled(name, True)
                print(f"  {name}: enabled={ok}")
        else:
            print(
                "\nInstalled but NOT enabled. Enable in Settings → Skills, or with:\n"
                f"    python -c \"import asyncio; from core.skills import set_enabled; "
                f"asyncio.run(set_enabled('{installed[0]}', True))\"\n"
                "An enabled skill changes what the model is told on every turn; "
                "enabling one is a decision, not an install detail."
            )
        return 1 if failures else 0

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
