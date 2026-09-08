# Security policy

Never commit or publish camera addresses, NVR accounts, RTSP URLs with credentials,
cloud API keys, SMTP authorisation codes, bridge tokens, databases, evidence media,
model weights, or production `.env` files.

If a secret is exposed, revoke or rotate it immediately, remove it from the public
branch before release, and report the incident privately to the repository owner.
Use dedicated read-only NVR accounts and restrict bridge access to a private VPN or
controlled network. Do not expose the Hikvision bridge or MediaMTX Control API to
the public Internet.
