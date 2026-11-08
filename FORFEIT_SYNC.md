# Forfeit sync

Forfeit has no public API, so task-rewards can't pull goals itself. Instead a
bridge reads your goals through the Overlord (Forfeit) connector in Claude and
pushes approved days to task-rewards.

## 1. Server setup

Set a long random secret in the deployment env vars (Vercel → Settings →
Environment Variables) and redeploy:

```
FORFEIT_SYNC_TOKEN=<random string>
```

Without it the endpoint answers `503 forfeit_sync_disabled`.

## 2. Endpoint

`POST /api/forfeit/sync` with header `Authorization: Bearer <FORFEIT_SYNC_TOKEN>`

```json
{"goals": [
  {"goal_id": "goal_9h0imdj1", "name": "Breakfast by 11:30am", "date": "2026-10-28", "status": "approved"}
]}
```

- Only `status: "approved"` earns points. Every goal sent (any status) is
  listed under **Settings → Forfeit goal scores**, where you can set its own
  value; blank uses the default `forfeit_goal` score (2.0).
- Each goal is credited once per date, so re-sending is safe.
- Dates up to 7 days back are accepted. Today's goals count toward the daily
  total (mood multiplier applies); past days are credited directly with a
  transaction in History.

## 3. Bridge (Claude scheduled task)

Run this prompt on a schedule (e.g. hourly, or 00:30 + 12:00 daily) from a
Claude task that has the Overlord connector enabled:

> Use the Overlord connector. Call `list_goals`, then for each goal whose
> date range covers the last 2 days call `get_goal_actions` with
> `days_back=2`. Collect every (goal_id, name, date, status) for those days,
> including non-approved ones so new goals show up in Settings. POST them as
> `{"goals":[{"goal_id","name","date","status"}]}`
> to `https://<your-app>/api/forfeit/sync` with header
> `Authorization: Bearer <FORFEIT_SYNC_TOKEN>`. Report what was credited
> and skipped. Never submit evidence or change anything in Forfeit.
