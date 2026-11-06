# Security

TaylorTrader places orders in real brokerage accounts, so security reports matter.

**To report a problem,** use GitHub's private reporting: the Security tab, then "Report a
vulnerability". Please don't open a public issue. Include what you found and how to reproduce it.

**Worth reporting:** anything that exposes API keys or passwords, gets past the safety limits, lets
another device or website control the bot, or lets the MCP server go live or read keys.

**Before you share logs or a config,** check them for keys. Keys live in your Keychain or `.env`, and
neither belongs in an issue. If a key has leaked, regenerate it in Alpaca first.

This is a small project with no bug bounty. Only the latest version gets fixes.
