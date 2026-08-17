# Drafting a signature formula at onboarding

The signature formula is what makes each mailbox's emails look different from every other mailbox's — critical for deliverability because mailbox-providers can spot identical signatures across many senders and flag the whole shard.

A formula has six pools and two rates. Each sender's signature is deterministic from their email address (same email → always same signature), but across 100 mailboxes you get 100 different-looking variants.

> **Hard rule: a signature never contains an email address.** Not the sender's own, not a shared inbox, not anyone's. The sender's address is already in the `From` header, so repeating it adds nothing — and recipient mail clients auto-link a bare address, putting a linked token in every message we send. This is deliverability principle P04, alongside the existing bans on URLs and hyperlinked company names.
>
> This is enforced in code, not left to judgement: `_sanitize_signature` in `api/jobs.py` strips any address from every signature on its way to Bison, neither generator composes one, and a CHECK constraint pins `include_email_rate` to 0 on `signature_formulas`. Do not add an address to a pool and do not reintroduce a rate for it. 835 live signatures were retro-cleaned on 2026-08-17.

## What goes into a formula

| Field | What it is | How many | Source |
|---|---|---|---|
| `style` | `html` or `plaintext` | one | Default `html`. Use `plaintext` only for spartan brands (e.g. ReachOS uses plaintext) |
| `company_names` | Variants of the brand name | 3–6 | "Acme", "Acme Inc", "Acme Ltd", "Acme Ltd UK" |
| `titles` | Job titles to rotate | 10–15 | The kinds of people who would plausibly send this email |
| `quotes` | Punchy POV statements | 4–8 | One-liners that match the client's brand voice. Optional — set rate to 0 if the client doesn't want quotes |
| `optouts` | Soft opt-out invitations | 12–18 | Natural ways to say "reply if you want me to stop" |
| `include_pronouns_rate` | 0–1 | one | Default 0.3 — 30% of signatures show "(she/her)" |
| `include_quote_rate` | 0–1 | one | Default 0.43 |
| `format_variants` | How many name+company layouts to use | 6 | Keep at 6 unless the client wants more variation |

The table has no email-address row on purpose — see the hard rule above. The `include_email_rate` column still exists on the table for schema history, but it is pinned to 0, nothing reads it, and the dataclass has no matching field.

## Interview pattern

Walk through these questions in order. Push back gently if answers feel generic.

### 1. Company name variants

"Give me 3–6 ways you'd write your company name. Include any legal entity variants — 'Acme', 'Acme Inc', 'Acme Limited', 'Acme UK', etc."

### 2. Job titles

"Cold emails go out under different people's names. Who are the people sending these? Give me 10–15 plausible titles."

Push: don't accept just "Sales Rep". Diversify across:
- Junior + senior
- Function (Sales, Marketing, Customer Success, Programme, Operations, Community, L&D, etc)
- Some unique to the client's industry

Example for a leadership training co (10X Managers):
```
Community Manager
LeadersLab Lead
Sales Development Representative
Community Events Lead
Programme Co-ordinator
Community Mentorship Lead
Operations Director
Management Development Partner
Leadership Development Consultant
Programme Manager
Learning & Development Partner
Leadership Coach
Performance Partner
```

### 3. Quotes (optional)

"Do you want each signature to potentially include a quote — a one-liner from your brand or a punchy POV? If no, skip. If yes, give me 4–8 short quotes."

Push: not generic motivational stuff. Should be specific to the client's POV.

Bad: "Success is a journey not a destination"
Good (for 10X Managers): "People don't leave bad jobs. They leave bad managers."

If user wants none, set `quotes = []` and `include_quote_rate = 0`.

### 4. Opt-out lines

"Give me 12–18 different ways to softly invite someone to opt out at the end of the email. Vary the wording — some 'FYI', some 'P.S.', some 'BTW', some apologetic, some breezy."

Push: this is where most signatures look identical, so variation matters most here. If they give you 5, ask for 10 more.

Example variants:
```
FYI if you'd rather I didn't reach out, just say the word.
PS not the right fit? Just reply and I won't be in touch again.
P.S. if you'd like me to stop reaching out, just say so.
BTW if you'd rather I focus elsewhere, just let me know.
Note — if this isn't for you, a quick reply is all it takes.
Just so you know — if you'd prefer not to hear from me, just drop me a line.
```

### 5. Rates

These can usually stay at defaults. Only ask if the client has specific aesthetic preferences:

- "How often should signatures show pronouns (she/her, he/him)?" → default 30%
- "How often should signatures include a quote?" → default 43%

Do not ask whether they want the email address shown — it is not an option. If a client asks for it, explain the auto-linking problem and say no.

### 6. Show a preview

Before committing, generate 3–4 example signatures and show them to the user. Confirm they look on-brand.

You can use this Python to preview (after writing the row, query it back, then run):

```python
import sys
sys.path.insert(0, '/home/admin/ColdEmailInfra/scripts')
from lib.client_context import load_client_context_by_slug
from api.jobs import _generate_signature_from_formula

ctx = load_client_context_by_slug('acme-co')
for email in ['alex.smith@acme-co.com', 'jordan.lee@acme-co.com', 'taylor.rivera@acme-co.com']:
    first, last = email.split('@')[0].split('.')
    sig = _generate_signature_from_formula(first.title(), last.title(), email, ctx.default_signature)
    print(f'--- {email} ---')
    print(sig)
    print()
```

(Or just paste a few sample signatures back to the user yourself — pick 3 plausible emails and walk through what would render.)

### 7. Workspace-specific overrides

If the client has multiple Bison workspaces with different brand voices (rare — most don't), ask if any workspace needs its own formula. ReachOS is an example: it uses a plaintext minimal style under the same client.

To create an override:

```sql
INSERT INTO signature_formulas (
  client_id, client_bison_workspace_id, style, company_names, titles
)
SELECT
  c.id, w.id, 'plaintext',
  ARRAY['ReachOS'], ARRAY['ReachOS']
FROM clients c, client_bison_workspaces w
WHERE c.slug = 'acme-co' AND w.client_id = c.id AND w.workspace_name = 'Brand X';
```

The unique-index on `(client_bison_workspace_id)` enforces one override per workspace.

## Validation

After writing the row, double check:
- `array_length(titles, 1) BETWEEN 10 AND 20`
- `array_length(optouts, 1) BETWEEN 12 AND 18`
- `array_length(company_names, 1) BETWEEN 3 AND 6`
- All rates between 0 and 1
- If `style='plaintext'` then the quote rate should be 0 (plaintext sigs ignore it)
- No pool value contains an email address, a URL, or a bare `.com` — the address ban is enforced in code, but a URL smuggled into a quote or opt-out line is not

```sql
SELECT style,
  array_length(company_names, 1) AS companies,
  array_length(titles, 1) AS titles,
  array_length(quotes, 1) AS quotes,
  array_length(optouts, 1) AS optouts,
  -- must return 0 rows' worth of offenders:
  (SELECT count(*) FROM unnest(sf.quotes || sf.optouts || sf.titles || sf.company_names) v
     WHERE v ~* '[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}' OR v ~* 'https?://') AS banned_tokens
FROM signature_formulas sf
JOIN clients c ON c.id = sf.client_id
WHERE c.slug = 'acme-co';
```
