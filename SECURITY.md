# Security

Report vulnerabilities privately to the repository owner before disclosure.
Never attach credentials, private conversations, `daily/`, `wiki/`, `.cmc/`,
`ACCESS.md`, `engine.json`, or machine paths.

Models receive redacted, bounded text and no write tools. Code validates proposals
and writes only managed wiki paths. Redaction catches common credential assignments;
it cannot detect every secret. Controlled purge covers local managed layers. Data
already sent to a provider, Git history, and external backups need separate deletion.
