# Data Security Policy

Customer data is encrypted at rest using AES-256 and in transit using TLS
1.2 or higher. Encryption keys are held in a managed key store, rotated
every 90 days, and never leave the hosting region.

Backups run nightly and are retained for 35 days, after which they are
deleted automatically. Backup archives are encrypted with the same standard
as live data and restored quarterly as a test of the recovery procedure.

When an account is closed, all customer content is purged from live systems
within 30 days. Access to production systems requires hardware-key
two-factor authentication and is reviewed monthly.
