# Observability — what the platform emits, and where to look at it

`TO-BE.md` §12: **the platform ships capabilities, never instruments.** Four of them
— traces pushed over OTLP, structured logs on stdout with correlation in-band, the
§9 metric families as SQL views, and a violations feed with a documented alert
condition. No collector, log store, dashboard or alert router is part of the
shipment. This file is the other half of that bargain: if the platform ships no
instrument, it owes you the wiring.

The rig below is one wiring, dev-only and thrown away daily. It exists so a claim
about the whole chain can be *checked* — the same reason `tests/test_schema_walls.py`
provokes walls rather than describing them.

## The two endpoints

Two, because two processes in two network namespaces. Both are presence toggles:
unset, spans still exist (they mint the correlation ids, which cost nothing), and the
container is told nothing about telemetry at all.

| Variable | Set by | Who pushes | Rig value |
|---|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | the control plane's own env | the control plane | `http://localhost:4318` |
| `DEV_AGENT_OTLP_ENDPOINT` | the control plane, injected per burst | each burst's `claude` CLI | `http://host.docker.internal:4318` |

The second string is the one that gets it wrong. `localhost` inside the box is the
box, and **the CLI drops export failures silently** — a burst pointed at nothing
looks exactly like a burst with telemetry off. `CLAUDE_CODE_OTEL_DIAG_STDERR=1` in
the container env makes it say why instead.

What a burst emits, and why each choice, is `control_plane.telemetry.agent_telemetry_env`
(B2): traces and log events yes, metrics no, tool **arguments** yes, tool and API
**bodies** no. `CONTROL_PLANE_TRACE_CONSOLE=1` prints the control plane's own spans
instead of pushing them; it is never passed into a container, where stdout is the
SDK's message channel.

## One join key

`trace_id` is the join, and it reaches every signal without a call site passing it:

- **The trace** is one burst (`ARCHITECTURE.md` §9). The control plane opens the
  `burst` span, injects its `traceparent`, and the CLI nests its own tree under it —
  so a burst's trace holds both processes' spans. The ledger records the pair
  `(trace_id, span_id)`, because the link a *successor* burst makes names a span
  (B1).
- **Control-plane log lines** carry `trace_id` / `burst_id` / `task_id` for the
  span's lifetime — bound in `burst_span`, so every line written during a burst has
  them.
- **Agent log events** carry `trace_id` *and* `span_id`, the latter being the burst
  span's own id: the CLI parents its events to the traceparent it was given. So a log
  record points at a span, not just a trace.
- **When there is no span there is still `burst.id`.** `OTEL_RESOURCE_ATTRIBUTES`
  stamps `task.id` / `burst.id` / `burst.role` / `burst.kind` on every signal the CLI
  emits, which is what makes the one standalone `llm_request` (B2 — the quick
  haiku call, `llm_request.context = standalone`) attributable at all: it lands
  outside the interaction tree, in a trace of its own.
- **The ledger** holds the same ids as columns, so "the trace for the burst that
  opened PR 41" is a query and not a search.

## The rig

Dev-only, never shipped, swappable by an edit to two files
(`docker/compose.telemetry.yaml` and `docker/telemetry/collector.yaml`). Nothing
above those files knows which viewer is running.

```bash
docker compose -f docker/compose.yaml up -d --wait                # 1. stores
uv run control-plane migrate                                      # 2. schema + views
docker compose -f docker/compose.yaml exec -T postgres \
  psql -U platform_admin -d platform \
  < docker/telemetry/dashboard-role.sql                           # 3. the reader
docker compose -f docker/compose.telemetry.yaml up -d --wait      # 4. the rig

export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
export DEV_AGENT_OTLP_ENDPOINT=http://host.docker.internal:4318
uv run control-plane burst --issue 1 --repo ../discord-bot --prompt "…" --budget 0.5
```

Step 3 is the one that looks out of place and is not: migration `0010` grants the
views to `platform_app` and stops, because how a *dashboard* authenticates is the
operator's business, not the schema's. It is also the step that makes the read
boundary real — see below.

Three UIs, one per capability, each the native one for it:

| Where | What it holds | The question it answers |
|---|---|---|
| `http://localhost:6006` — Phoenix | the burst trace, both processes' spans | what did this burst *do*, in what order, how long |
| `http://localhost:9428/select/vmui` — VictoriaLogs | the agent's log events | what did the CLI say — prompts, tool decisions, API requests |
| `http://localhost:3000` — Grafana | the reporting views | how is the platform doing across tasks |

A LogsQL query for one burst, which is the whole correlation story in one line:

```
trace_id:"7caf15929cf77e014997da6bf8395979" | sort by (_time)
```

### What a burst looks like in Phoenix

Everything lands in the `default` project — nothing sets a Phoenix project name — and
each burst is one trace, rooted at the control plane's own `burst` span:

```
burst                              9.5s   ← the control plane's span: the whole burst
  claude_code.interaction          5.9s   ← the CLI adopted our traceparent here
    claude_code.llm_request        2.7s   LLM   model, finish_reason, ttft_ms, usage
    claude_code.tool             41.0ms   TOOL  tool_name=Bash, full_command=…, tool_use_id
      claude_code.tool.blocked_on_user     6ms
      claude_code.tool.execution          35ms  TOOL  success=true
    claude_code.llm_request        3.3s   LLM
```

Four things worth knowing when you look:

- **The gap on the left is the platform.** `burst` starts ~2.7s before the first
  CLI span — container start, clone, minting a GitHub token. Platform overhead and
  agent work are separated for free, which is most of why the burst span exists.
- **Tool arguments are there** (`full_command`, `file_path`), because tool visibility
  *is* the experiment's dataset (§9). Tool and API *bodies* are not.
- **`tool_use_id` is on both tool spans**, which is the handle B6 (#68) resolves a
  test report's citation against — visible here before anything reads it.
- **Span links are not shown, at all.** Phoenix's span model has no links field, so
  B1's link to the predecessor burst is exported and then dropped. Lineage is
  readable anyway: `burst.resumed_from` is an attribute on the root span, which is
  the reason B1 recorded it as one as well as as a link. The authority is the
  ledger's `resumed_from_burst_id`.

One trace will look wrong and is not: a lone `claude_code.llm_request` with no parent
and `llm_request.context = standalone`. That is the CLI's quick side-call, outside the
interaction tree and in a trace of its own (B2) — attributable only by its `burst.id`,
which is why `OTEL_RESOURCE_ATTRIBUTES` stamps every signal.

### What a burst looks like in VictoriaLogs

The content lives here, not in the spans. Eight event types, measured on a 3-tool
burst — a work burst on a real issue is the same shapes, tens of times over:

| Event | Per | Carries |
|---|---|---|
| `user_prompt` | burst | the **full prompt text** — for a scheduled burst, what the platform composed and actually said |
| `tool_decision` | tool call | `decision`, `source`, `tool_name`, **`tool_parameters`** (the arguments), `tool_use_id` |
| `tool_result` | tool call | `success`, `duration_ms`, `tool_input`, and *sizes* rather than bodies (`tool_result_size_bytes`) |
| `hook_execution_start` / `_complete` | tool call | `hook_event=PreToolUse`, `hook_name`, `num_hooks`, `num_blocking`, `total_duration_ms` — the **wall's own execution** |
| `api_request` | model call | **`cost_usd`, `input_tokens`, `output_tokens`, `cache_read_tokens`**, `duration_ms`, `model`, `effort`, `speed` |
| `assistant_response` | turn | the full response text |
| `plugin_loaded` | burst | the toolkit plugin the image installs |

Three consequences worth carrying around:

- **The per-request cost and token breakdown is here**, in `api_request` — so what
  Phoenix declines to count is an accounting gap in one viewer, not missing data. The
  ledger's burst row remains the authority for the total.
- **`num_blocking` on the hook events is the wall reporting itself**, once per tool
  call: `0` means it let the call through. The deny path should read `1` with
  `decision=deny`, which is stated rather than measured — the burst that was supposed
  to trip a wall declined to call the tool at all.
- **Tool arguments include the `git` and `gh` commands**, so a PR's title and body
  text arrive here as tool parameters. That is the intended dataset (§9 — tool
  visibility *is* the experiment) and it is also the concrete answer to what a burst
  puts in front of an adopter's collector.

**A collector in front of both, on purpose.** `ARCHITECTURE.md` §9 originally paired
Phoenix's own OTLP listener directly with the emitters. The rig puts a collector
there instead for three reasons that outlived the shortcut: logs need a second
destination and Phoenix takes traces only; both processes should learn exactly one
address; and the swap TO-BE promises then happens in a config file rather than in an
environment variable the platform sets.

**No metrics pipeline, deliberately.** The ledger is the metrics store, so an
exported metric would be a second copy of a ledger fact. The collector has nowhere
to put one, which means it refuses the export rather than quietly keeping it — if
this rig ever starts rejecting metrics you did not know were being sent, that is the
wall working.

**No volumes.** `down` leaves nothing and the next `up` starts empty. A rig that
accumulates history is a store, and the store this platform has is the ledger.

### When nothing arrives

In order, cheapest first:

1. `docker compose -f docker/compose.telemetry.yaml logs otel-collector` — the
   `debug` exporter prints per-batch counts (`resource spans`, `log records`). If
   they are there, the emitters are fine and the problem is downstream.
2. The same log, for export failures. A destination that is down shows up here as a
   retry loop, and it is the *only* place it shows up: the CLI is silent by design
   and the control plane's exporter is not much louder.
3. `docker compose -f docker/compose.telemetry.yaml ps -a` — with `-a`, because a
   dead container disappears from the default listing and its DNS name goes with it.
   A destination that crashed reads upstream as `lookup victorialogs … no such
   host`, which points at the network and means the process.
4. Only then the endpoint variables, and the `host.docker.internal` trap above.

Two things measured on the rig that are worth knowing before they cost you an hour:

- **Do not make a correlation id a VictoriaLogs stream field.** The CLI sends
  `task.id` / `burst.id` / `burst.role` / `burst.kind` on the resource *and* on every
  log record, so each name matches twice, and VictoriaLogs answers a duplicate
  stream tag with a FATAL panic that takes down the process — after which the
  symptom is the DNS error above, two containers from the cause. The rig streams on
  `service.name` alone; every other attribute is still an ordinary searchable field,
  which is all correlation needs (`docker/telemetry/collector.yaml`).
- **Phoenix types the spans but cannot count them.** It reads the CLI's `gen_ai.*`
  attributes and classifies `claude_code.llm_request` as `LLM`, `claude_code.tool`
  and `.tool.execution` as `TOOL` — so the LLM-flavored views do engage. What stays
  at zero is its **token and cost accounting**: the CLI reports usage as
  `input_tokens` / `output_tokens` / `cache_read_tokens`, not `gen_ai.usage.*`, so
  Phoenix's totals read 0 and its cost is null while the numbers sit right there in
  the attributes. Read cost from the burst row, which is the authoritative copy
  anyway (§9). Content panes are empty by *our* choice — bodies off, the transcript
  is the deep record.

## The alert condition

`TO-BE.md` §12: **any new `violations` row is a page.** The platform emits and never
routes, so the condition is written down here in the three forms an adopter's tooling
can consume — with what each costs, because they are not equivalent.

A wall hit is not a crash. It is an agent trying something the walls stopped, which
is the experiment's primary dataset (§9) — and a *new* `reason` means the walls met
something they had not met before.

**1. Pull, through the read boundary.** What the reference dashboard uses, and the
only form a least-privilege dashboard role can run:

```sql
SELECT repository, tool, reason, hits, last_seen
  FROM reporting.containment
 WHERE last_seen > now() - interval '5 minutes';
```

Any row means page. The window must be longer than your poll interval or a hit lands
between two polls and is never seen. Fidelity is group grain — `(repository, tool,
reason)` — so a second identical hit inside the window keeps one alert firing rather
than raising a new one. For paging that is usually right: somebody is already
looking.

**2. The outbox, by cursor.** Exact, one notification per row, and the form that
matches the sentence in TO-BE:

```sql
SELECT id, kind, payload FROM events
 WHERE kind = 'violation_recorded' AND id > $cursor ORDER BY id;
```

The event and the `violations` row are written in one statement — both or neither
(`STORES.md`, "The events outbox") — so a consumer that keeps its own cursor cannot
miss a violation. `payload` carries `violation_id`, `task_id`, `burst_id`, `tool`,
`reason`: enough to route on, and the row it names is there for the rest. `events` is
a base table, so this needs the runtime's role or one like it, not the dashboard's.

**3. The rows themselves**, `SELECT … FROM violations WHERE id > $cursor` — the same
privilege cost as form 2 and no outbox consumer to write, at the price of the
`detail` column being in front of whoever gets paged.

One caveat on both cursors: an id watermark is safe while one process writes, which
is the case today. When E2 (#33) puts two replicas behind one ledger, a row can
commit *after* the cursor has passed its id, and the fix is to overlap — re-read a
few seconds behind the watermark, or page on the window of form 1, which cannot skip.

Alerting on anything else the platform emits is the adopter's to define; this is the
one condition the platform states, because it is the one where the platform knows
what the number means.

## Reference dashboards

`examples/dashboards/` — examples, not components:

- `reporting.sql` — every panel as a statement any Postgres driver can run. No tool
  macros and nothing outside `reporting.*`, so the file is portable and stays inside
  the boundary.
- `grafana-reporting.json` — the same statements as a dashboard, provisioned into the
  rig's Grafana straight from `examples/` (so the rig renders the committed file, not
  a copy of it).

`tests/test_dashboards.py` runs every one of those statements against the views **as
a role with nothing but the two grants**, and holds the JSON's copy of the SQL to the
`.sql` file's. A panel that reaches for a base table fails there rather than in
front of somebody.

**The reader is a role, and it is the operator's.** Views execute with their owner's
privileges, so USAGE on `reporting` plus SELECT on its views buys every aggregate the
platform publishes and no row of `tasks`, `bursts` or `violations`:

```sql
GRANT USAGE ON SCHEMA reporting TO <dashboard_role>;
GRANT SELECT ON ALL TABLES IN SCHEMA reporting TO <dashboard_role>;
```

`docker/telemetry/dashboard-role.sql` is those two statements plus a rig password and
a default-privileges line, and the rig's Grafana connects as that role — so the
reference dashboard cannot quietly come to depend on a base table. Point it at
`platform_app` instead and the boundary is gone, silently, with everything still
working. That is why it is written down.

## No server

The laptop default. Control-plane logs are logfmt on stdout and teed per burst to
`data/logs/{burst_id}.log`, correlation ids in-band, so the whole capability works
with no rig at all:

```bash
lnav data/logs/*.log                                   # one pane, timestamps merged
grep 757ffedcafce55e098216f977d14f51c data/logs/*.log   # one burst, by trace id
```

That is the entire logs contract — 12-factor out, correlation in-band, consumable by
any pipeline. No shipper is shipped or assumed; the rig ingests what the platform
*pushes*, which is the agent's log events, and the control plane's own lines stay on
stdout where the operator's own stack collects them.
