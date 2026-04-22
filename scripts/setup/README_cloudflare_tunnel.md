# Cloudflare Tunnel — permanent URL for the Arbiter dashboard

A named Cloudflare Tunnel gives the Arbiter API a URL that (a) survives
reboots, (b) never changes, (c) does not require port forwarding, and
(d) is reachable from anywhere without Tailscale.

You need a domain you own on Cloudflare (free plan is fine). A
`*.trycloudflare.com` quick tunnel works for smoke tests but rotates on
every restart, so it is not suitable as a permanent URL.

## One-time setup

```bash
# 1. Install cloudflared (if not already present)
brew install cloudflared

# 2. Log in and pick the Cloudflare zone (domain) to use.
#    This opens a browser window for approval and writes
#    ~/.cloudflared/cert.pem.
cloudflared tunnel login

# 3. Create the tunnel, write config.yml, route DNS, and
#    install a LaunchAgent that auto-starts the tunnel.
./scripts/setup/setup_cloudflare_tunnel.sh arbiter.YOURDOMAIN.com
```

The script is idempotent — safe to re-run after changing hostname or
config. It:

- Creates (or reuses) a named tunnel called `arbiter`
- Writes `~/.cloudflared/config.yml` with an ingress rule pointing at
  `http://127.0.0.1:8080` (the Arbiter API port)
- Runs `cloudflared tunnel route dns` to bind the hostname to the
  tunnel's UUID
- Installs a LaunchAgent at
  `~/Library/LaunchAgents/com.arbiter.cloudflared.plist` with
  `RunAtLoad` + `KeepAlive` so the tunnel starts on login and
  restarts if it crashes
- `launchctl load`s the agent immediately

## Verifying

```bash
launchctl list | grep com.arbiter.cloudflared
curl -sI https://arbiter.YOURDOMAIN.com/ | head -n1
tail -f ~/Library/Logs/arbiter-cloudflared.log
```

The Arbiter API must be bound to `127.0.0.1:8080` (or `0.0.0.0:8080`)
for the tunnel to have something to connect to. If you run the API on
a different port, edit `LOCAL_ORIGIN` in the setup script.

## Updating the hostname

```bash
./scripts/setup/setup_cloudflare_tunnel.sh new.YOURDOMAIN.com
```

## Tearing it down

```bash
launchctl unload ~/Library/LaunchAgents/com.arbiter.cloudflared.plist
rm ~/Library/LaunchAgents/com.arbiter.cloudflared.plist
cloudflared tunnel delete arbiter
```
