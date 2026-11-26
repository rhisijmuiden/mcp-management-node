"""Work performed in the monthly report: tasks, procedures, automatic recovery, changes and requests,
in plain language with duration and outcome, in English and Dutch, in HTML and PDF."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mmn.services import worklog


def test_duration_text():
    assert worklog.duration_text(None, "en") == "–"
    assert worklog.duration_text(0.4, "nl") == "< 1 min"
    assert worklog.duration_text(12, "en") == "12 min"
    assert worklog.duration_text(60, "nl") == "1 uur"
    assert worklog.duration_text(65, "en") == "1 h 5 min"
    assert worklog.duration_text(3 * 1440, "nl") == "3 dag(en)"


async def test_work_section(admin_client):
    from sqlalchemy import delete

    from mmn.db import sessionmaker
    from mmn.models import Change, Environment, Node, NodeJob, RemediationAttempt, RunbookRun, Ticket
    from mmn.services import reports

    now = datetime.now(UTC)
    t0 = now - timedelta(hours=3)
    async with sessionmaker()() as db:
        tz = await reports.timezone(db)
        period = reports.current_period(now, tz)
        start, _end = reports.month_bounds(period, tz)
        t0 = max(t0, start + timedelta(minutes=5))
        e = Environment(name="Worklog BV", kind="customer")
        db.add(e)
        await db.flush()
        n = Node(environment_id=e.id, name="srv-work")
        db.add(n)
        await db.flush()
        at = lambda m: t0 + timedelta(minutes=m)  # noqa: E731
        run_rb = RunbookRun(runbook_name="Monthly patch round", customer_id=e.id, status="succeeded",
                            state=[{"id": "s1", "status": "succeeded", "job_id": "job-in-runbook"}],
                            created_at=at(0), finished_at=at(25), trigger="manual")
        run_fix = RunbookRun(runbook_name="Restart service", customer_id=e.id, status="succeeded", state=[],
                             created_at=at(1), finished_at=at(2), trigger="remediation")
        db.add_all([run_rb, run_fix])
        await db.flush()
        db.add_all([
            # Two update runs on the same day merge into one line; the runbook's own job is left out.
            NodeJob(node_id=n.id, kind="updates.apply", status="success", source="ui", requested_by="reinder",
                    params={}, result={"upgraded": 7}, output="", started_at=at(5), finished_at=at(17)),
            NodeJob(node_id=n.id, kind="updates.apply", status="success", source="mcp", params={},
                    result={"upgraded": 3}, output="", started_at=at(30), finished_at=at(33)),
            NodeJob(id="job-in-runbook", node_id=n.id, kind="guest.reboot", status="success", source="mcp",
                    params={"name": "web01", "vmid": 101}, result={}, output="", started_at=at(6), finished_at=at(8)),
            NodeJob(node_id=n.id, kind="guest.snapshot", status="failed", source="system",
                    params={"name": "db01", "vmid": 102}, result={}, output="", started_at=at(40), finished_at=at(41)),
            NodeJob(node_id=n.id, kind="file.fetch", status="success", source="mcp", params={}, result={}, output="",
                    started_at=at(42), finished_at=at(42)),
            RemediationAttempt(rule_name="Restart a stopped service", customer_id=e.id, source="alert",
                               source_key="alert:x", target="srv-work", problem="The print service had stopped",
                               status="succeeded", run_id=run_fix.id, created_at=at(1), finished_at=at(3)),
            Change(customer_id=e.id, title="Replace the firewall", kind="normal", impact="low", status="completed",
                   starts_at=at(60), ends_at=at(120), started_at=at(62), finished_at=at(107), node_ids=[n.id],
                   outcome_note="New firewall in use, no interruption."),
            Ticket(customer_id=e.id, subject="Printer does not work", category="incident", priority="normal",
                   status="resolved", created_at=at(-30), resolved_at=at(10), node_ids=[]),
        ])
        eid, nid = e.id, n.id
        await db.commit()
        try:
            e = await db.get(Environment, eid)
            await db.refresh(e, ["nodes"])
            d = await reports.build(db, e, period, "nl", live=False)
            w = d["work"]
            titles = [x["title"] for x in w["items"]]
            assert "Updates geïnstalleerd (2×)" in titles
            upd = next(x for x in w["items"] if x["title"].startswith("Updates"))
            assert upd["detail"] == "10 update(s)" and upd["took"] == "15 min" and upd["result_label"] == "Gereed"
            assert upd["by_label"] == "Beheerder"
            assert 'Procedure "Monthly patch round" uitgevoerd' in titles
            assert not any("web01" in t for t in titles)  # part of the procedure
            assert not any("Restart service" in t for t in titles)  # shown as the automatic repair
            fix = next(x for x in w["items"] if x["category"] == "recovery")
            assert fix["title"] == "Automatisch opgelost: The print service had stopped" and fix["result"] == "fixed"
            chg = next(x for x in w["items"] if x["category"] == "change")
            assert chg["took"] == "45 min" and chg["detail"].startswith("New firewall") and chg["where"] == "srv-work"
            req = next(x for x in w["items"] if x["category"] == "request")
            assert req["title"].startswith("Uw verzoek #") and req["took"] == "40 min"
            snap = next(x for x in w["items"] if "db01" in x["title"])
            assert snap["result_label"] == "Niet gelukt" and snap["by_label"] == "Automatisch"
            assert not any(x["title"] == "file.fetch" for x in w["items"])
            assert w["fixed"] == 2 and w["count"] == 7
            html = reports.render_html(d, tz)
            assert "Uitgevoerde werkzaamheden" in html and "Replace the firewall" in html
            assert w["line"] == "7 werkzaamheden uitgevoerd, in totaal 1 uur 28 min; 2 problemen opgelost."
            assert reports.render_pdf(reports._jsonable(d), tz).startswith(b"%PDF")
            en = await reports.build(db, e, period, "en", live=False)
            assert "Installed updates (2×)" in [x["title"] for x in en["work"]["items"]]
            assert "Work performed" in reports.render_html(en, tz)
        finally:
            await db.rollback()
            await db.execute(delete(NodeJob).where(NodeJob.node_id == nid))
            for m in (RemediationAttempt, RunbookRun):
                await db.execute(delete(m).where(m.customer_id == eid))
            await db.execute(delete(Node).where(Node.environment_id == eid))
            await db.execute(delete(Environment).where(Environment.id == eid))
            await db.commit()
