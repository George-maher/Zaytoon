"""Verify the shell's real geometry with a *mapped* window.

    cd desktop && ..\backend\.venv\Scripts\python scripts/measure_panel.py

Why mapped: customTkinter propagates size through a <Configure> event, so a
withdrawn window reports the 300x300 library defaults and every measurement is
meaningless. This maps the window (off-screen, so it never interrupts anyone),
lets the geometry settle, and prints the numbers that matter.

What it pins: a header bar of pills must be a bar, not a 300px band. The
customTkinter default for an unsized frame is 300x300, and one leaked into the
header, which is why the row of pills used to float in the middle of an empty
band with ~150px of dead space above and below it.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import customtkinter as ctk  # noqa: E402

from app import panel as panel_mod  # noqa: E402

#: A pill row is ~22px. Anything approaching 300 means a CTkFrame default leaked
#: back in, which is the bug this script exists to catch.
HEADER_MAX_SANE = 60


def main() -> int:
    ctk.set_appearance_mode("dark")
    root = ctk.CTk()
    root.geometry("820x620-3000-3000")  # off-screen: visible to Tk, not to you
    p = panel_mod.ChatPanel(root, width=820, height=620)
    root.update()
    p.update()
    for _ in range(5):
        root.update_idletasks()
        root.update()

    print(f"panel      actual={p.winfo_width()}x{p.winfo_height()} "
          f"natural={p.winfo_reqwidth()}x{p.winfo_reqheight()}")
    print(f"header     height={p.header.winfo_height()} "
          f"natural_height={p.header.winfo_reqheight()}")
    print(f"body       height={p._body.winfo_height()}")
    print(f"transcript height={p.transcript.winfo_height()}")
    print(f"side       height={p.side.winfo_height()}")
    print(f"core       {p.core.winfo_width()}x{p.core.winfo_height()}")

    problems = []
    if p.header.winfo_height() > HEADER_MAX_SANE:
        problems.append(
            f"header is {p.header.winfo_height()}px tall — a customTkinter "
            f"default leaked back in"
        )
    # A shell that cannot fit its configured size will be clipped on a laptop
    # screen, which is the whole point of the clamp in _position_panel().
    if p.winfo_reqheight() > 620:
        problems.append(
            f"natural height {p.winfo_reqheight()} exceeds the 620 it is given, "
            f"so the window cannot shrink to its configured size"
        )

    root.destroy()
    if problems:
        print("\nPROBLEMS:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nGEOMETRY OK — header is a bar, and the shell fits 820x620")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())