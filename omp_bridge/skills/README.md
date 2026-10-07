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
use the `to_manager` tool and answer staged deliveries), and (C-D70 (7))
`workbench-recovery` (manager only, agent-only: what to do on a Workbench
recovery notice with `workbench_status`, a follow-up, cancel or
`restart_worker`). Their text must match the tool schemas in
`omp_bridge/g3/bridge.ts`.

OMP 18.7.0 (installed; 18.6.1 is not available locally) has no per-skill
frontmatter that hides a skill from the user's `/skill:` menu while the model
still sees it: `hide`/`disableModelInvocation` hide it from the model, and
`skills.enableSkillCommands: false` drops every skill command. So
`workbench-recovery` is listed in the manager's slash menu; its description says
it is agent-only.

The backend start-up isolation check reports any loaded skill that is not in
this directory (after the role filter) as a leak.
