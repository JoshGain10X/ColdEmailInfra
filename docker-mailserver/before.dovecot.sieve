# Global Dovecot Sieve filter applied to every inbound message before per-user
# rules. Routes Instantly peer-warmup chatter into the Warmup/ folder so it
# never reaches INBOX (where Bison's IMAP poller would mistake it for a real
# reply and inflate reply-rate stats).
#
# The warmup tag string is set in instantly_admin.py (WARMUP_FILTER_TAG) and
# Instantly puts it in the Subject of every warmup peer mail. Re: replies
# carry the same Subject prefix, so this single rule catches both directions.
#
# Compiled into /tmp/docker-mailserver/before.dovecot.sieve by docker-mailserver
# at container start. Restart the container after editing.
require ["fileinto", "mailbox"];

if header :contains "Subject" "sointerested" {
  fileinto :create "Warmup";
  stop;
}
