// Starts hn-blackout's post-poem workflow at 09:00 IST via workflow_dispatch.
// Secret: GH_DISPATCH_TOKEN, a fine-grained PAT for mr-dinesh/hn-blackout only,
// with Actions: read and write.
const URL_ =
  "https://api.github.com/repos/mr-dinesh/hn-blackout/actions/workflows/post-poem.yml/dispatches";

export default {
  async scheduled(event, env, ctx) {
    if (!env.GH_DISPATCH_TOKEN) throw new Error("GH_DISPATCH_TOKEN is not set");
    const res = await fetch(URL_, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "hn-blackout-trigger",
      },
      body: JSON.stringify({ ref: "main" }),
    });
    // 204 = dispatched. Throwing marks the cron run failed in the dashboard.
    if (res.status !== 204) {
      throw new Error(`dispatch failed: ${res.status} ${await res.text()}`);
    }
    console.log("dispatched post-poem.yml");
  },
};
