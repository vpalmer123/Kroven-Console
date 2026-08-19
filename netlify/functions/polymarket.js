export default async (req) => {
  if (req.method !== "GET") return new Response("GET only", { status: 405 });
  try {
    const url = new URL(req.url);
    const q = (url.searchParams.get("q") || "").toLowerCase();
    const r = await fetch(
      "https://gamma-api.polymarket.com/events?limit=100&active=true&closed=false&order=volume&ascending=false"
    );
    const events = await r.json();
    const words = q.split(/\s+/).filter((w) => w.length > 2);
    const scored = (Array.isArray(events) ? events : [])
      .map((e) => ({
        e,
        score: words.reduce(
          (s, w) => s + ((e.title || "").toLowerCase().includes(w) ? 1 : 0),
          0
        ),
      }))
      .filter((x) => x.score > 0)
      .sort((a, b) => b.score - a.score)
      .slice(0, 5)
      .map((x) => x.e);

    const results = scored.map((e) => {
      const markets = (e.markets || [])
        .slice(0, 3)
        .map((m) => {
          let outStr = "n/a";
          try {
            const prices = JSON.parse(m.outcomePrices || "[]");
            const outcomes = JSON.parse(m.outcomes || "[]");
            outStr = outcomes
              .map((o, i) => `${o} ${(parseFloat(prices[i]) * 100).toFixed(0)}%`)
              .join(", ");
          } catch {}
          return `${m.question || m.groupItemTitle || ""}: ${outStr}`;
        })
        .join(" | ");
      const vol = Math.round(e.volume || 0).toLocaleString();
      return `${e.title} (volume $${vol}): ${markets}`;
    });

    return new Response(JSON.stringify({ results }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  } catch (e) {
    return new Response(JSON.stringify({ error: e.message, results: [] }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }
};
export const config = { path: "/api/polymarket" };
