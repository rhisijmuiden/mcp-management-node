# Monitoring, uptime and SLA

## Monitoring

**Maintenance → Monitoring & uptime → Monitoring** keeps one sample per machine per minute for 30 days: CPU, memory
and disk, with trends from 1 hour to 30 days, storage use and the health and wear of physical disks.

**Alerts** open for offline machines, high CPU, memory, disk or storage use, failing disks, UniFi problems, backups,
certificates, Microsoft 365 and more. They are mailed when they open and when they close. Who gets which mail is set
under **Settings → Notifications**.

## Uptime and SLA

**Monitoring & uptime → Uptime & SLA** checks your customers' services from the outside, and adds them up to the SLA you
actually delivered.

### Kinds of checks

| Check | What it tests |
| --- | --- |
| **Website** | HTTP(S) status, a keyword that must (not) be on the page, response time, the certificate and its expiry |
| **TCP port** | A port answers, for example a mail server, RDP gateway or database |
| **Ping** | The host answers |
| **DNS** | A record (A, AAAA, CNAME, MX, TXT, …) exists, optionally with the expected value |
| **VPN** | The portal of an SSL VPN answers, or a host behind a tunnel answers ping |

### Add a check

1. Click **Add check**, choose the customer and the kind.
2. Fill in the address and what counts as good.
3. Choose **where from**: from Stackpilot (the outside view) or from the agent of a machine at the customer (for
   internal addresses and tunnels).
4. Set the interval (30 seconds to an hour), *down after* N failures in a row, and optionally *slow above*.
5. **Test** runs it once without saving. Then save.

A check that is down, slow or has an expiring certificate becomes an alert and is mailed. The menu shows how many
checks are down. Link the check to the machine its service runs on, and [automatic recovery](../automation/recovery.md)
can restart the service.

### The SLA

The tab **SLA** shows per month and per customer the measured availability, the target, whether it was met, and the
downtime against the allowed downtime. The target comes from the customer's SLA setting. Downtime **inside the
maintenance window does not count** (unless you switch that off). The monthly report has a section on it.
