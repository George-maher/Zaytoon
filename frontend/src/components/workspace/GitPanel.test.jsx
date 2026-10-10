/**
 * The source-control panel.
 *
 * The behaviour worth testing is **what it does with each failure**, because
 * the panel has three genuinely different failures and conflating them is the
 * bug: a 404 is "not a repository here" (hide, not an error), a 403 is a
 * containment decision (no retry), and a 409 is git's own refusal (show its
 * words). A panel that renders all three as a red banner trains people to
 * ignore red.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import GitPanel from "./GitPanel";
import * as api from "../../lib/api";

function respond(overrides = {}) {
  return {
    getGitStatus: vi.fn(async () => ({ clean: true, branch: "main", entries: [] })),
    getGitLog: vi.fn(async () => ({ commits: [] })),
    getGitDiff: vi.fn(async () => ({ diff: "", empty: true })),
    getGitBranches: vi.fn(async () => ({
      branches: [{ name: "main", sha: "abc", current: true, remote: false }],
      current: "main",
    })),
    gitAdd: vi.fn(async () => ({})),
    gitCommit: vi.fn(async () => ({})),
    gitCheckout: vi.fn(async () => ({})),
    gitFetch: vi.fn(async () => ({})),
    gitPull: vi.fn(async () => ({})),
    gitPush: vi.fn(async () => ({})),
    gitRestore: vi.fn(async () => ({})),
    gitMerge: vi.fn(async () => ({})),
    ...overrides,
  };
}

/** Stub every API function the panel imports, so a missing one is explicit. */
function mockApi(stubs) {
  vi.spyOn(api, "getGitStatus").mockImplementation(stubs.getGitStatus);
  vi.spyOn(api, "getGitLog").mockImplementation(stubs.getGitLog);
  vi.spyOn(api, "getGitDiff").mockImplementation(stubs.getGitDiff);
  vi.spyOn(api, "getGitBranches").mockImplementation(stubs.getGitBranches);
  vi.spyOn(api, "gitAdd").mockImplementation(stubs.gitAdd);
  vi.spyOn(api, "gitCommit").mockImplementation(stubs.gitCommit);
  vi.spyOn(api, "gitCheckout").mockImplementation(stubs.gitCheckout);
  vi.spyOn(api, "gitFetch").mockImplementation(stubs.gitFetch);
  vi.spyOn(api, "gitPull").mockImplementation(stubs.gitPull);
  vi.spyOn(api, "gitPush").mockImplementation(stubs.gitPush);
  vi.spyOn(api, "gitRestore").mockImplementation(stubs.gitRestore);
  vi.spyOn(api, "gitMerge").mockImplementation(stubs.gitMerge);
}

describe("GitPanel", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("shows the current branch", async () => {
    mockApi(respond({ getGitStatus: vi.fn(async () => ({ clean: true, branch: "trunk", entries: [] })) }));
    render(<GitPanel path="." />);
    expect(await screen.findByText("trunk")).toBeInTheDocument();
  });

  it("reports a clean tree", async () => {
    mockApi(respond());
    render(<GitPanel path="." />);
    expect(await screen.findByText(/no changes/i)).toBeInTheDocument();
  });

  it("hides itself when the folder is not a repository", async () => {
    // 404 is the answer to "is there a panel to show", not a failure to report.
    // The stub carries the status code because `getGitStatus` now attaches one:
    // without it the panel cannot tell this apart from the failures below, and
    // that conflation was the bug.
    mockApi(respond({
      getGitStatus: vi.fn(async () => ({ error: "not a git repository", status: 404 })),
    }));
    render(<GitPanel path="." />);
    expect(await screen.findByText(/not a git repository/i)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("keeps the panel and says why when the failure is not a 404", async () => {
    // Every status used to collapse into `present = false`, so a containment
    // refusal hid the panel behind "This folder is not a git repository" —
    // a confident statement about the folder that had nothing to do with it.
    mockApi(respond({
      getGitStatus: vi.fn(async () => ({ error: "outside the workspace", status: 403 })),
    }));
    render(<GitPanel path="." />);
    expect(await screen.findByRole("alert")).toHaveTextContent("outside the workspace");
    expect(screen.queryByText(/not a git repository/i)).not.toBeInTheDocument();
    // A containment decision cannot be retried — the boundary does not move
    // because the user asked again.
    expect(screen.queryByRole("button", { name: /retry/i })).not.toBeInTheDocument();
  });

  it("offers a retry when re-reading could plausibly succeed", async () => {
    mockApi(respond({
      getGitStatus: vi.fn(async () => ({ error: "backend restarting", status: 503 })),
    }));
    render(<GitPanel path="." />);
    fireEvent.click(await screen.findByRole("button", { name: /retry/i }));
    expect(api.getGitStatus).toHaveBeenCalledTimes(2);
  });

  it("stops saying 'Reading…' forever when the fetch itself throws", async () => {
    // No status code exists to classify, so the panel has nothing but the
    // message — and leaving it on the loading line would be the one state
    // with no way out at all.
    mockApi(respond({
      getGitStatus: vi.fn(async () => {
        throw new Error("network down");
      }),
    }));
    render(<GitPanel path="." />);
    expect(await screen.findByRole("alert")).toHaveTextContent("network down");
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  it("renders each changed entry with its status", async () => {
    mockApi(respond({
      getGitStatus: vi.fn(async () => ({
        clean: false,
        branch: "main",
        entries: [
          { path: "src/app.py", status: "modified", staged: false, unstaged: true },
          { path: "notes.md", status: "untracked", staged: false, unstaged: false },
        ],
      })),
    }));
    render(<GitPanel path="." />);
    expect(await screen.findByText("src/app.py")).toBeInTheDocument();
    expect(screen.getByText("notes.md")).toBeInTheDocument();
    expect(screen.getByText("modified")).toBeInTheDocument();
    expect(screen.getByText("untracked")).toBeInTheDocument();
  });

  it("stages one file and refreshes from the response", async () => {
    // The response carries the refreshed status, so the panel never shows a
    // tree it knows to be stale — which is the one thing a source-control
    // panel must never do.
    mockApi(respond({
      getGitStatus: vi.fn(async () => ({
        clean: false,
        branch: "main",
        entries: [{ path: "a.py", status: "modified", staged: false, unstaged: true }],
      })),
      gitAdd: vi.fn(async () => ({
        status: { clean: true, branch: "main", entries: [] },
      })),
    }));
    render(<GitPanel path="." />);
    // Exact match: "Stage all" in the action row also matches a loose
    // /stage/i, and the point of this test is the *row* button.
    fireEvent.click(await screen.findByRole("button", { name: /^stage$/i }));
    await waitFor(() => expect(screen.getByText(/no changes/i)).toBeInTheDocument());
    expect(api.gitAdd).toHaveBeenCalledWith(["a.py"], { path: "." });
  });

  it("shows a refused commit with git's own message", async () => {
    mockApi(respond({
      getGitStatus: vi.fn(async () => ({
        clean: false, branch: "main", staged: 1,
        entries: [{ path: "a.py", status: "modified", staged: true, unstaged: false }],
      })),
      gitCommit: vi.fn(async () => ({ error: "nothing to commit, working tree clean" })),
    }));
    render(<GitPanel path="." />);
    fireEvent.change(await screen.findByLabelText(/commit message/i), {
      target: { value: "wip" },
    });
    fireEvent.click(screen.getByRole("button", { name: /commit/i }));
    // Verbatim, because git's refusals are specific in a way a paraphrase
    // would not be.
    expect(await screen.findByText(/nothing to commit/i)).toBeInTheDocument();
  });

  it("keeps the typed message after a failed commit", async () => {
    // A lost half-typed message is the most annoying possible bug here, and it
    // is what happens when the input and the error share one piece of state.
    mockApi(respond({
      getGitStatus: vi.fn(async () => ({
        clean: false, branch: "main", staged: 1,
        entries: [{ path: "a.py", status: "modified", staged: true, unstaged: false }],
      })),
      gitCommit: vi.fn(async () => ({ error: "refused" })),
    }));
    render(<GitPanel path="." />);
    const input = await screen.findByLabelText(/commit message/i);
    fireEvent.change(input, { target: { value: "my message" } });
    fireEvent.click(screen.getByRole("button", { name: /commit/i }));
    await screen.findByText(/refused/i);
    expect(screen.getByLabelText(/commit message/i)).toHaveValue("my message");
  });

  it("cannot commit with nothing staged", async () => {
    mockApi(respond({
      getGitStatus: vi.fn(async () => ({
        clean: false, branch: "main", staged: 0,
        entries: [{ path: "a.py", status: "modified", staged: false, unstaged: true }],
      })),
    }));
    render(<GitPanel path="." />);
    expect(await screen.findByRole("button", { name: /commit/i })).toBeDisabled();
  });

  it("cannot commit an empty message", async () => {
    mockApi(respond({
      getGitStatus: vi.fn(async () => ({
        clean: false, branch: "main", staged: 1,
        entries: [{ path: "a.py", status: "modified", staged: true, unstaged: false }],
      })),
    }));
    render(<GitPanel path="." />);
    expect(await screen.findByRole("button", { name: /commit/i })).toBeDisabled();
  });

  it("renders Arabic labels when the language is ar", async () => {
    mockApi(respond());
    render(<GitPanel path="." language="ar" />);
    expect(await screen.findByText("لا تغييرات")).toBeInTheDocument();
    expect(screen.getByLabelText("المصدر")).toBeInTheDocument();
  });

  it("offers a branch switcher without remote-tracking branches", async () => {
    mockApi(respond({
      getGitBranches: vi.fn(async () => ({
        branches: [
          { name: "main", sha: "a", current: true, remote: false },
          { name: "dev", sha: "b", current: false, remote: false },
          { name: "origin/main", sha: "c", current: false, remote: true },
        ],
        current: "main",
      })),
    }));
    render(<GitPanel path="." />);
    const select = await screen.findByRole("combobox", { name: /branch/i });
    // Checking out `origin/main` lands on a detached HEAD — a trap for someone
    // expecting a branch switch — so remote refs are not offered.
    expect(screen.queryByRole("option", { name: /origin\/main/ })).not.toBeInTheDocument();
    expect(screen.getByRole("option", { name: /dev/ })).toBeInTheDocument();
    fireEvent.change(select, { target: { value: "dev" } });
    await waitFor(() => expect(api.gitCheckout).toHaveBeenCalledWith("dev", { path: "." }));
  });

  it("pulls fast-forward first and offers a merge when it is refused", async () => {
    mockApi(respond({
      getGitStatus: vi.fn(async () => ({
        clean: false, branch: "main",
        entries: [{ path: "a.py", status: "modified", staged: false, unstaged: true }],
      })),
      gitPull: vi.fn(async () => ({ error: "fatal: Not possible to fast-forward, aborting." })),
    }));
    render(<GitPanel path="." />);
    fireEvent.click(await screen.findByRole("button", { name: /^pull$/i }));
    // git's own words, verbatim — and the path out of the refusal.
    expect(await screen.findByText(/fast-forward/i)).toBeInTheDocument();
    expect(api.gitPull).toHaveBeenCalledWith({ ffOnly: true, path: "." });
    fireEvent.click(screen.getByRole("button", { name: /retry as merge/i }));
    await waitFor(() => expect(api.gitPull).toHaveBeenCalledWith({ ffOnly: false, path: "." }));
  });

  it("does not offer a merge for a pull that never had an upstream", async () => {
    // "no tracking information" is a different problem: merging would not
    // help, so offering the button would just produce a second failure.
    mockApi(respond({
      gitPull: vi.fn(async () => ({ error: "There is no tracking information for the current branch." })),
    }));
    render(<GitPanel path="." />);
    fireEvent.click(await screen.findByRole("button", { name: /^pull$/i }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /retry as merge/i })).not.toBeInTheDocument();
  });

  it("fetches, pushes and stages all through the action row", async () => {
    mockApi(respond({
      getGitStatus: vi.fn(async () => ({
        clean: false, branch: "main", staged: 1,
        entries: [{ path: "a.py", status: "modified", staged: true, unstaged: false }],
      })),
    }));
    render(<GitPanel path="." />);
    fireEvent.click(await screen.findByRole("button", { name: /^fetch$/i }));
    await waitFor(() => expect(api.gitFetch).toHaveBeenCalledWith({ path: "." }));
    fireEvent.click(screen.getByRole("button", { name: /^push$/i }));
    await waitFor(() => expect(api.gitPush).toHaveBeenCalledWith({ path: "." }));
    fireEvent.click(screen.getByRole("button", { name: /stage all/i }));
    await waitFor(() => expect(api.gitAdd).toHaveBeenCalledWith([], { all: true, path: "." }));
  });

  it("cannot stage all on a clean tree", async () => {
    // Staging nothing is always wrong; letting someone press it only to be
    // told so wastes the interaction.
    mockApi(respond());
    render(<GitPanel path="." />);
    expect(await screen.findByRole("button", { name: /stage all/i })).toBeDisabled();
  });

  it("unstages a staged file through git restore", async () => {
    mockApi(respond({
      getGitStatus: vi.fn(async () => ({
        clean: false, branch: "main", staged: 1,
        entries: [{ path: "a.py", status: "modified", staged: true, unstaged: false }],
      })),
      gitRestore: vi.fn(async () => ({ status: { clean: true, branch: "main", entries: [] } })),
    }));
    render(<GitPanel path="." />);
    fireEvent.click(await screen.findByRole("button", { name: /unstage/i }));
    await waitFor(() =>
      expect(api.gitRestore).toHaveBeenCalledWith(["a.py"], { staged: true, path: "." })
    );
  });

  it("requires a second click to discard", async () => {
    mockApi(respond({
      getGitStatus: vi.fn(async () => ({
        clean: false, branch: "main",
        entries: [{ path: "a.py", status: "modified", staged: false, unstaged: true }],
      })),
    }));
    render(<GitPanel path="." />);
    fireEvent.click(await screen.findByRole("button", { name: /discard a\.py/i }));
    // The first click arms. Nothing has been destroyed yet.
    expect(api.gitRestore).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /confirm discard a\.py/i }));
    await waitFor(() =>
      expect(api.gitRestore).toHaveBeenCalledWith(["a.py"], { staged: false, path: "." })
    );
  });

  it("offers no discard for an untracked file", async () => {
    // `git restore` cannot remove an untracked file — offering the button and
    // failing is worse than not offering it.
    mockApi(respond({
      getGitStatus: vi.fn(async () => ({
        clean: false, branch: "main",
        entries: [{ path: "fresh.txt", status: "untracked", staged: false, unstaged: false }],
      })),
    }));
    render(<GitPanel path="." />);
    expect(await screen.findByRole("button", { name: /^stage$/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /discard/i })).not.toBeInTheDocument();
  });

  it("offers both stage and unstage for a partially staged file", async () => {
    // `MM`: staged *and* worktree changes. The porcelain columns are honest
    // now, so both actions belong on the row.
    mockApi(respond({
      getGitStatus: vi.fn(async () => ({
        clean: false, branch: "main", staged: 1,
        entries: [{ path: "a.py", status: "both", staged: true, unstaged: true }],
      })),
    }));
    render(<GitPanel path="." />);
    expect(await screen.findByRole("button", { name: /^stage$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /unstage/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /discard a\.py/i })).toBeInTheDocument();
  });

  it("shows a file's diff in the pane, narrowed to that file", async () => {
    mockApi(respond({
      getGitStatus: vi.fn(async () => ({
        clean: false, branch: "main",
        entries: [{ path: "a.py", status: "modified", staged: false, unstaged: true }],
      })),
      getGitDiff: vi.fn(async () => ({ diff: "-old\n+new", empty: false })),
    }));
    render(<GitPanel path="." />);
    fireEvent.click(await screen.findByRole("button", { name: /^diff$/i }));
    expect(await screen.findByText(/-old/)).toBeInTheDocument();
    expect(api.getGitDiff).toHaveBeenCalledWith(".", { staged: false, file: "a.py" });
    fireEvent.click(screen.getByRole("button", { name: /close/i }));
    expect(screen.queryByText(/-old/)).not.toBeInTheDocument();
  });

  it("says so when a file has no textual diff yet", async () => {
    // An empty pane reads as "no changes" — the honest answer for an
    // untracked file is that git has nothing to compare it against.
    mockApi(respond({
      getGitStatus: vi.fn(async () => ({
        clean: false, branch: "main",
        entries: [{ path: "fresh.txt", status: "untracked", staged: false, unstaged: false }],
      })),
      getGitDiff: vi.fn(async () => ({ diff: "", empty: true })),
    }));
    render(<GitPanel path="." />);
    fireEvent.click(await screen.findByRole("button", { name: /^diff$/i }));
    expect(await screen.findByText(/not be tracked yet/i)).toBeInTheDocument();
  });

  // -- merge -------------------------------------------------------------
  const twoBranches = {
    branches: [
      { name: "main", sha: "abc", current: true, remote: false },
      { name: "dev", sha: "def", current: false, remote: false },
      // Never a merge target from here: folding origin/main in would offer
      // a ref that leaves HEAD detached.
      { name: "origin/main", sha: "fff", current: false, remote: true },
    ],
    current: "main",
  };

  it("merges another local branch only after the arm click", async () => {
    mockApi(respond({
      getGitBranches: vi.fn(async () => twoBranches),
      gitMerge: vi.fn(async () => ({
        status: { clean: true, branch: "main", entries: [] },
      })),
    }));
    render(<GitPanel path="." />);

    // Arm: the row appears, the branch is chosen, nothing has been sent.
    fireEvent.click(await screen.findByRole("button", { name: /^merge$/i }));
    expect(api.gitMerge).not.toHaveBeenCalled();
    const select = await screen.findByLabelText("Branch to merge");
    expect(select).toHaveValue("dev");
    expect(select.querySelectorAll("option")).toHaveLength(1); // no remotes

    // Send on the second click.
    fireEvent.click(screen.getByRole("button", { name: "MERGE" }));
    await waitFor(() =>
      expect(api.gitMerge).toHaveBeenCalledWith("dev", { path: "." }),
    );
    await waitFor(() =>
      expect(screen.queryByLabelText("Branch to merge")).not.toBeInTheDocument(),
    );
  });

  it("has nothing to merge when there is only one branch", async () => {
    mockApi(respond()); // just main
    render(<GitPanel path="." />);

    const merge = await screen.findByRole("button", { name: /^merge$/i });
    expect(merge).toBeDisabled();
    fireEvent.click(merge);
    expect(screen.queryByLabelText("Branch to merge")).not.toBeInTheDocument();
  });

  it("closes the merge row without sending anything", async () => {
    mockApi(respond({ getGitBranches: vi.fn(async () => twoBranches) }));
    render(<GitPanel path="." />);

    fireEvent.click(await screen.findByRole("button", { name: /^merge$/i }));
    await screen.findByLabelText("Branch to merge");
    fireEvent.click(screen.getByRole("button", { name: "CANCEL" }));

    expect(screen.queryByLabelText("Branch to merge")).not.toBeInTheDocument();
    expect(api.gitMerge).not.toHaveBeenCalled();
  });

  it("shows git's own words when the merge is refused", async () => {
    mockApi(respond({
      getGitBranches: vi.fn(async () => twoBranches),
      gitMerge: vi.fn(async () => ({
        error: "CONFLICT (content): Merge conflict in a.py",
      })),
    }));
    render(<GitPanel path="." />);

    fireEvent.click(await screen.findByRole("button", { name: /^merge$/i }));
    fireEvent.click(await screen.findByRole("button", { name: "MERGE" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "CONFLICT (content): Merge conflict in a.py",
    );
  });

  it("says an empty history is empty rather than showing nothing", async () => {
    mockApi(respond());
    render(<GitPanel path="." />);
    expect(
      await screen.findByText(/no commits yet on this branch/i),
    ).toBeInTheDocument();
  });

  it("keeps 'loading history' on screen until the history arrives", async () => {
    let resolveLog;
    mockApi(respond({
      getGitLog: vi.fn(() => new Promise((resolve) => { resolveLog = resolve; })),
    }));
    render(<GitPanel path="." />);

    expect(await screen.findByText(/loading history/i)).toBeInTheDocument();
    await waitFor(() => expect(api.getGitLog).toHaveBeenCalled());
    expect(screen.queryByText(/no commits yet/i)).not.toBeInTheDocument();

    resolveLog({ commits: [{ sha: "abc1234", short: "abc1234", subject: "init" }] });
    await waitFor(() => expect(screen.getByText("init")).toBeInTheDocument());
    expect(screen.queryByText(/loading history/i)).not.toBeInTheDocument();
  });

  it("separates a failed history read from an empty repository", async () => {
    // A bare `[]` used to mean all three at once, so a repository with a long
    // history whose log request failed rendered as a brand-new one — a claim
    // the panel had no basis for.
    mockApi(respond({
      getGitLog: vi.fn(async () => ({ error: "git log failed: bad object" })),
    }));
    render(<GitPanel path="." />);

    expect(await screen.findByText(/git log failed: bad object/)).toBeInTheDocument();
    expect(screen.queryByText(/no commits yet/i)).not.toBeInTheDocument();
  });
});