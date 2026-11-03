# MCP curated tool surface

`POST /mcp` advertises 25 tools by default: 18 core intake/session/status tools and seven action-enum domain tools. The compact `tools` JSON measures 15,488 UTF-8 bytes and 3,384 o200k tokens, below the 18,000-byte and 3,850-token limits. The core `intake_show` accepts `query.view=history|links|context`; `sessions_list` accepts `query.view=lifecycle|tree`.

The action domains are `intake_governance`, `bus_read`, `bus_write`, `conversations_read`, `conversations_write`, `fleet_read`, and `ops_write`. Each action is resolved to its existing MCP route and then uses the same `toolRequest` validation and HTTP dispatch. `intake_update` still runs through the existing bounded CAS retry logic. Read domains explicitly advertise `readOnlyHint=true` and `destructiveHint=false`; write domains explicitly advertise `readOnlyHint=false` and `destructiveHint=true`. The legacy `conversations_read` POST receipt action appears as `mark_read` under `conversations_write` so every action in the read domain is actually read-only.

## Legacy compatibility

`POST /mcp?toolsets=all` and `POST /mcp?toolsets=legacy` return the historical 87-tool discovery set. `LEGACY_MCP_ALIASES` maps those names to their canonical core or domain tool. Calls using every route name currently registered in `MCP_ROUTES` remain accepted. Since the current main branch has grown to 116 route names, the 29 newer names are recorded separately as `EXTENDED_MCP_ALIASES`; they remain callable and eligible read/write actions appear in the compact domain enums without changing the specified 87-name legacy list.

The following host callbacks and replaced launcher/job calls remain callable for compatibility but are not discoverable from the default tool list or domain action enums: `heartbeat`, `sessions_observe`, `task_result`, `start_agent`, `start_session`, `plan_agent`, `tasks_submit`, `jobs_list`, `jobs_show`, and `artifact`. Their CLI leaves have explicit reasons in `CLI_EXCLUSIONS`; their MCP names remain in the corresponding alias maps.

## Deprecation observation

The Hub records daily aggregates for raw tool name, canonical tool name, alias/canonical classification, caller kind, count, and last-call timestamp. The aggregate keeps a rolling 30-day window and stores no caller IDs, bearer values, arguments, or payloads. The window supports measuring old-name use before any future alias retirement.
