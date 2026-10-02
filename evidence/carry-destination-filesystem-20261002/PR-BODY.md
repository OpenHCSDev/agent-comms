Fix atomic carry staging across filesystems

Actual #514 publication failed EXDEV: HOME recovery receipts were also used as atomic replacement staging for the /var/tmp original goal ledger. Original DB remained unchanged.

Move candidate staging into the existing held-file resource beside the destination. All goal and native carry callers share that physical lifetime; retain preimages, original identities, sidecar UNKNOWN protocol and explicit recovery. No public mutation or replay. Private cross-filesystem validation comes after the source batch.
