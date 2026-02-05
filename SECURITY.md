# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## Reporting a Vulnerability

If you discover a security vulnerability in qsync, please report it responsibly:

1. **Do not** open a public GitHub Issue for security vulnerabilities
2. Email the maintainer directly at the address listed in the repository
3. Include:
   - A description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Any suggested fixes (optional)

You can expect:
- Acknowledgment within 48 hours
- Regular updates on progress
- Credit in the security advisory (unless you prefer anonymity)

## Security Best Practices

### API Token Security

- **Never commit `.env` files** containing API tokens to version control
- Add `.env` to your `.gitignore`
- Rotate API tokens immediately if accidentally exposed
- Use workspace-level `.env` files, not user-level shell profiles for production

### Keychain Storage (Recommended)

For improved security, store your Qualtrics API token in the system keychain instead of `.env` files:

```bash
# Install keyring support
pipx inject qsync keyring  # or: pip install keyring

# Store token securely
python -m keyring set "qualtrics-token" "token"
```

Then keep your `.env` minimal (base URL only) and run `qsync doctor` to confirm `qualtrics_token_source: keyring`.

### Environment Isolation

- Use separate API tokens for development and production
- Consider using Qualtrics sandbox environments for testing
- Limit API token permissions to only what's needed

### Audit Logging

qsync logs all push operations to `logs/qualtrics_push.log` (JSONL format). Review these logs periodically for unexpected activity.

## Known Security Considerations

### Survey Data

- `qsync` caches survey definitions locally in `surveys/` directory
- These JSON files may contain survey logic and question text
- Ensure appropriate access controls on your workspace directory

### Library Messages

- Shared library messages affect multiple surveys
- Use `qsync eos clone-shared` to create survey-specific copies before editing
- Review the impact before pushing changes to shared resources

### Response Data

- By default, `qsync` does not download response data
- If you export responses, ensure compliance with your data governance policies

## Security Updates

Security updates will be released as patch versions (e.g., 0.1.1) and announced in:
- GitHub Releases
- CHANGELOG.md

Subscribe to GitHub releases to receive notifications.
