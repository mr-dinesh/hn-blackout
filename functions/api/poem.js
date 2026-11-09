// POST /api/poem — { titles: string[] } → { title, poem: string[], model }
// The prompt is built here, not by the client, so this can't be used as a
// general-purpose LLM proxy.
// First model that works wins. Keep model-watch/watch.json in the monorepo in sync.
const MODELS = [
  "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
  "@cf/mistralai/mistral-small-3.1-24b-instruct",
];

const SCHEMA = {
  type: "object",
  properties: {
    title: { type: "string" },
    poem: { type: "array", items: { type: "string" } },
  },
  required: ["title", "poem"],
};

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

function buildPrompt(titles) {
  const numbered = titles.map((t, i) => `${i + 1}. ${t}`).join("\n");
  return `You are a blackout poet in the style of Austin Kleon. From these Hacker News headlines, select exactly 10–15 words that form a haunting, evocative poem when read in sequence.

Rules:
- Each word MUST appear exactly as written in the headlines (preserve capitalisation and any attached punctuation)
- Return ONLY valid JSON: {"title": "short evocative poem title", "poem": ["word1", "word2", ...]}
- Prefer unexpected juxtapositions — fragments that resonate, feel mysterious, human, or strange
- Avoid mundane connectives: and, the, a, of, in, to, for, is, are, was, with, on, at, by, an

Headlines:
${numbered}`;
}

// Workers AI returns `response` as an object when JSON mode works, or as a
// string (sometimes wrapped in prose or code fences) when it doesn't.
function parseResponse(r) {
  if (r && typeof r === "object") return r;
  if (typeof r !== "string") return null;
  const m = r.match(/\{[\s\S]*\}/);
  if (!m) return null;
  try { return JSON.parse(m[0]); } catch { return null; }
}

export async function onRequestPost({ request, env }) {
  let body;
  try { body = await request.json(); } catch { return json({ error: "Invalid JSON" }, 400); }

  const titles = body?.titles;
  if (!Array.isArray(titles) || titles.length < 1 || titles.length > 30 ||
      !titles.every(t => typeof t === "string" && t.trim())) {
    return json({ error: "titles must be an array of 1–30 non-empty strings" }, 400);
  }
  const clean = titles.map(t => t.trim().slice(0, 300));
  const prompt = buildPrompt(clean);

  let lastErr = "No model available";
  for (const model of MODELS) {
    try {
      const resp = await env.AI.run(model, {
        messages: [{ role: "user", content: prompt }],
        temperature: 0.9,
        max_tokens: 300,
        response_format: { type: "json_schema", json_schema: SCHEMA },
      });
      const out = parseResponse(resp?.response);
      const poem = Array.isArray(out?.poem) ? out.poem.filter(w => typeof w === "string" && w.trim()) : [];
      if (poem.length) {
        return json({ title: typeof out.title === "string" && out.title ? out.title : "untitled", poem, model });
      }
      lastErr = `${model}: no poem in reply`;
    } catch (err) {
      lastErr = `${model}: ${err.message}`;
      console.warn("hn-blackout: model failed —", lastErr);
    }
  }
  return json({ error: lastErr }, 502);
}
