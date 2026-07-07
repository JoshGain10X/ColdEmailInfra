-- 2026-07-07: outreach landing page on sending-domain roots.
--
-- When client_settings.landing_page_url is set, new deploys create the apex
-- A record unproxied (grey cloud) and Caddy on the shard VPS terminates TLS
-- for https://<root> itself, reverse-proxying this origin URL. Every
-- non-asset path is rewritten to the landing path, so the client's full
-- site is never browsable on a sending domain.
--
-- NULL keeps the legacy behaviour: proxied apex + Cloudflare 301 redirect
-- rule to client_settings.redirect_url (ReachOS, Scouted stay on this).

ALTER TABLE public.client_settings
    ADD COLUMN IF NOT EXISTS landing_page_url text;

COMMENT ON COLUMN public.client_settings.landing_page_url IS
    'Absolute https URL of the landing page served on sending-domain apexes '
    '(e.g. https://hello.10xmanagers.com/outreach). NULL = legacy redirect behaviour.';

-- 10X Managers opts in.
UPDATE public.client_settings cs
SET landing_page_url = 'https://hello.10xmanagers.com/outreach'
FROM public.clients c
WHERE c.id = cs.client_id
  AND c.slug = '10x-managers';
