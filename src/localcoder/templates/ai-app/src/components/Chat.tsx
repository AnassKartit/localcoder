"use client";
import { useState, useRef, useEffect, FormEvent } from "react";

type Message = { role: "user" | "assistant"; content: string };

export default function Chat({
  placeholder = "Type a message...",
  initialMessage,
}: {
  placeholder?: string;
  initialMessage?: string;
}) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState(initialMessage || "");
  const [loading, setLoading] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function send(e: FormEvent) {
    e.preventDefault();
    if (!input.trim() || loading) return;

    const userMsg: Message = { role: "user", content: input.trim() };
    setMessages((prev) => [...prev, userMsg]);
    setInput("");
    setLoading(true);

    const assistantMsg: Message = { role: "assistant", content: "" };
    setMessages((prev) => [...prev, assistantMsg]);

    try {
      const res = await fetch("/api/ai", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: userMsg.content,
          history: messages,
        }),
      });

      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.error || "Request failed");
      }

      const reader = res.body!.getReader();
      const decoder = new TextDecoder();
      let full = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        full += decoder.decode(value, { stream: true });
        setMessages((prev) => {
          const updated = [...prev];
          updated[updated.length - 1] = { role: "assistant", content: full };
          return updated;
        });
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Something went wrong";
      setMessages((prev) => {
        const updated = [...prev];
        updated[updated.length - 1] = {
          role: "assistant",
          content: `Error: ${msg}`,
        };
        return updated;
      });
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      {/* Messages */}
      <div className="space-y-3 min-h-[200px] max-h-[60vh] overflow-y-auto">
        {messages.map((m, i) => (
          <div
            key={i}
            className={`animate-in rounded-xl px-4 py-3 ${
              m.role === "user"
                ? "glass ml-8"
                : "bg-[var(--surface)] mr-8 border border-[var(--border)]"
            }`}
          >
            <p className="text-xs font-medium mb-1 text-[var(--muted)]">
              {m.role === "user" ? "You" : "AI"}
            </p>
            <p className="whitespace-pre-wrap text-sm leading-relaxed">
              {m.content}
              {loading && i === messages.length - 1 && m.role === "assistant" && (
                <span className="inline-block w-2 h-4 bg-[var(--accent)] ml-1 animate-pulse" />
              )}
            </p>
          </div>
        ))}
        <div ref={endRef} />
      </div>

      {/* Input */}
      <form onSubmit={send} className="flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={placeholder}
          disabled={loading}
          className="flex-1 glass rounded-xl px-4 py-3 text-sm outline-none focus:ring-1 focus:ring-[var(--accent)] placeholder:text-[var(--muted)] disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={loading || !input.trim()}
          className="px-5 py-3 rounded-xl bg-[var(--accent)] text-white font-medium text-sm hover:brightness-110 transition disabled:opacity-40"
        >
          {loading ? "..." : "Send"}
        </button>
      </form>
    </div>
  );
}
