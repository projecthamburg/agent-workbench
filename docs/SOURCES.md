# Sources consulted for the founding design

Consulted 2026-10-05. Provider commands and availability can change; verify the installed tool.

- [OpenAI Codex command reference](https://developers.openai.com/codex/cli/reference): explicit
  session fork and working-directory selection. Local installed help additionally exposed a queue
  command; its unattended wake semantics remain to be qualified.
- [Claude Code commands](https://code.claude.com/docs/en/commands): account-visible usage, context,
  model and compaction controls, with version/plan differences.
- [Claude model and usage guidance](https://support.claude.com/en/articles/14552983-models-usage-and-limits-in-claude-code):
  task-dependent model selection and long-context cost considerations; actual account state wins.
- [Resend receiving reference](https://github.com/resend/resend-skills/blob/main/skills/resend/references/receiving.md):
  receiving API/polling, webhook verification and separate receiving-domain considerations.
- [Gemini CLI session management](https://geminicli.com/docs/cli/session-management/): a distinct
  provider format. These docs do not establish Antigravity CLI capture/commands.

Private worked-example inspection found an existing project-local source-capture tool and older
capture designs. Those motivated preserving source archives, redacted retrieval derivatives and
truncation guards. They are not redistributed here; their local identities and private records
remain outside this public scaffold. Reusable implementations must provide their own reviewed
adapters and evidence rather than relying on inaccessible private claims.

The reasoning ethos and requested roles came from the founding human's instructions. That is a
design origin, not an externally validated efficiency result.
