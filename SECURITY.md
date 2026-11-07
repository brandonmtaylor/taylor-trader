# Security

TaylorTrader places orders in real brokerage accounts, so security reports matter.

**To report a problem,** use GitHub's private reporting: the Security tab, then "Report a
vulnerability". Please don't open a public issue. Include what you found and how to reproduce it.

**Worth reporting:** anything that exposes API keys or passwords, gets past the safety limits, lets
another device or website control the bot, or lets the MCP server go live or read keys.

**What runs with full trust.** Anything that can reach 127.0.0.1:8787 on your Mac is treated as you.
That includes other programs and other accounts on a shared Mac. It still can't read your keys: the
dashboard never shows them, and changing where a key or password is sent (the AI endpoint, the email
server, the ntfy server) deletes the stored one, so it has to be entered again. Don't expose the
dashboard through tunnels or proxies; to use it from your phone, run `taylortrader wifi on`.

**Before you share logs or a config,** check them for keys. Keys live in your Keychain or `.env`, and
neither belongs in an issue. If a key has leaked, regenerate it in Alpaca first.

This is a small project with no bug bounty. Only the latest version gets fixes.
