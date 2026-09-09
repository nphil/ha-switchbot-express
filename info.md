# SwitchBot Express

Replaces the core SwitchBot Bluetooth integration for connectable devices with
one that lets you choose how long the connection stays open, so a curtain does
not spend one to six seconds connecting before every single command.

Core drops the link 8.5 seconds after the last command, so any command that is
not part of a burst pays a fresh proxy connect. The protocol handling here is
still `PySwitchbot`; only the connection policy is different.

- **Linger** after a command you issued (15 s by default), so follow-up nudges
  are instant
- **Prewarm** action: connect ahead of a scheduled or presence-driven command,
  which is how you get an instant first command without paying for it all day
- **Hold** the connection permanently — opt-in, for solar or mains fed units
- **Battery aware**: linger and hold suspend themselves on a flat battery, with
  a repair that says so
- **Fail fast** on a bad proxy path, so the retry roams onto a better proxy
- A **Connection** sensor: which proxy holds the link, drops in the last hour,
  reconnect attempt in flight

Entity IDs match core's, so migrating is remove, add, and keep the same name.
Supports Curtain and Curtain 3.
