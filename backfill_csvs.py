#!/usr/bin/env python3
"""One-time backfill: upload existing _bison.csv files to Supabase Storage."""
import os
from pathlib import Path
from dotenv import load_dotenv
from supabase import create_client

load_dotenv(override=True)

SHARDS_DIR = Path(__file__).resolve().parent / "shards"
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

csv_files = sorted(SHARDS_DIR.glob("*_bison.csv"))
print(f"Found {len(csv_files)} CSV files to upload")

for csv_path in csv_files:
    domain = csv_path.name.replace("_bison.csv", "")
    storage_path = f"{domain}.csv"

    try:
        with open(csv_path, "rb") as fh:
            sb.storage.from_("shard-csvs").upload(
                storage_path, fh.read(),
                file_options={"content-type": "text/csv", "upsert": "true"},
            )
        existing = sb.table("infra_shards").select("id").eq("domain", domain).execute()
        if existing.data:
            sb.table("infra_shards").update(
                {"csv_storage_path": storage_path}
            ).eq("domain", domain).execute()
            print(f"  OK  {domain} -> {storage_path}")
        else:
            print(f"  WARN {domain}: uploaded CSV but no infra_shards row found")
    except Exception as e:
        print(f"  FAIL {domain}: {e}")

print("Done.")
