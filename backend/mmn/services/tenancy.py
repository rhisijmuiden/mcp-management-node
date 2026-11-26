"""Customer isolation.

Everything in Stackpilot hangs under a customer (stored in the ``environments`` table; "Home" is
just a customer). A request or an MCP call can be limited to a set of customers: an MCP token that
is tied to one or more customers, or the customer picked in the switcher of the web interface.

The limit lives in a context variable. While it is set, every ORM query (SELECT, UPDATE, DELETE,
``Session.get`` and relationship loads) only sees rows that belong to those customers, through
``with_loader_criteria``. So a token for customer A can never read, change or even name a node,
server, UniFi connection, job, alert or deployment of customer B, whatever tool it calls.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterable, Iterator
from contextlib import contextmanager

from sqlalchemy import event, or_, select
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria

_allowed: contextvars.ContextVar[frozenset[str] | None] = contextvars.ContextVar("mmn_customers", default=None)

HEADER = "x-stackpilot-customer"


def allowed() -> frozenset[str] | None:
    """The customer ids the current call may see, or None for all customers."""
    return _allowed.get()


def set_allowed(ids: Iterable[str] | None) -> contextvars.Token:
    return _allowed.set(frozenset(ids) if ids is not None else None)


def reset(token: contextvars.Token) -> None:
    _allowed.reset(token)


@contextmanager
def limited_to(ids: Iterable[str] | None) -> Iterator[None]:
    tok = set_allowed(ids)
    try:
        yield
    finally:
        _allowed.reset(tok)


@contextmanager
def unrestricted() -> Iterator[None]:
    """For system work that must see everything (never around user input)."""
    tok = _allowed.set(None)
    try:
        yield
    finally:
        _allowed.reset(tok)


def is_allowed(customer_id: str | None) -> bool:
    a = _allowed.get()
    return a is None or (customer_id is not None and customer_id in a)


def _criteria(ids: list[str]):
    from mmn.models import (
        Alert,
        BackupCoverage,
        CertEndpoint,
        Certificate,
        Change,
        ConfigBackup,
        CostItem,
        CostRate,
        CostStatement,
        CostUsage,
        CustomerReport,
        Deployment,
        DeploymentRun,
        Document,
        DocumentAttachment,
        DocumentRevision,
        Environment,
        FirewallSource,
        FirewallVersion,
        M365SignIn,
        M365Tenant,
        MetricSample,
        Node,
        NodeJob,
        PairingCode,
        PortalUser,
        Repo,
        RepoAccess,
        Runbook,
        RunbookRun,
        SdnDesign,
        Ticket,
        TicketMessage,
        Transfer,
        UnifiConnection,
        UnifiEvent,
        UptimeCheck,
        UptimeIncident,
        UptimeResult,
        VaultAccess,
        VaultGrant,
        VaultItem,
        VaultItemHistory,
        VpnTunnel,
    )

    node_ids = select(Node.id).where(Node.environment_id.in_(ids)).scalar_subquery()
    conn_ids = select(UnifiConnection.id).where(UnifiConnection.environment_id.in_(ids)).scalar_subquery()
    dep_ids = (select(Deployment.id).where(Deployment.node_id.in_(
        select(Node.id).where(Node.environment_id.in_(ids)))).scalar_subquery())
    linked_repos = select(RepoAccess.repo_id).where(RepoAccess.customer_id.in_(ids)).scalar_subquery()
    check_ids = select(UptimeCheck.id).where(UptimeCheck.customer_id.in_(ids)).scalar_subquery()
    ticket_ids = select(Ticket.id).where(Ticket.customer_id.in_(ids)).scalar_subquery()
    return [
        with_loader_criteria(Document, Document.customer_id.in_(ids), include_aliases=True),
        with_loader_criteria(Change, Change.customer_id.in_(ids), include_aliases=True),
        with_loader_criteria(CostItem, CostItem.customer_id.in_(ids), include_aliases=True),
        with_loader_criteria(CostRate, or_(CostRate.customer_id.is_(None), CostRate.customer_id.in_(ids)),
                             include_aliases=True),
        with_loader_criteria(CostUsage, CostUsage.customer_id.in_(ids), include_aliases=True),
        with_loader_criteria(CostStatement, CostStatement.customer_id.in_(ids), include_aliases=True),
        with_loader_criteria(CertEndpoint, CertEndpoint.customer_id.in_(ids), include_aliases=True),
        with_loader_criteria(Certificate, Certificate.customer_id.in_(ids), include_aliases=True),
        with_loader_criteria(FirewallSource, FirewallSource.customer_id.in_(ids), include_aliases=True),
        with_loader_criteria(FirewallVersion, FirewallVersion.customer_id.in_(ids), include_aliases=True),
        with_loader_criteria(VpnTunnel, VpnTunnel.customer_id.in_(ids), include_aliases=True),
        with_loader_criteria(M365Tenant, M365Tenant.customer_id.in_(ids), include_aliases=True),
        with_loader_criteria(M365SignIn, M365SignIn.customer_id.in_(ids), include_aliases=True),
        with_loader_criteria(DocumentRevision, DocumentRevision.customer_id.in_(ids), include_aliases=True),
        with_loader_criteria(DocumentAttachment, DocumentAttachment.customer_id.in_(ids), include_aliases=True),
        with_loader_criteria(VaultItem, VaultItem.customer_id.in_(ids), include_aliases=True),
        with_loader_criteria(VaultItemHistory, VaultItemHistory.customer_id.in_(ids), include_aliases=True),
        with_loader_criteria(VaultGrant, VaultGrant.customer_id.in_(ids), include_aliases=True),
        with_loader_criteria(VaultAccess, VaultAccess.customer_id.in_(ids), include_aliases=True),
        with_loader_criteria(Ticket, Ticket.customer_id.in_(ids), include_aliases=True),
        with_loader_criteria(TicketMessage, TicketMessage.ticket_id.in_(ticket_ids), include_aliases=True),
        with_loader_criteria(PortalUser, PortalUser.customer_id.in_(ids), include_aliases=True),
        with_loader_criteria(UptimeCheck, UptimeCheck.customer_id.in_(ids), include_aliases=True),
        with_loader_criteria(UptimeIncident, UptimeIncident.customer_id.in_(ids), include_aliases=True),
        with_loader_criteria(UptimeResult, UptimeResult.check_id.in_(check_ids), include_aliases=True),
        with_loader_criteria(Environment, Environment.id.in_(ids), include_aliases=True),
        with_loader_criteria(Node, Node.environment_id.in_(ids), include_aliases=True),
        with_loader_criteria(BackupCoverage, BackupCoverage.environment_id.in_(ids), include_aliases=True),
        with_loader_criteria(CustomerReport, CustomerReport.environment_id.in_(ids), include_aliases=True),
        with_loader_criteria(PairingCode, PairingCode.environment_id.in_(ids), include_aliases=True),
        with_loader_criteria(UnifiConnection, UnifiConnection.environment_id.in_(ids), include_aliases=True),
        with_loader_criteria(UnifiEvent, UnifiEvent.connection_id.in_(conn_ids), include_aliases=True),
        with_loader_criteria(NodeJob, NodeJob.node_id.in_(node_ids), include_aliases=True),
        with_loader_criteria(MetricSample, MetricSample.node_id.in_(node_ids), include_aliases=True),
        with_loader_criteria(Alert, Alert.node_id.in_(node_ids), include_aliases=True),
        with_loader_criteria(SdnDesign, SdnDesign.node_id.in_(node_ids), include_aliases=True),
        with_loader_criteria(ConfigBackup, ConfigBackup.node_id.in_(node_ids), include_aliases=True),
        with_loader_criteria(Deployment, Deployment.node_id.in_(node_ids), include_aliases=True),
        # The software library: a repository is visible to the customers it belongs to or is linked to.
        with_loader_criteria(Repo, Repo.owner_customer_id.in_(ids) | Repo.id.in_(linked_repos), include_aliases=True),
        with_loader_criteria(RepoAccess, RepoAccess.customer_id.in_(ids), include_aliases=True),
        with_loader_criteria(DeploymentRun, DeploymentRun.deployment_id.in_(dep_ids), include_aliases=True),
        # Global runbooks (no customer) are visible to everyone; runs only to their customer.
        with_loader_criteria(Runbook, Runbook.customer_id.in_(ids) | Runbook.customer_id.is_(None),
                             include_aliases=True),
        with_loader_criteria(RunbookRun, RunbookRun.customer_id.in_(ids), include_aliases=True),
        with_loader_criteria(Transfer, Transfer.source_node_id.in_(node_ids) & Transfer.target_node_id.in_(node_ids),
                             include_aliases=True),
    ]


@event.listens_for(Session, "do_orm_execute")
def _limit_to_customers(state: ORMExecuteState) -> None:
    ids = _allowed.get()
    if ids is None:
        return
    if not (state.is_select or state.is_update or state.is_delete):
        return
    state.statement = state.statement.options(*_criteria(sorted(ids)))
