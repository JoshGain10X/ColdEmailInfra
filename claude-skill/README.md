# Claude Code skill — cold-email-infra

A Claude Code skill that wraps the v2 multi-tenant ColdEmailInfra API for natural-language management of cold email infrastructure (domains, shards, mailboxes, Bison workspaces) across multiple agency clients.

## Install on a new machine

```bash
# Clone or pull this repo somewhere first, then:
cp -r claude-skill/cold-email-infra ~/.claude/skills/
cd ~/.claude/skills/cold-email-infra
cp .env.example .env
# Edit .env and paste the INFRA_API_KEY (from your password manager)
```

Open Claude Code in any directory and the skill auto-loads. Trigger it with:

- "what's the status of our cold email infra"
- "onboard a new client called Acme"
- "buy 5 domains for 10x-managers"
- "deploy a shard for acme-co on the domain we just bought"
- "destroy <domain>"

See `cold-email-infra/SKILL.md` for the full command list and `cold-email-infra/workflows.md` for guided walkthroughs.

## Keep in sync

When changes are made to the live skill at `~/.claude/skills/cold-email-infra/`, copy them back to this repo and commit:

```bash
cp -r ~/.claude/skills/cold-email-infra/* claude-skill/cold-email-infra/
# DO NOT copy .env — .gitignore'd both at the skill level and the repo level
git add claude-skill && git commit -m "..."
```

## File map

- `cold-email-infra/SKILL.md` — frontmatter + command reference + safety rules
- `cold-email-infra/api-reference.md` — every endpoint, request/response shape, curl recipes
- `cold-email-infra/workflows.md` — long flows (onboarding, purchase, deploy cycle, teardown, status)
- `cold-email-infra/signature-formula-help.md` — how to draft a per-client signature pool at onboarding
- `cold-email-infra/.env.example` — config template (INFRA_API_BASE, INFRA_API_KEY)
