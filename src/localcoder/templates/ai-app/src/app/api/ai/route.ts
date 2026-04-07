import { NextRequest, NextResponse } from "next/server";

// ── Swappable AI Backend ──
// Change ONE env var to switch between:
//   Local:   LLM_API_BASE=http://localhost:8089/v1    (llama-server / Ollama)
//   RunPod:  LLM_API_BASE=https://api.runpod.ai/v2/YOUR_ID/openai/v1
//   OpenAI:  LLM_API_BASE=https://api.openai.com/v1
//   Gemini:  LLM_API_BASE=https://generativelanguage.googleapis.com/v1beta/openai
//   Groq:    LLM_API_BASE=https://api.groq.com/openai/v1

const API_BASE = process.env.LLM_API_BASE || "http://localhost:8089/v1";
const API_KEY = process.env.LLM_API_KEY || "no-key-required";
const MODEL = process.env.LLM_MODEL || "local";

// ── System prompt — THIS IS WHAT THE LLM CUSTOMIZES ──
const SYSTEM_PROMPT = `{{SYSTEM_PROMPT}}`;

export async function POST(req: NextRequest) {
  const { message, history = [] } = await req.json();

  const messages = [
    { role: "system", content: SYSTEM_PROMPT },
    ...history.slice(-10),
    { role: "user", content: message },
  ];

  try {
    const response = await fetch(`${API_BASE}/chat/completions`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${API_KEY}`,
      },
      body: JSON.stringify({
        model: MODEL,
        messages,
        max_tokens: 2048,
        temperature: 0.7,
        stream: true,
      }),
    });

    if (!response.ok) {
      const err = await response.text();
      return NextResponse.json({ error: err }, { status: response.status });
    }

    // Stream the response
    const encoder = new TextEncoder();
    const stream = new ReadableStream({
      async start(controller) {
        const reader = response.body!.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() || "";

          for (const line of lines) {
            if (!line.startsWith("data: ")) continue;
            const data = line.slice(6).trim();
            if (data === "[DONE]") continue;
            try {
              const json = JSON.parse(data);
              const content = json.choices?.[0]?.delta?.content;
              if (content) {
                controller.enqueue(encoder.encode(content));
              }
            } catch {}
          }
        }
        controller.close();
      },
    });

    return new Response(stream, {
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  } catch (error: unknown) {
    const msg = error instanceof Error ? error.message : "AI request failed";
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
