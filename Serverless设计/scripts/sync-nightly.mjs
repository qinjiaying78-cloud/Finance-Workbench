import { setTimeout as delay } from 'node:timers/promises';
import { pathToFileURL } from 'node:url';

const terminal = new Set(['succeeded', 'partial', 'failed']);
const retryable = new Set([408, 425, 429, 500, 502, 503, 504]);

export async function runSync({ projectUrl, secret, requestKey, fetcher = fetch, wait = delay, timeoutMs = 14 * 60_000 }) {
  const base = new URL(projectUrl);
  if (base.protocol !== 'https:' || base.username || base.password || base.pathname !== '/' || base.search || base.hash) {
    throw new Error('SUPABASE_URL must be an HTTPS project origin');
  }
  if (secret.length < 32 || !/^[a-zA-Z0-9:_-]{1,120}$/.test(requestKey)) throw new Error('Invalid sync secret or request key');
  const url = new URL('/functions/v1/sync-cron', base);
  const deadline = Date.now() + timeoutMs;
  let failures = 0;
  let runId;
  while (Date.now() < deadline) {
    let response;
    try {
      response = await fetcher(url, {
        method: 'POST', redirect: 'error',
        headers: { 'content-type': 'application/json', 'x-fw-cron-secret': secret },
        body: JSON.stringify({ request_key: requestKey }),
        signal: AbortSignal.timeout(Math.max(1, Math.min(40_000, deadline - Date.now()))),
      });
    } catch {
      if (++failures >= 5) throw new Error('Sync endpoint is unreachable after retry');
      await wait(Math.min(30_000, 2 ** failures * 1000));
      continue;
    }
    if (retryable.has(response.status)) {
      if (response.body) await response.body.cancel().catch(() => {});
      if (++failures >= 5) throw new Error(`Sync endpoint retry limit reached (HTTP ${response.status})`);
      const seconds = Number(response.headers.get('retry-after')) || 2 ** failures;
      await wait(Math.min(60, Math.max(2, seconds)) * 1000);
      continue;
    }
    if (!response.ok) throw new Error(`Sync request rejected (HTTP ${response.status})`);
    failures = 0;
    const result = await response.json();
    if (!/^[0-9a-f-]{36}$/i.test(result.run_id) || (runId && result.run_id !== runId)) throw new Error('Unexpected run ID');
    runId = result.run_id;
    if (terminal.has(result.status)) {
      const stats = Object.fromEntries(['read','new','success','anomalies'].map(key => {
        const value = result.stats?.[key];
        if (!Number.isSafeInteger(value) || value < 0) throw new Error('Invalid sync statistics');
        return [key,value];
      }));
      console.log(JSON.stringify({ run_id:runId,status:result.status,...stats }));
      if (result.status !== 'succeeded') throw new Error(`Sync ended ${result.status}; review the in-app sync log`);
      return;
    }
    if (!['queued','running'].includes(result.status)) throw new Error('Unexpected sync status');
    await wait(3000);
  }
  throw new Error('Sync did not finish before the job deadline; retry the same workflow run');
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const { SUPABASE_URL, FW_CRON_SECRET, GITHUB_RUN_ID } = process.env;
  if (!SUPABASE_URL || !FW_CRON_SECRET || !GITHUB_RUN_ID) {
    console.error('Missing SUPABASE_URL, FW_CRON_SECRET, or GITHUB_RUN_ID');
    process.exitCode = 1;
  } else {
    try {
      await runSync({ projectUrl:SUPABASE_URL, secret:FW_CRON_SECRET, requestKey:`github:${GITHUB_RUN_ID}` });
    } catch (error) {
      // Do not print response payloads, credentials, document IDs or stack traces to Actions logs.
      console.error(error instanceof SyntaxError ? 'Sync endpoint returned malformed JSON' : error.message);
      process.exitCode = 1;
    }
  }
}
