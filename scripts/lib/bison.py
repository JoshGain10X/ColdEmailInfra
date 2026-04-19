import csv
from pathlib import Path


BISON_HEADERS = [
    "Name",
    "Email",
    "Password",
    "IMAP Server",
    "IMAP Port",
    "SMTP Server",
    "SMTP Port",
    "Daily Limit",
    "SMTP Secure",
    "IMAP Secure",
]


def export(mailboxes: list[dict], mail_hostname: str, out_path: Path, daily_limit: int = 10) -> None:
    """Write mailboxes to a Bison-compatible CSV.

    mail_hostname is the root mail host (e.g. mail.example.co.uk) used for both SMTP and IMAP.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(BISON_HEADERS)
        for mb in mailboxes:
            writer.writerow([
                f"{mb['first_name']} {mb['last_name']}",
                mb["email"],
                mb["password"],
                mail_hostname,
                993,
                mail_hostname,
                465,
                daily_limit,
                "SSL",
                "SSL",
            ])
