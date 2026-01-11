# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it responsibly:

1. **Do not** open a public GitHub issue for security vulnerabilities
2. Email the maintainer directly or use GitHub's private vulnerability reporting feature
3. Include detailed steps to reproduce the vulnerability
4. Allow reasonable time for a fix before public disclosure

## Security Considerations

### Data Storage

- Memories are stored in a local SQLite database (`~/.amplifier/memories.db` by default)
- The database is **not encrypted** - ensure appropriate filesystem permissions
- Consider the sensitivity of data being stored in memories

### Access Control

- This module does not implement authentication or authorization
- Access is controlled by filesystem permissions on the SQLite database
- In multi-user environments, ensure proper file permissions

### Data Retention

- Memories persist until explicitly deleted or compacted
- Use the `compact` tool or `delete_memory` to remove sensitive information
- The `export_memories` tool exports all data - handle exports securely

## Best Practices

1. **Filesystem permissions**: Ensure `~/.amplifier/` has appropriate permissions (700 recommended)
2. **Sensitive data**: Avoid storing credentials, API keys, or PII in memories
3. **Backups**: Secure any database backups with encryption
4. **Shared systems**: Use per-user database paths in shared environments
