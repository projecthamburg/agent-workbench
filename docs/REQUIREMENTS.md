# Setup requirements and placeholders

Users supply these privately. An unset optional integration must disable itself clearly.

| Requirement | Placeholder/configuration | Qualification needed |
|---|---|---|
| Objective and success criteria | `<PROJECT_OBJECTIVE>`, `<ACCEPTANCE_RULES>` | Human intent recorded; requirements have owners/evidence |
| Project and isolated checkouts | `<PROJECT_ROOT>`, `<WORKTREE_ROOT>` | Git or equivalent; ownership and conflict rules |
| Agent tools and accounts | `<SUPERVISOR_PROVIDER>`, `<EXECUTOR_PROVIDER>`, `<ADVISOR_PROVIDER>`, `<CURATOR_PROVIDER>` | Installed versions, authenticated availability, terms and permissions |
| Model restrictions | `<ROLE_MODEL_POLICY>` | Actual account picker/list; confirmed active model, no silent fallback |
| Session continuity | `<SUPERVISOR_SESSION_ID>` | Fork/resume semantics and parent identity verified |
| Native capture adapters | `<NATIVE_SESSION_STORE>` | Available format, tool/event fidelity, workspace filtering and omissions |
| Private runtime | `<PRIVATE_RUNTIME_ROOT>` | Owner-only access, retention/backup, outside public publishing scope |
| Retrieval | `<INDEX_BACKEND>` | Source pointers, redaction, index freshness, known record retrieval |
| Role messages | `<MAIL_ROOT>` | Stable IDs, ACKs, deduplication, tested wake mechanism |
| Machine control, when needed | `<GUI_ADAPTER>` | Accessibility/screen permissions, lease and safe cleanup; optional for headless tasks |
| Human recovery | `<RECOVERY_COMMAND>` | Project-specific, usable without an active model; documented effects |
| Limits | `<TASK_TIME_LIMIT>`, `<QUERY_LIMIT>`, `<SPEND_LIMIT>` | Reservations and unknown outcomes accounted for |
| Usage measurement | `<USAGE_SOURCE>` | Dollars versus subscription limits versus estimates separated |
| Email, optional | `<OPERATOR_EMAIL>`, `<VERIFIED_SENDER_EMAIL>`, `<DEDICATED_RECEIVING_ADDRESS>` | Verified account scope, delivery and reply test |
| Email secrets, optional | `RESEND_API_KEY`, `RESEND_WEBHOOK_SECRET` | Local secret store; never literal credentials in examples |
| Publication | `<PUBLIC_REPOSITORY_URL>`, `<LICENSE>` | Reviewed allowlist, rights, no personal/client material |

Credentials and addresses are not sample data. Do not paste real examples into public issues,
fixtures, transcripts or journal entries. Include platform limitations instead of promising
identical native screen control or transcript access across providers and operating systems.
