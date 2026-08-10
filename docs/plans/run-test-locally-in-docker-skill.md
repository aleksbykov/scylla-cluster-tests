---
status: draft
domain: ai-tooling
created: 2024-05-24
last_updated: 2024-05-24
owner: null
---

# Run SCT Test Locally in Docker — AI Skill Plan

## Problem Statement

Running SCT tests locally with the Docker backend is a frequent developer task for debugging and fast iteration. However, most test configuration YAML files in `test-cases/` are designed for cloud deployments: they specify large node counts (6-192 nodes), multi-hour durations (3h-48h), and cloud-specific instance types. Running these configurations locally without modification exhausts machine resources or fails outright.

Key pain points:

- **Manual YAML editing every run**: Developers must lower `n_db_nodes`, `n_loaders`, `test_duration`, and `stress_cmd` durations by hand before each local Docker run.
- **Forgotten environment variables**: `SCT_SCYLLA_VERSION`, `SCT_ENABLE_ARGUS`, and `SCT_USE_MGMT` must be set correctly each time. Missing any of these causes failed runs or unwanted external service connections.
- **Stress command syntax varies by tool**: `cassandra-stress` uses `duration=Xm`, `scylla-bench` uses `-duration=Xm`, and other tools have their own conventions. Manual editing is error-prone.
- **Docker-specific constraints are undocumented in one place**: SMP, memory limits, missing Scylla Manager support, and monitoring stack differences are scattered across `docs/docker-backend-overview.md` but not surfaced at test launch time.

A dedicated AI skill (`run-test-locally-in-docker`) would automate config validation, parameter capping, and command generation, eliminating these repetitive manual steps.

## Current State

### Configuration System

- `sdcm/sct_config.py:489` — `test_duration: int` — Test duration in minutes, used for instance lifecycle and timeout threads.
- `sdcm/sct_config.py:521` — `n_db_nodes: IntOrList` — Number of database nodes; supports int or list for multi-DC.
- `sdcm/sct_config.py:527` — `n_loaders: IntOrList` — Number of loader nodes.
- `sdcm/sct_config.py:664` — `use_mgmt: Boolean` — Controls Scylla Manager installation; not supported on Docker backend.
- `sdcm/sct_config.py:2028` — `enable_argus: Boolean` — Controls Argus reporting; should be disabled for local runs.
- `sdcm/sct_config.py:2090` — `simulated_racks: int` — Forces GossipingPropertyFileSnitch to simulate racks.

### Existing Docker Test Configurations

- `test-cases/PR-provision-test-docker.yaml` — Minimal Docker test config: 1 db node, 1 loader, `duration=1m`, `use_mgmt: false`. This is the closest existing reference for local Docker runs.
- `test-cases/PR-provision-test.yaml` — Cloud-oriented provision test: 3 db nodes, `duration=1m`, `replication_factor=3`. Shows typical cloud-to-Docker differences.

### Docker Backend Documentation

- `docs/docker-backend-overview.md` — Documents Docker backend constraints:
  - Scylla Manager is not installed (line 14-15).
  - Monitoring stack runs on the host, not a dedicated node (line 11-12).
  - `append_scylla_args: '--smp 2 --memory 2G'` recommended for multi-node configs (line 28-29).
- `docs/docker-backend-nemesis.md` — Lists nemesis compatibility on Docker (not all nemesis work).

### Existing Skills

- No skill in `skills/` covers local test execution or Docker backend configuration.
- `skills/designing-skills/workflows/create-a-skill.md` defines the 6-phase skill creation process.
- Existing skills (`writing-unit-tests`, `writing-nemesis`, `code-review`) demonstrate the expected structure: frontmatter, Essential Principles with WHY reasoning, When to Use/When NOT to Use, Quick Reference tables, Reference Index, and Success Criteria checklist.

### What's Missing

- No AI skill that validates and adapts test configs for local Docker execution.
- No automated capping of `n_db_nodes`, `n_loaders`, `simulated_racks`, `test_duration`, or stress command durations.
- No centralized checklist for Docker-specific environment variables and resource settings.

## Goals

1. **Create a self-contained AI skill** at `skills/run-test-locally-in-docker/` that an LLM activates when a user asks to run an SCT test locally with the Docker backend.
2. **Automatically validate and cap configuration parameters** in user-selected YAML files: `n_db_nodes` <= 3, `n_loaders` <= 1, `simulated_racks` = 3, `test_duration` <= 30 minutes.
3. **Adapt all stress command durations** across tool syntaxes (`cassandra-stress`, `scylla-bench`, `latte`, `ycsb`) to not exceed the capped test duration.
4. **Generate a correct execution command** with `SCT_SCYLLA_VERSION=latest`, `SCT_ENABLE_ARGUS=false`, `SCT_USE_MGMT=false`, and `--backend docker`.
5. **Register the skill** in both `AGENTS.md` and `CLAUDE.md` for dual-platform discovery.

## Implementation Phases

### Phase 1: Create Skill Structure and SKILL.md

**Importance**: Critical
**Description**: Create the skill directory and main `SKILL.md` with frontmatter, Essential Principles, When to Use / When NOT to Use, Quick Reference tables, and Success Criteria. The frontmatter `description` must include trigger keywords ("run test locally", "docker backend", "local Docker execution", "run SCT locally") so that Claude Code activates it correctly.

**Deliverables**:
- `skills/run-test-locally-in-docker/SKILL.md`
- `skills/run-test-locally-in-docker/workflows/` directory
- `skills/run-test-locally-in-docker/references/` directory (if needed)

**Essential Principles to include in SKILL.md** (each must explain WHY):
- **Never modify tracked `test-cases/` files** — create a local copy or use environment variable overrides. Modifying tracked files causes accidental commits of local-only changes.
- **Cap resources before running** — Docker runs share the host machine's CPU and RAM. Uncapped configs (6+ db nodes, multi-hour stress) exhaust resources and freeze the machine.
- **Disable external services** — Argus and Scylla Manager are not available or needed locally. Leaving them enabled causes connection timeouts that add 5-10 minutes of wasted startup time.
- **Preserve stress command syntax** — Only change the numeric duration value; never rewrite the command structure. Each stress tool has different argument syntax and breaking it causes cryptic load failures.

**Definition of Done**:
- [ ] `SKILL.md` created with valid YAML frontmatter (`name`, `description` < 1024 chars, no angle brackets)
- [ ] Essential Principles section with 4+ principles, each explaining WHY
- [ ] When to Use (4+ scenarios) and When NOT to Use (3+ scenarios) sections
- [ ] Quick Reference table for Docker-safe parameter limits
- [ ] Reference Index linking to workflow files
- [ ] Success Criteria checklist
- [ ] SKILL.md under 500 lines

---

### Phase 2: Create Config Validation and Adaptation Workflow

**Importance**: Critical
**Description**: Create a numbered-phase workflow that guides the LLM through reading a user-specified YAML config, identifying parameters that exceed Docker-safe limits, and producing a modified configuration. The workflow must handle:

1. **Scalar parameters**: `n_db_nodes`, `n_loaders`, `simulated_racks`, `test_duration`.
2. **Stress command durations** across multiple tool syntaxes:
   - `cassandra-stress`: `duration=180m` (no dash prefix)
   - `scylla-bench`: `-duration=30m` (dash prefix)
   - `latte`/`ycsb`: various formats (mark as Needs Investigation if uncommon)
3. **Replication factor alignment**: If `n_db_nodes` is capped to N, `replication_factor=M` in stress commands must satisfy M <= N.
4. **Docker resource hints**: Recommend `append_scylla_args: '--smp 2 --memory 2G'` for multi-node configs per `docs/docker-backend-overview.md:28-29`.

**Dependencies**: Phase 1 (directory structure exists)

**Deliverables**:
- `skills/run-test-locally-in-docker/workflows/validate-and-adapt-config.md`

**Definition of Done**:
- [ ] Workflow has numbered phases with entry/exit criteria
- [ ] Covers all 4 scalar parameters with specific cap values
- [ ] Documents stress command duration patterns for `cassandra-stress` and `scylla-bench`
- [ ] Includes before/after examples for a real config (e.g., `test-cases/longevity/longevity-10gb-3h.yaml`)
- [ ] Recommends `append_scylla_args` for Docker resource management
- [ ] Instructs AI to show diff of changes for user approval before applying
- [ ] Workflow file under 300 lines

---

### Phase 3: Create Execution Command Workflow

**Importance**: Critical
**Description**: Create a workflow that assembles the final execution command. This includes setting environment variables, choosing between `uv run sct.py run-test` and `./docker/env/hydra.sh run-test`, and handling cluster reuse for iterative development.

**Dependencies**: Phase 2 (config is validated)

**Deliverables**:
- `skills/run-test-locally-in-docker/workflows/execute-local-test.md`

**Definition of Done**:
- [ ] Workflow produces a complete, copy-pasteable command block
- [ ] Environment variables set: `SCT_SCYLLA_VERSION=latest`, `SCT_ENABLE_ARGUS=false`, `SCT_USE_MGMT=false`
- [ ] `--backend docker` and `--config <path>` correctly specified
- [ ] Documents `SCT_REUSE_CLUSTER` for iterative runs
- [ ] Covers both `uv run sct.py run-test` and Hydra execution paths
- [ ] Workflow file under 300 lines

---

### Phase 4: Register Skill for Platform Discovery

**Importance**: Critical
**Description**: Register the skill in both platform discovery mechanisms so AI agents can find and activate it.

**Dependencies**: Phases 1-3 (all skill files exist)

**Deliverables**:
- Updated `AGENTS.md` Skills table with new row
- Updated `CLAUDE.md` Skills list with new entry

**Definition of Done**:
- [ ] `AGENTS.md` Skills table has `run-test-locally-in-docker` row with description and path
- [ ] `CLAUDE.md` lists `run-test-locally-in-docker` in the available skills section
- [ ] Existing symlinks (`.github/skills` -> `skills/`, `.claude/skills` -> `skills/`) auto-include the new directory
- [ ] All file references in SKILL.md resolve to existing files (no broken links)

## Testing Requirements

### Structural Validation

- Verify `SKILL.md` frontmatter is valid YAML: `name` and `description` fields present, description under 1024 characters, no angle brackets.
- Verify all markdown links in SKILL.md resolve to existing files under `skills/run-test-locally-in-docker/`.
- Verify SKILL.md is under 500 lines, workflow files under 300 lines each.
- Run `uv run sct.py pre-commit` to check formatting.

### Trigger Evaluation

Test the description field against these queries:

**Should trigger**:
- "I want to run longevity_test locally with Docker"
- "Run this test on docker backend"
- "Help me execute a test locally using Docker"
- "Adapt this config for local Docker execution"
- "How do I run SCT tests on my machine?"

**Should NOT trigger**:
- "Write a unit test for the config parser"
- "Review this PR for code quality"
- "Create a new nemesis for disk corruption"
- "Deploy this test on AWS"

### Workflow Simulation

- Take `test-cases/longevity/longevity-10gb-3h.yaml` (6 db nodes, 255m duration, 180m stress) and verify the skill:
  - Caps `n_db_nodes` to 3
  - Caps `n_loaders` to 1
  - Sets `simulated_racks` to 3
  - Reduces `test_duration` to 30
  - Changes `duration=180m` to `duration=25m` in stress commands
  - Checks `replication_factor=3` is valid with 3 nodes (passes)
  - Recommends `append_scylla_args`
  - Generates correct execution command with all environment variables

## Success Criteria

All Definition of Done items across Phases 1-4 are met. Additionally:

1. An LLM presented with "run this test locally with Docker" activates the skill and produces a working command without the user needing to manually set environment variables or edit YAML.
2. No modification is made to tracked `test-cases/` files; all changes use local copies or environment variable overrides.
3. The generated command strictly includes `SCT_SCYLLA_VERSION=latest`, `SCT_ENABLE_ARGUS=false`, `SCT_USE_MGMT=false`, and `--backend docker`.

## Risk Mitigation

### Risk: LLM corrupts stress command syntax when editing duration

**Likelihood**: Medium
**Impact**: Broken stress commands cause silent test failures or cryptic cassandra-stress errors.
**Mitigation**: The validation workflow instructs the AI to only change the numeric value after `duration=` (e.g., `duration=180m` -> `duration=25m`) and show the before/after string for explicit user approval. The workflow includes regex patterns for each tool's syntax.

### Risk: Test configurations use multi-DC node lists that complicate simple capping

**Likelihood**: Low
**Impact**: `n_db_nodes: [3, 3, 3]` (multi-DC) cannot be naively capped to `3`.
**Mitigation**: The validation workflow detects list-type `n_db_nodes` and instructs the AI to either reduce to a single-DC config (`n_db_nodes: 3`) or ask the user for guidance. Multi-DC Docker setups are uncommon for local development.

### Risk: Skill triggers on non-Docker execution requests

**Likelihood**: Low
**Impact**: AI applies Docker-specific caps to AWS/GCE configs, breaking cloud test runs.
**Mitigation**: The description field explicitly includes "docker" and "locally" as trigger keywords. The When NOT to Use section lists cloud backends. The workflow's first step confirms the user intends Docker backend before making any changes.

### Risk: Docker resource exhaustion even with capped config

**Likelihood**: Medium
**Impact**: Machine freezes or OOM kills with 3 db nodes if SMP/memory not bounded.
**Mitigation**: The workflow recommends `append_scylla_args: '--smp 2 --memory 2G'` for any config with more than 1 db node, per guidance in `docs/docker-backend-overview.md:26-29`.

## Related Plans

- [ai-skills-framework.md](ai-skills-framework.md) — Framework plan for AI skills in SCT; this plan adds a new skill following that framework.

## PR History

| Phase | PR | Status |
|-------|-----|--------|
| Phase 1 | -- | Not started |
| Phase 2 | -- | Not started |
| Phase 3 | -- | Not started |
| Phase 4 | -- | Not started |
