# Legacy one-off scripts

Hardcoded to the 10X Managers single-tenant setup. Preserved for git history
only — NOT called by the v2 multi-tenant API. The deploy/verify/destroy/
load-bison flow lives in `api/jobs.py` and uses `lib/client_context.py` to
resolve per-client credentials and settings.

If you need behaviour from one of these scripts in v2, port it into a proper
client-aware function in `api/jobs.py` or `scripts/lib/` rather than calling
the script directly.

## Files

- `backfill_signatures.py` — one-time signature backfill for 10X mailboxes
- `backfill_sig_leaderslab.py` — same for the LeadersLab workspace
- `backfill_csvs.py` — re-upload existing shard CSVs to Supabase Storage
- `fix_ssl_certs.py` — repair Let's Encrypt certs on existing 10X shards
- `scripts/fix_subdomain_redirects.py` — fix CF redirect rules on 4 named 10X domains
- `scripts/deploy_shard.py` — the original CLI deploy entry point; superseded by `api/main.py` `/api/deploy`
