# The budget governor

**Goal:** keep the agents working steadily, never lock the human out of their own Claude, and never spend money by
accident.

## Where the numbers come from

When Claude Code runs headless with `--output-format stream-json --verbose`, it emits events like this:

```json
{"type": "rate_limit_event",
 "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour", "resetsAt": 1790293800,
   "isUsingOverage": false,
   "unifiedWindows": {"five_hour": {"utilization": 0.14, "resetsAt": 1790293800},
                      "seven_day": {"utilization": 0.16, "resetsAt": 1790348400}}}}
```

These are the account's own limits as Anthropic's API reports them, and they cover **everything** on the account:
all farms, all Remote Control sessions and your own use of Claude. clodfarm stores the newest one in DynamoDB
(`BUDGET/LATEST`, written only if newer) and never estimates from token counts.

Before the first run there's no data. The governor then allows one agent, which measures. `clodfarm budget
--refresh` makes one tiny call to measure on demand.

## The rules (`clodfarm/governor.py`, applied in order)

1. **Money:** if the account is drawing on paid overage and `FARM_ALLOW_OVERAGE=0` (the default), run nothing.
2. **Hard stops**, each lasting until that window resets:
   - Claude reported `rejected`;
   - the 5-hour window is at or above `FARM_FIVE_HOUR_CEILING` (0.85);
   - the 7-day window is at or above `FARM_WEEKLY_TARGET` (0.80).
3. **Pacing:** for each window, a pace line `allowed(t) = min(target, target × elapsed_fraction + band)`.
   - At or under the line, run at full concurrency. Inside the band, run at partial concurrency
     (`ceil(max_workers × headroom / band)`).
   - Over the line, run nothing until the line catches up. The governor computes that moment and sleeps until then.
   - The weekly band is tight (5%), so the week is spread out. The 5-hour band is loose (30%), so bursts are fine.
   - Pacing never drops below `FARM_MIN_WORKERS` (1). It slows the farm down; only the hard stops above stop it.
     Without this floor, normal use early in a week (right after the reset) froze the farm.
4. **Optional end-of-week burst:** with `FARM_BURST_HOURS` > 0 (off by default), weekly pacing is skipped that
   many hours before the weekly reset. The agents then run at full speed, but never past the target.

**API key mode** (`ANTHROPIC_API_KEY` set, no OAuth token): there are no subscription windows to pace. The agents run
at `FARM_MAX_WORKERS` until the day's list-price spend (summed from each run's `total_cost_usd`) reaches
`FARM_DAILY_BUDGET_USD`, then pause until 00:00 UTC. `FARM_TASK_BUDGET_USD` also passes `--max-budget-usd`
to every run.

A window whose reset time has passed counts as empty, so a stale snapshot never blocks work forever.

## How it's enforced across boxes

The governor's number is a cap on **account-wide concurrency slots** (`SLOT/<n>` items with leases). A worker must
hold a slot to start an agent. With `workers = 2`, only slots 0 and 1 can be taken, however many containers share
the table. Slots are renewed while the agent runs, released when it ends, and expire on their own if the box dies.

Runs already in progress are never killed when the cap drops. The cap only limits new starts, so a few in-flight
agents can briefly overshoot. That is one reason the ceilings default well below 100%.

## When a limit is hit anyway

If a run ends with `status: rejected`, or with a usage-limit error in its text, the attempt is handed back (it
doesn't count toward `max_attempts`) and the task is re-queued. The rejection becomes the budget snapshot, and every
worker on every box pauses until the reported reset time.

## Per-seat limits

Every seat (Claude account) can have its own limits over the farm-wide defaults, so one person can keep their Claude
at 50% of each 5-hour window while another lets theirs run to 85%:

```bash
clodfarm limits                                          # every seat's limits (* = its own)
clodfarm limits --claude mathias --five-hour 50 --weekly 60
clodfarm limits --seat friend-9c1d --max-workers 1
clodfarm limits --claude mathias --clear                 # back to the defaults
```

The farm manager, or the person of a Claude on that seat, can also set them on the farm's **USAGE** page (dock: U).
They're kept in the farm's store, so every box on that seat picks them up within a few minutes, with no restart.
`FARM_SEAT_LIMITS` (JSON, see `.env.example`) sets them from the environment; what's stored wins.

| Limit | Default from | Meaning |
|---|---|---|
| `five_hour_ceiling` | `FARM_FIVE_HOUR_CEILING` | agents stop at this share of a 5-hour window |
| `weekly_target` | `FARM_WEEKLY_TARGET` | agents stop at this share of the 7-day window (and the week is paced towards it) |
| `max_workers` | `FARM_MAX_WORKERS` | at most this many agents at once; it can lower the box's `FARM_MAX_WORKERS`, not raise it |

There's no daily window in Claude's limits: "half of my usage" means a 5-hour ceiling of 0.5, a weekly target of
0.5, or both.

## Usage telemetry

Every usage report is also kept as history: one point per seat and minute, for `FARM_USAGE_HISTORY_DAYS` (35) days.

- **USAGE page** (`/usage`): each seat's 5-hour and weekly utilization over 5 h, 24 h, 7 d or 30 d, the line its
  agents stop at, and what the governor is doing.
- **CLI:** `clodfarm usage [--claude NAME | --seat S] [--hours 24] [--json | --csv]`.
- **JSON:** `GET /api/usage?hours=24[&seat=S]`.
- **Prometheus:** `GET /metrics` with `clodfarm_utilization_ratio`, `clodfarm_limit_ratio`,
  `clodfarm_window_resets_timestamp_seconds`, `clodfarm_allowed_workers`, `clodfarm_running_workers`,
  `clodfarm_max_workers`, `clodfarm_usage_measured_timestamp_seconds` and `clodfarm_overage_in_use`, labelled by
  `seat`, `claudes` and `window`. Set `FARM_METRICS_TOKEN` to require `Authorization: Bearer <token>`.

Usage is measured from every agent run, and every `FARM_USAGE_REFRESH` seconds (300) while a Claude is idle, so an
idle farm still draws a line.

## Tuning

| Want | Set |
|---|---|
| more room for yourself | `FARM_WEEKLY_TARGET=0.7` |
| one Claude at half of each 5-hour window | `clodfarm limits --claude NAME --five-hour 50` |
| agents only in a quiet week | `FARM_WEEKLY_TARGET=0.5` |
| run only at night | `clodfarm pause` / `clodfarm resume` from cron |
| more parallelism | `FARM_MAX_WORKERS=6` (and a bigger box: about 400-600 MB RAM per agent) |

`clodfarm budget --json` shows the snapshot, the decision and the reasoning. Every run also stores the utilization
before and after it (`clodfarm result ID`), so you can see what each sub-agent cost as a share of the window.
