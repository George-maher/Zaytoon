import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import ChatMessage from "./ChatMessage";

/**
 * The routing badge is a claim about which model answered *this* answer.
 *
 * Two failure modes matter more than the layout, and both are tested here:
 *
 *  - **A badge nobody made.** The model comes from the backend's own
 *    `model.completed` event, never from the configured default. An answer
 *    with no observed model must render no badge at all — a placeholder would
 *    be the frontend inventing provenance.
 *  - **A badge in the wrong place.** Inside an Arabic bubble the identifier
 *    has to stay `dir="ltr"` (and keep its bidi isolation), or `qwen/qwen3-30b`
 *    renders reordered against the Arabic text around it — the exact
 *    mixed-language case the design rules call out.
 */

const assistant = (extra = {}) => ({
  id: 1,
  role: "assistant",
  content: "The build is green.",
  streaming: false,
  ts: "10:04",
  ...extra,
});

describe("ChatMessage routing badge", () => {
  it("names the provider and model the backend reported", () => {
    render(
      <ChatMessage
        language="en"
        msg={assistant({ model: "qwen/qwen3-30b-a3b:free", provider: "openrouter", latencyMs: 812, attempt: 2 })}
      />,
    );

    const badge = screen.getByText("qwen/qwen3-30b-a3b:free");
    expect(badge.getAttribute("dir")).toBe("ltr");
    // The detail a user needs when the answer looks wrong is in the tooltip:
    // attempt number and latency are what distinguish "it answered" from "it
    // fell back three times and this is the one that stuck".
    expect(badge.getAttribute("title")).toContain("openrouter");
    expect(badge.getAttribute("title")).toContain("attempt 2");
    expect(badge.getAttribute("title")).toContain("812 ms");
  });

  it("renders no badge when no model was observed", () => {
    render(<ChatMessage language="en" msg={assistant()} />);

    expect(screen.getByText("The build is green.")).toBeTruthy();
    // The default model is not a report about this answer, so it must not
    // appear. `modelName` is deliberately not rendered here.
    expect(document.querySelector('[dir="ltr"].font-mono')).toBeNull();
  });

  it("never badges a user message", () => {
    render(
      <ChatMessage
        language="en"
        msg={{ id: 2, role: "user", content: "hi", model: "qwen/qwen3-30b-a3b:free", provider: "openrouter" }}
      />,
    );

    expect(screen.queryByText("qwen/qwen3-30b-a3b:free")).toBeNull();
  });

  it("keeps an Arabic answer's badge isolated from the surrounding RTL text", () => {
    render(
      <ChatMessage
        language="ar"
        msg={assistant({ content: "البناء ناجح.", model: "arcee-ai/trinity-mini:free", provider: "openrouter", attempt: 1 })}
      />,
    );

    const badge = screen.getByText("arcee-ai/trinity-mini:free");
    expect(badge.getAttribute("dir")).toBe("ltr");
    // The Arabic label and the identifier do not run together: the title is
    // the Arabic word for provider, not the English one.
    expect(badge.getAttribute("title")).toContain("المزود");
  });

  it("falls back to the provider alone when the model is unnamed", () => {
    render(<ChatMessage language="en" msg={assistant({ provider: "openrouter" })} />);

    expect(screen.getByText("openrouter")).toBeTruthy();
  });
});
