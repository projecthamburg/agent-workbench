# Agent Workbench

A project-independent design for supervised work across several AI tools: a supervisor, an
executor forked when implementation begins, an evaluator/advisor, a curator/researcher, and a
human recovery terminal.

**Status: tested cooperative-control prototype.** The persistent lease/coordinator and small
native pause-request panel are implemented. There is no bootable launcher, installed plugin
or qualified unattended workflow in this revision. The first worked example is an AI-visibility
research pipeline; its browser and network rules belong to an adapter, not to the generic core.

The human defines the project and priorities with the supervisor. The advisor tests interpretations;
the curator keeps source-grounded memory and researches unresolved questions. At implementation
time, a context-preserving executor fork receives bounded tasks in an isolated checkout. Independent
review/research can proceed in parallel; shared edits, integration and machine control are serialized.

See [design](docs/DESIGN.md), [requirements](docs/REQUIREMENTS.md),
[implementation batches](docs/ROADMAP.md), [reasoning and evidence](docs/EVIDENCE.md),
[sources](docs/SOURCES.md) and the [founding journal](journal/2026-10-05-founding.md).

Public examples contain placeholders. Credentials, personal identifiers, native sessions, client
evidence and screenshots belong in a private project runtime outside the public repository.
Do not copy a private project history into this repository to create the worked example.

The POSIX standard-library core requires Python 3.9 or later. The panel additionally needs a
working Tk installation; imports alone do not establish GUI compatibility. Run the 46 offline
contract tests from this directory:

```sh
python3 -m unittest discover -s tests -p 'test_workbench*.py' -v
```

Read the [lease contract](docs/MACHINE-LEASE.md), [coordinator protocol](docs/COORDINATOR.md)
and [panel limits](docs/PANEL.md) before adopting them. The application must supply semantic
restoration checks and route every shared-machine action through its coordinator. Those
adapter integrations are not supplied here. See the [implementation journal](journal/2026-10-05-cooperative-control.md)
for evidence, review repairs and unresolved assumptions.
