# Workbench skills

This directory is the only skill source the Workbench-launched OMP processes
load (C-D59). The launcher passes it as `skills.customDirectories` in the
per-role overlay while every ambient skill source (`~/.agents/skills`,
`~/.omp/agent/skills`, project `.omp/skills`, `.claude/skills`, ...) is turned
off by `omp_bridge/omp-isolation.yml`.

Layout: one directory per skill, `<name>/SKILL.md` with `name` and
`description` frontmatter. This README is not a skill.

Role filtering: `ROLE_SKILL_PATTERNS` in `src/workbench/backend/launcher.py`
maps `manager`/`worker` to glob patterns written as `skills.includeSkills` in
that role's overlay. An empty tuple means no role filter (every skill here is
offered to that role). Current skills (C-D65, CW-18 U6): `to-worker` (manager
only; how to use the `to_worker` tool) and `to-manager` (worker only; how to
use the `to_manager` tool and answer staged deliveries). Their text must match
the tool schemas in `omp_bridge/g3/bridge.ts`.

The backend start-up isolation check reports any loaded skill that is not in
this directory (after the role filter) as a leak.
