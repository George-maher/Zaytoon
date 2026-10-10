import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import AIFloatingBar from "./AIFloatingBar";

/**
 * The bar exists so that "you can talk to Zaytoon from anywhere" is true of
 * the screens that have no prompt of their own. What is worth pinning is that
 * it is the *same* composer — one accessible name, one microphone (the
 * product has exactly one), and the shared status line rather than a private
 * copy of the turn's progress.
 */
const voice = { onVoiceStart: vi.fn(), onVoiceStop: vi.fn() };

describe("AIFloatingBar", () => {
  it("gives a screen the product's composer, with its one accessible name", () => {
    render(<AIFloatingBar language="en" isConnected onSend={vi.fn()} {...voice} />);
    expect(screen.getByLabelText(/your message/i)).toBeInTheDocument();
    expect(screen.getAllByLabelText(/activate voice mode/i)).toHaveLength(1);
  });

  it("sends through the same send path InputBar uses", () => {
    const onSend = vi.fn();
    render(<AIFloatingBar language="en" isConnected onSend={onSend} {...voice} />);
    const field = screen.getByLabelText(/your message/i);
    fireEvent.change(field, { target: { value: "what time is it" } });
    fireEvent.submit(field.closest("form"));
    expect(onSend).toHaveBeenCalledWith("what time is it");
    expect(field.value).toBe("");
  });

  it("announces turn progress through the shared status line", () => {
    render(
      <AIFloatingBar language="en" isConnected statusText="Calling get_time…" onSend={vi.fn()} {...voice} />,
    );
    const line = screen.getByRole("status");
    expect(line).toHaveTextContent("Calling get_time…");
    // The sentence carries the meaning; the LED beside it is decorative.
    expect(line).toHaveTextContent("STATUS");
  });

  it("shows no live region at all when there is no status to report", () => {
    // An empty aria-live region is a region that will announce whatever
    // changes inside it next — which on a quiet screen is nothing, and on a
    // busy one is unrelated to the status the region was created for.
    render(<AIFloatingBar language="en" isConnected onSend={vi.fn()} {...voice} />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("labels the composer in Arabic", () => {
    render(<AIFloatingBar language="ar" isConnected onSend={vi.fn()} {...voice} />);
    expect(screen.getByLabelText("رسالتك")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "إرسال" })).toBeInTheDocument();
  });

  it("leaves SEND unavailable while disconnected", () => {
    render(<AIFloatingBar language="en" isConnected={false} onSend={vi.fn()} {...voice} />);
    expect(screen.getByRole("button", { name: /send/i })).toBeDisabled();
  });
});
