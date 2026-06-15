import json
from pathlib import Path
from typing import Any


SHARDS_DIR = Path(__file__).resolve().parents[2] / "shards"


class ShardState:
    def __init__(self, domain: str):
        self.domain = domain
        self.path = SHARDS_DIR / f"{domain}.json"
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
        else:
            self.data = {
                "domain": domain,
                "steps": {},
                "subdomains": [],
                "mailboxes": [],
                "vps": {},
                "dkim": {},
                "dns_record_ids": {},
                "dmarc_inbox": "",
            }

    def save(self) -> None:
        SHARDS_DIR.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True))

    def is_step_done(self, name: str) -> bool:
        return bool(self.data["steps"].get(name))

    def mark_step_done(self, name: str) -> None:
        self.data["steps"][name] = True
        self.save()

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()
