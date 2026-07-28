"""PostgreSQL fact repository and fencing write boundary."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
import hashlib
from pathlib import Path
import re
from typing import Any, Iterator, LiteralString, cast

from psycopg import Connection, Cursor, connect, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from issue_control.contracts import (
    ActorKind,
    IssueEvent,
    IssueSession,
    IssueState,
    RiskTier,
    issue_key as make_issue_key,
)
from issue_control.state_machine import transition_session as apply_transition


DEFAULT_CLUSTER_NAME = "hermes-issue-control"
DEFAULT_RENEWAL_INTERVAL = timedelta(seconds=10)
DEFAULT_TAKEOVER_AFTER = timedelta(seconds=60)
_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


class RepositoryError(RuntimeError):
    """Base error for durable repository failures."""


class RepositoryConflict(RepositoryError):
    """Persisted facts conflict with the requested immutable fact."""


class StaleFenceError(RepositoryError):
    """The caller is not the current unexpired PostgreSQL-fenced leader."""


class EventDisposition(StrEnum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class MutationContext:
    node_id: str
    lease_epoch: int
    run_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("run_id is required")


@dataclass(frozen=True, slots=True)
class LeadershipDecision:
    node_id: str
    is_leader: bool
    lease_epoch: int
    role: str
    leader_node: str | None
    renewed_at: datetime


@dataclass(frozen=True, slots=True)
class ObservationResult:
    disposition: EventDisposition
    session: IssueSession


@dataclass(frozen=True, slots=True)
class SessionMutation:
    kind: str
    issue_key: str
    session_id: str
    run_id: str
    lease_epoch: int
    context_version: int
    recorded_at: datetime


class PostgresIssueRepository:
    """Deep repository seam for facts, sessions, snapshots, and fencing.

    A fresh PostgreSQL transaction and connection is used per call. This keeps
    the adapter thread-safe for concurrent claims and makes restart behavior
    identical to a new process.
    """

    def __init__(
        self,
        dsn: str,
        *,
        schema: str = "hermes_issue_control",
        cluster_name: str = DEFAULT_CLUSTER_NAME,
        renewal_interval: timedelta = DEFAULT_RENEWAL_INTERVAL,
        takeover_after: timedelta = DEFAULT_TAKEOVER_AFTER,
    ) -> None:
        if not dsn:
            raise ValueError("PostgreSQL DSN is required")
        if not _SCHEMA_RE.fullmatch(schema):
            raise ValueError("schema must be a simple PostgreSQL identifier")
        if renewal_interval != DEFAULT_RENEWAL_INTERVAL:
            raise ValueError("Phase 1A requires a 10-second renewal interval")
        if takeover_after != DEFAULT_TAKEOVER_AFTER:
            raise ValueError("Phase 1A requires a 60-second takeover interval")
        self.dsn = dsn
        self.schema = schema
        self.cluster_name = cluster_name
        self.renewal_interval = renewal_interval
        self.takeover_after = takeover_after

    def _connection(self) -> Connection[dict[str, Any]]:
        return cast(
            Connection[dict[str, Any]],
            connect(self.dsn, row_factory=cast(Any, dict_row)),
        )

    @contextmanager
    def _transaction(
        self,
    ) -> Iterator[tuple[Connection[dict[str, Any]], Cursor[dict[str, Any]]]]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SET LOCAL search_path TO {}, public").format(
                        sql.Identifier(self.schema)
                    )
                )
                yield connection, cursor

    def migrate(self) -> None:
        migrations_dir = Path(__file__).with_name("migrations")
        migration_paths = sorted(migrations_dir.glob("*.sql"))
        if not migration_paths:
            raise RepositoryError("no issue-control migrations found")

        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"hermes-issue-control:migrate:{self.schema}",),
                )
                cursor.execute(
                    sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                        sql.Identifier(self.schema)
                    )
                )
                cursor.execute(
                    sql.SQL("SET LOCAL search_path TO {}, public").format(
                        sql.Identifier(self.schema)
                    )
                )
                for path in migration_paths:
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS schema_migrations (
                            version TEXT PRIMARY KEY,
                            applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
                        )
                        """
                    )
                    cursor.execute(
                        "SELECT 1 FROM schema_migrations WHERE version = %s",
                        (path.name,),
                    )
                    if cursor.fetchone():
                        continue
                    cursor.execute(
                        sql.SQL(
                            cast(
                                LiteralString,
                                path.read_text(encoding="utf-8"),
                            )
                        )
                    )
                    cursor.execute(
                        "INSERT INTO schema_migrations(version) VALUES (%s)",
                        (path.name,),
                    )

    def bootstrap_cluster(
        self,
        *,
        primary_node: str,
        standby_node: str,
        now: datetime,
    ) -> None:
        if primary_node == standby_node:
            raise ValueError("primary and standby nodes must differ")
        with self._transaction() as (_connection, cursor):
            cursor.execute(
                """
                INSERT INTO issue_control_leases(
                    cluster_name, primary_node, standby_node, renewed_at
                )
                VALUES (%s, %s, %s, clock_timestamp())
                ON CONFLICT (cluster_name) DO NOTHING
                """,
                (self.cluster_name, primary_node, standby_node),
            )
            cursor.execute(
                """
                SELECT primary_node, standby_node
                FROM issue_control_leases
                WHERE cluster_name = %s
                """,
                (self.cluster_name,),
            )
            row = cursor.fetchone()
            if not row or (
                row["primary_node"],
                row["standby_node"],
            ) != (primary_node, standby_node):
                raise RepositoryConflict("cluster node configuration is immutable")
            cursor.execute(
                """
                INSERT INTO issue_control_nodes(node_id, configured_role)
                VALUES (%s, 'primary'), (%s, 'standby')
                ON CONFLICT (node_id) DO NOTHING
                """,
                (primary_node, standby_node),
            )

    def try_acquire_leadership(
        self,
        *,
        node_id: str,
        now: datetime,
    ) -> LeadershipDecision:
        with self._transaction() as (_connection, cursor):
            cursor.execute(
                """
                SELECT *, clock_timestamp() AS database_now
                FROM issue_control_leases
                WHERE cluster_name = %s
                FOR UPDATE
                """,
                (self.cluster_name,),
            )
            row = cursor.fetchone()
            if not row:
                raise RepositoryError("cluster must be bootstrapped before leadership")
            if node_id not in (row["primary_node"], row["standby_node"]):
                raise RepositoryError(
                    f"node {node_id!r} is not configured for this cluster"
                )

            configured_role = "primary" if node_id == row["primary_node"] else "standby"
            database_now = row["database_now"]
            elapsed = database_now - row["renewed_at"]
            current_leader = row["leader_node"]
            epoch = row["lease_epoch"]

            may_take_initial = current_leader is None and configured_role == "primary"
            may_take_expired = (
                current_leader not in (None, node_id) and elapsed >= self.takeover_after
            )
            may_take_unowned = current_leader is None and elapsed >= self.takeover_after
            may_renew = current_leader == node_id and elapsed < self.takeover_after

            if may_take_initial or may_take_expired or may_take_unowned:
                epoch += 1
                cursor.execute(
                    """
                    UPDATE issue_control_leases
                    SET leader_node = %s,
                        lease_epoch = %s,
                        renewed_at = %s,
                        last_takeover_at = %s
                    WHERE cluster_name = %s
                    """,
                    (node_id, epoch, database_now, database_now, self.cluster_name),
                )
                current_leader = node_id
                renewed_at = database_now
                is_leader = True
            elif may_renew:
                cursor.execute(
                    """
                    UPDATE issue_control_leases
                    SET renewed_at = %s
                    WHERE cluster_name = %s
                    """,
                    (database_now, self.cluster_name),
                )
                renewed_at = database_now
                is_leader = True
            else:
                renewed_at = row["renewed_at"]
                is_leader = False

            cursor.execute(
                """
                UPDATE issue_control_nodes
                SET observed_epoch = %s, last_seen_at = %s
                WHERE node_id = %s
                """,
                (epoch, database_now, node_id),
            )
            return LeadershipDecision(
                node_id=node_id,
                is_leader=is_leader,
                lease_epoch=epoch,
                role="leader" if is_leader else "standby",
                leader_node=current_leader,
                renewed_at=renewed_at,
            )

    def report_node_status(
        self,
        *,
        node_id: str,
        ready: bool,
        observed_epoch: int,
        now: datetime,
        detail: dict[str, Any],
    ) -> None:
        with self._transaction() as (_connection, cursor):
            cursor.execute(
                """
                UPDATE issue_control_nodes
                SET ready = %s,
                    observed_epoch = %s,
                    last_seen_at = clock_timestamp(),
                    detail = %s
                WHERE node_id = %s
                """,
                (ready, observed_epoch, Jsonb(detail), node_id),
            )
            if cursor.rowcount != 1:
                raise RepositoryError(f"node {node_id!r} is not configured")

    def _assert_fence(
        self,
        cursor: Cursor[dict[str, Any]],
        context: MutationContext,
    ) -> None:
        cursor.execute(
            """
            SELECT leader_node, lease_epoch,
                   clock_timestamp() - renewed_at < %s AS lease_fresh
            FROM issue_control_leases
            WHERE cluster_name = %s
            FOR SHARE
            """,
            (self.takeover_after, self.cluster_name),
        )
        row = cursor.fetchone()
        if (
            not row
            or row["leader_node"] != context.node_id
            or row["lease_epoch"] != context.lease_epoch
            or not row["lease_fresh"]
        ):
            raise StaleFenceError(
                f"node {context.node_id!r} epoch {context.lease_epoch} is stale"
            )

    @staticmethod
    def _session_from_row(row: dict[str, Any]) -> IssueSession:
        return IssueSession(
            issue_key=row["issue_key"],
            session_id=row["session_id"],
            state=IssueState(row["state"]),
            context_version=row["context_version"],
            task_graph_ref=row["task_graph_ref"],
            active_run_id=row["active_run_id"],
            risk_tier=RiskTier(row["risk_tier"]),
            lease_epoch=row["lease_epoch"],
        )

    def _existing_observation(
        self,
        cursor: Cursor[dict[str, Any]],
        event: IssueEvent,
    ) -> ObservationResult | None:
        cursor.execute(
            "SELECT * FROM issue_events WHERE event_id = %s",
            (event.event_id,),
        )
        existing_event = cursor.fetchone()
        if not existing_event:
            return None
        _assert_same_event(existing_event, event)
        cursor.execute(
            "SELECT * FROM issue_sessions WHERE session_id = %s",
            (existing_event["session_id"],),
        )
        existing_session = _required_row(
            cursor.fetchone(),
            operation="read duplicate event session",
        )
        return ObservationResult(
            EventDisposition.DUPLICATE,
            self._session_from_row(existing_session),
        )

    def _closed_session_for_stale_event(
        self,
        cursor: Cursor[dict[str, Any]],
        event: IssueEvent,
    ) -> IssueSession | None:
        cursor.execute(
            """
            SELECT session_id
            FROM issue_sessions
            WHERE issue_key = %s AND ended_at IS NULL
            FOR UPDATE
            """,
            (event.issue_key,),
        )
        if cursor.fetchone():
            return None
        cursor.execute(
            """
            SELECT *
            FROM issue_sessions
            WHERE issue_key = %s AND ended_at IS NOT NULL
            ORDER BY last_github_version DESC, last_github_tiebreaker DESC
            LIMIT 1
            FOR UPDATE
            """,
            (event.issue_key,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        if (event.github_version, _event_tiebreaker(event)) > (
            row["last_github_version"],
            row["last_github_tiebreaker"],
        ):
            return None
        return self._session_from_row(row)

    @staticmethod
    def _insert_event(
        cursor: Cursor[dict[str, Any]],
        *,
        event: IssueEvent,
        session_id: str,
        context: MutationContext,
        now: datetime,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO issue_events(
                event_id, issue_key, github_version, event_type,
                actor_kind, occurred_at, sanitized_payload_ref,
                session_id, run_id, lease_epoch, recorded_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event.event_id,
                event.issue_key,
                event.github_version,
                event.event_type,
                event.actor_kind.value,
                event.occurred_at,
                event.sanitized_payload_ref,
                session_id,
                context.run_id,
                context.lease_epoch,
                now,
            ),
        )

    def _append_snapshot(
        self,
        cursor: Cursor[dict[str, Any]],
        *,
        session: IssueSession,
        kind: str,
        context: MutationContext,
        now: datetime,
        event_id: str | None = None,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO issue_session_snapshots(
                issue_key, session_id, state, context_version,
                task_graph_ref, active_run_id, risk_tier, mutation_kind,
                event_id, run_id, lease_epoch, recorded_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                session.issue_key,
                session.session_id,
                session.state.value,
                session.context_version,
                session.task_graph_ref,
                session.active_run_id,
                session.risk_tier.value,
                kind,
                event_id,
                context.run_id,
                context.lease_epoch,
                now,
            ),
        )

    def _claim_session(
        self,
        cursor: Cursor[dict[str, Any]],
        *,
        canonical_issue_key: str,
        candidate_session_id: str,
        risk_tier: RiskTier,
        context: MutationContext,
        now: datetime,
    ) -> tuple[IssueSession, bool]:
        cursor.execute(
            """
            SELECT *
            FROM issue_sessions
            WHERE issue_key = %s AND ended_at IS NULL
            FOR UPDATE
            """,
            (canonical_issue_key,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.execute(
                """
                SELECT count(*) AS lifecycle_count
                FROM issue_sessions
                WHERE issue_key = %s
                """,
                (canonical_issue_key,),
            )
            lifecycle_number = (
                _required_row(
                    cursor.fetchone(),
                    operation="count issue lifecycles",
                )["lifecycle_count"]
                + 1
            )
            lifecycle_session_id = (
                candidate_session_id
                if lifecycle_number == 1
                else f"{candidate_session_id}:{lifecycle_number}"
            )
            initial_context_version = _durable_context_version(
                state=IssueState.DISCOVERED,
                state_revision=0,
                github_version=-1,
                github_tiebreaker="",
                lease_epoch=context.lease_epoch,
            )
            cursor.execute(
                """
                INSERT INTO issue_sessions(
                    session_id, issue_key, state, context_version,
                    risk_tier, lease_epoch, started_at, updated_at
                )
                VALUES (%s, %s, 'discovered', %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                (
                    lifecycle_session_id,
                    canonical_issue_key,
                    initial_context_version,
                    RiskTier.UNKNOWN.value,
                    context.lease_epoch,
                    now,
                    now,
                ),
            )
            row = cursor.fetchone()
            if row:
                session = self._session_from_row(row)
                self._append_snapshot(
                    cursor,
                    session=session,
                    kind="claimed",
                    context=context,
                    now=now,
                )
                return session, True
            cursor.execute(
                """
                SELECT *
                FROM issue_sessions
                WHERE issue_key = %s AND ended_at IS NULL
                FOR UPDATE
                """,
                (canonical_issue_key,),
            )
            row = cursor.fetchone()
            if not row:
                raise RepositoryConflict("active-session claim lost without a winner")

        _row, session = self._adopt_locked_session_fence(
            cursor,
            row=row,
            context=context,
            now=now,
        )
        return session, False

    def _adopt_locked_session_fence(
        self,
        cursor: Cursor[dict[str, Any]],
        *,
        row: dict[str, Any],
        context: MutationContext,
        now: datetime,
    ) -> tuple[dict[str, Any], IssueSession]:
        session = self._session_from_row(row)
        if session.lease_epoch > context.lease_epoch:
            raise StaleFenceError("session is already owned by a newer fencing epoch")
        if session.lease_epoch == context.lease_epoch:
            return row, session
        adopted_context_version = _durable_context_version(
            state=session.state,
            state_revision=row["state_revision"],
            github_version=row["last_github_version"],
            github_tiebreaker=row["last_github_tiebreaker"],
            lease_epoch=context.lease_epoch,
        )
        cursor.execute(
            """
            UPDATE issue_sessions
            SET lease_epoch = %s,
                context_version = %s,
                updated_at = %s
            WHERE session_id = %s
            RETURNING *
            """,
            (
                context.lease_epoch,
                adopted_context_version,
                now,
                session.session_id,
            ),
        )
        adopted_row = _required_row(
            cursor.fetchone(),
            operation="adopt fencing epoch",
        )
        adopted = self._session_from_row(adopted_row)
        self._append_snapshot(
            cursor,
            session=adopted,
            kind="fence_adopted",
            context=context,
            now=now,
        )
        return adopted_row, adopted

    def claim_session(
        self,
        *,
        issue_key: str,
        candidate_session_id: str,
        risk_tier: RiskTier,
        context: MutationContext,
        now: datetime,
    ) -> tuple[IssueSession, bool]:
        canonical_issue_key = _canonical_issue_key(issue_key)
        with self._transaction() as (_connection, cursor):
            self._assert_fence(cursor, context)
            return self._claim_session(
                cursor,
                canonical_issue_key=canonical_issue_key,
                candidate_session_id=candidate_session_id,
                risk_tier=risk_tier,
                context=context,
                now=now,
            )

    def observe_event(
        self,
        event: IssueEvent,
        *,
        candidate_session_id: str,
        risk_tier: RiskTier,
        context: MutationContext,
        now: datetime,
    ) -> ObservationResult:
        with self._transaction() as (_connection, cursor):
            self._assert_fence(cursor, context)
            duplicate = self._existing_observation(cursor, event)
            if duplicate:
                return duplicate
            historical_session = self._closed_session_for_stale_event(cursor, event)
            if historical_session is not None:
                self._insert_event(
                    cursor,
                    event=event,
                    session_id=historical_session.session_id,
                    context=context,
                    now=now,
                )
                return ObservationResult(
                    EventDisposition.STALE,
                    historical_session,
                )
            session, _created = self._claim_session(
                cursor,
                canonical_issue_key=event.issue_key,
                candidate_session_id=candidate_session_id,
                risk_tier=risk_tier,
                context=context,
                now=now,
            )
            duplicate = self._existing_observation(cursor, event)
            if duplicate:
                return duplicate
            cursor.execute(
                """
                SELECT state, state_revision, last_github_version,
                       last_github_tiebreaker
                FROM issue_sessions
                WHERE session_id = %s
                """,
                (session.session_id,),
            )
            version_row = _required_row(
                cursor.fetchone(),
                operation="read session GitHub version",
            )
            last_github_version = version_row["last_github_version"]
            last_github_tiebreaker = version_row["last_github_tiebreaker"]
            tiebreaker = _event_tiebreaker(event)
            applied = (event.github_version, tiebreaker) > (
                last_github_version,
                last_github_tiebreaker,
            )
            self._insert_event(
                cursor,
                event=event,
                session_id=session.session_id,
                context=context,
                now=now,
            )
            if not applied:
                return ObservationResult(EventDisposition.STALE, session)

            projected_context_version = _durable_context_version(
                state=IssueState(version_row["state"]),
                state_revision=version_row["state_revision"],
                github_version=event.github_version,
                github_tiebreaker=tiebreaker,
                lease_epoch=context.lease_epoch,
            )
            cursor.execute(
                """
                UPDATE issue_sessions
                SET last_github_version = %s,
                    last_github_tiebreaker = %s,
                    risk_tier = %s,
                    lease_epoch = %s,
                    context_version = %s,
                    updated_at = %s
                WHERE session_id = %s
                RETURNING *
                """,
                (
                    event.github_version,
                    tiebreaker,
                    risk_tier.value,
                    context.lease_epoch,
                    projected_context_version,
                    now,
                    session.session_id,
                ),
            )
            session = self._session_from_row(
                _required_row(cursor.fetchone(), operation="project event")
            )
            return ObservationResult(EventDisposition.APPLIED, session)

    def transition_session(
        self,
        *,
        issue_key: str,
        expected_session_id: str,
        target: IssueState,
        expected_context_version: int,
        context: MutationContext,
        now: datetime,
    ) -> IssueSession:
        canonical_issue_key = _canonical_issue_key(issue_key)
        with self._transaction() as (_connection, cursor):
            self._assert_fence(cursor, context)
            cursor.execute(
                """
                SELECT *
                FROM issue_sessions
                WHERE issue_key = %s
                  AND session_id = %s
                  AND ended_at IS NULL
                FOR UPDATE
                """,
                (canonical_issue_key, expected_session_id),
            )
            row = cursor.fetchone()
            if not row:
                raise RepositoryConflict(
                    f"expected session {expected_session_id!r} is not active"
                )
            return self._transition_locked_session(
                cursor,
                row=row,
                target=target,
                expected_context_version=expected_context_version,
                context=context,
                now=now,
            )

    def ensure_session_triaged(
        self,
        *,
        issue_key: str,
        expected_session_id: str,
        context: MutationContext,
        now: datetime,
    ) -> IssueSession:
        canonical_issue_key = _canonical_issue_key(issue_key)
        with self._transaction() as (_connection, cursor):
            self._assert_fence(cursor, context)
            cursor.execute(
                """
                SELECT *
                FROM issue_sessions
                WHERE issue_key = %s
                  AND session_id = %s
                  AND ended_at IS NULL
                FOR UPDATE
                """,
                (canonical_issue_key, expected_session_id),
            )
            row = cursor.fetchone()
            if not row:
                raise RepositoryConflict(
                    f"expected session {expected_session_id!r} is not active"
                )
            row, session = self._adopt_locked_session_fence(
                cursor,
                row=row,
                context=context,
                now=now,
            )
            if session.state is IssueState.TRIAGED:
                return session
            if session.state is not IssueState.DISCOVERED:
                raise RepositoryConflict(
                    f"initial triage cannot accept session state {session.state.value}"
                )
            return self._transition_locked_session(
                cursor,
                row=row,
                target=IssueState.TRIAGED,
                expected_context_version=session.context_version,
                context=context,
                now=now,
            )

    def _transition_locked_session(
        self,
        cursor: Cursor[dict[str, Any]],
        *,
        row: dict[str, Any],
        target: IssueState,
        expected_context_version: int,
        context: MutationContext,
        now: datetime,
    ) -> IssueSession:
        session = self._session_from_row(row)
        updated = apply_transition(
            session,
            target=target,
            expected_context_version=expected_context_version,
            lease_epoch=context.lease_epoch,
        )
        state_revision = row["state_revision"] + 1
        transitioned_context_version = _durable_context_version(
            state=updated.state,
            state_revision=state_revision,
            github_version=row["last_github_version"],
            github_tiebreaker=row["last_github_tiebreaker"],
            lease_epoch=context.lease_epoch,
        )
        ended_at = now if target is IssueState.CLOSED else None
        cursor.execute(
            """
            UPDATE issue_sessions
            SET state = %s,
                context_version = %s,
                state_revision = %s,
                updated_at = %s,
                ended_at = %s
            WHERE session_id = %s
            RETURNING *
            """,
            (
                updated.state.value,
                transitioned_context_version,
                state_revision,
                now,
                ended_at,
                updated.session_id,
            ),
        )
        persisted = self._session_from_row(
            _required_row(cursor.fetchone(), operation="transition session")
        )
        self._append_snapshot(
            cursor,
            session=persisted,
            kind="state_transition",
            context=context,
            now=now,
        )
        return persisted

    def get_session(self, issue_key: str) -> IssueSession:
        canonical_issue_key = _canonical_issue_key(issue_key)
        with self._transaction() as (_connection, cursor):
            cursor.execute(
                """
                SELECT *
                FROM issue_sessions
                WHERE issue_key = %s AND ended_at IS NULL
                """,
                (canonical_issue_key,),
            )
            row = cursor.fetchone()
            if not row:
                raise RepositoryError(f"no active session for {canonical_issue_key}")
            return self._session_from_row(row)

    def count_active_sessions(self, issue_key: str) -> int:
        canonical_issue_key = _canonical_issue_key(issue_key)
        with self._transaction() as (_connection, cursor):
            cursor.execute(
                """
                SELECT count(*) AS count
                FROM issue_sessions
                WHERE issue_key = %s AND ended_at IS NULL
                """,
                (canonical_issue_key,),
            )
            return _required_row(
                cursor.fetchone(),
                operation="count active sessions",
            )["count"]

    def event_count(self, issue_key: str) -> int:
        canonical_issue_key = _canonical_issue_key(issue_key)
        with self._transaction() as (_connection, cursor):
            cursor.execute(
                "SELECT count(*) AS count FROM issue_events WHERE issue_key = %s",
                (canonical_issue_key,),
            )
            return _required_row(
                cursor.fetchone(),
                operation="count issue events",
            )["count"]

    def latest_github_version(self, issue_key: str) -> int:
        canonical_issue_key = _canonical_issue_key(issue_key)
        with self._transaction() as (_connection, cursor):
            cursor.execute(
                """
                SELECT last_github_version
                FROM issue_sessions
                WHERE issue_key = %s AND ended_at IS NULL
                """,
                (canonical_issue_key,),
            )
            row = cursor.fetchone()
            if not row:
                raise RepositoryError(f"no active session for {canonical_issue_key}")
            return row["last_github_version"]

    def list_session_mutations(self, issue_key: str) -> list[SessionMutation]:
        canonical_issue_key = _canonical_issue_key(issue_key)
        with self._transaction() as (_connection, cursor):
            cursor.execute(
                """
                SELECT mutation_kind, issue_key, session_id, run_id,
                       lease_epoch, context_version, recorded_at
                FROM issue_session_snapshots
                WHERE issue_key = %s
                ORDER BY snapshot_sequence
                """,
                (canonical_issue_key,),
            )
            return [
                SessionMutation(
                    kind=row["mutation_kind"],
                    issue_key=row["issue_key"],
                    session_id=row["session_id"],
                    run_id=row["run_id"],
                    lease_epoch=row["lease_epoch"],
                    context_version=row["context_version"],
                    recorded_at=row["recorded_at"],
                )
                for row in cursor.fetchall()
            ]

    def issue_session_trace(
        self,
        *,
        issue_key: str | None = None,
        run_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if issue_key is not None:
            issue_key = _canonical_issue_key(issue_key)
        if run_id is not None and not run_id.strip():
            raise ValueError("run_id filter must be nonblank")
        if not 1 <= limit <= 500:
            raise ValueError("trace limit must be between 1 and 500")
        with self._transaction() as (_connection, cursor):
            cursor.execute(
                """
                SELECT issue_key, session_id, run_id, lease_epoch,
                       mutation_kind AS kind, recorded_at
                FROM (
                    SELECT issue_key, session_id, run_id, lease_epoch,
                           mutation_kind, recorded_at, snapshot_sequence AS sequence
                    FROM issue_session_snapshots
                    UNION ALL
                    SELECT issue_key, session_id, run_id, lease_epoch,
                           'event_observed' AS mutation_kind,
                           recorded_at, ledger_sequence AS sequence
                    FROM issue_events
                ) AS trace
                WHERE (%s::text IS NULL OR issue_key = %s::text)
                  AND (%s::text IS NULL OR run_id = %s::text)
                ORDER BY recorded_at DESC, sequence DESC
                LIMIT %s
                """,
                (issue_key, issue_key, run_id, run_id, limit),
            )
            return [
                {
                    "issue_key": row["issue_key"],
                    "session_id": row["session_id"],
                    "run_id": row["run_id"],
                    "lease_epoch": row["lease_epoch"],
                    "kind": row["kind"],
                    "recorded_at": row["recorded_at"].isoformat(),
                }
                for row in cursor.fetchall()
            ]

    def record_reconciliation_started(
        self,
        repository: str,
        run_id: str,
        context: MutationContext,
        now: datetime,
    ) -> None:
        with self._transaction() as (_connection, cursor):
            self._assert_fence(cursor, context)
            cursor.execute(
                """
                INSERT INTO issue_reconciliation_status(
                    repository, run_id, started_at, completed_at,
                    open_issue_count, observed_issue_count, error, lease_epoch
                )
                VALUES (%s, %s, clock_timestamp(), NULL, 0, 0, NULL, %s)
                ON CONFLICT (repository) DO UPDATE
                SET run_id = EXCLUDED.run_id,
                    started_at = EXCLUDED.started_at,
                    completed_at = NULL,
                    open_issue_count = 0,
                    observed_issue_count = 0,
                    error = NULL,
                    lease_epoch = EXCLUDED.lease_epoch
                """,
                (repository, run_id, context.lease_epoch),
            )

    def record_reconciliation_completed(
        self,
        repository: str,
        run_id: str,
        *,
        open_issue_count: int,
        observed_issue_count: int,
        newest_github_updated_at: datetime | None,
        context: MutationContext,
        now: datetime,
    ) -> None:
        with self._transaction() as (_connection, cursor):
            self._assert_fence(cursor, context)
            cursor.execute(
                """
                UPDATE issue_reconciliation_status
                SET completed_at = clock_timestamp(),
                    newest_github_updated_at = %s,
                    open_issue_count = %s,
                    observed_issue_count = %s,
                    error = NULL,
                    lease_epoch = %s
                WHERE repository = %s AND run_id = %s
                """,
                (
                    newest_github_updated_at,
                    open_issue_count,
                    observed_issue_count,
                    context.lease_epoch,
                    repository,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RepositoryConflict(
                    "reconciliation completion does not match the active run"
                )

    def record_reconciliation_failed(
        self,
        repository: str,
        run_id: str,
        error: str,
        context: MutationContext,
        now: datetime,
    ) -> None:
        with self._transaction() as (_connection, cursor):
            self._assert_fence(cursor, context)
            cursor.execute(
                """
                UPDATE issue_reconciliation_status
                SET completed_at = clock_timestamp(),
                    error = %s,
                    lease_epoch = %s
                WHERE repository = %s AND run_id = %s
                """,
                (
                    error[:1024],
                    context.lease_epoch,
                    repository,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RepositoryConflict(
                    "reconciliation failure does not match the active run"
                )

    def ping(self) -> bool:
        with self._transaction() as (_connection, cursor):
            cursor.execute("SELECT 1 AS ok")
            return (
                _required_row(
                    cursor.fetchone(),
                    operation="ping PostgreSQL",
                )["ok"]
                == 1
            )

    def control_status(
        self,
        *,
        now: datetime,
        authorized_repositories: tuple[str, ...],
    ) -> dict[str, Any]:
        with self._transaction() as (_connection, cursor):
            cursor.execute(
                """
                SELECT leader_node, lease_epoch, renewed_at,
                       clock_timestamp() AS database_now
                FROM issue_control_leases
                WHERE cluster_name = %s
                """,
                (self.cluster_name,),
            )
            lease = cursor.fetchone()
            if not lease:
                raise RepositoryError("cluster is not bootstrapped")
            database_now = lease["database_now"]
            lease_age = max(
                0.0,
                (database_now - lease["renewed_at"]).total_seconds(),
            )
            cursor.execute(
                """
                SELECT node_id, configured_role, ready, observed_epoch,
                       last_seen_at, detail
                FROM issue_control_nodes
                ORDER BY configured_role, node_id
                """
            )
            nodes = []
            for row in cursor.fetchall():
                heartbeat_age = (
                    max(
                        0.0,
                        (database_now - row["last_seen_at"]).total_seconds(),
                    )
                    if row["last_seen_at"]
                    else None
                )
                current_role = (
                    "leader" if row["node_id"] == lease["leader_node"] else "standby"
                )
                nodes.append({
                    "node_id": row["node_id"],
                    "role": current_role,
                    "configured_role": row["configured_role"],
                    "ready": bool(row["ready"])
                    and heartbeat_age is not None
                    and heartbeat_age < self.takeover_after.total_seconds(),
                    "observed_epoch": row["observed_epoch"],
                    "last_seen_at": (
                        row["last_seen_at"].isoformat() if row["last_seen_at"] else None
                    ),
                    "heartbeat_age_seconds": heartbeat_age,
                    "detail": row["detail"],
                })
            cursor.execute(
                """
                SELECT repository, run_id, started_at, completed_at,
                       newest_github_updated_at, open_issue_count,
                       observed_issue_count, error, lease_epoch
                FROM issue_reconciliation_status
                WHERE repository = ANY(%s)
                ORDER BY repository
                """,
                (list(authorized_repositories),),
            )
            by_repository = {row["repository"]: row for row in cursor.fetchall()}
            reconciliation = []
            for repository in authorized_repositories:
                row = by_repository.get(repository)
                if not row:
                    reconciliation.append({
                        "repository": repository,
                        "run_id": None,
                        "started_at": None,
                        "completed_at": None,
                        "lag_seconds": None,
                        "open_issue_count": 0,
                        "observed_issue_count": 0,
                        "classified": False,
                        "error": None,
                        "lease_epoch": None,
                    })
                    continue
                lag = (
                    max(
                        0.0,
                        (database_now - row["completed_at"]).total_seconds(),
                    )
                    if row["completed_at"]
                    else None
                )
                reconciliation.append({
                    "repository": repository,
                    "run_id": row["run_id"],
                    "started_at": (
                        row["started_at"].isoformat() if row["started_at"] else None
                    ),
                    "completed_at": (
                        row["completed_at"].isoformat() if row["completed_at"] else None
                    ),
                    "lag_seconds": lag,
                    "open_issue_count": row["open_issue_count"],
                    "observed_issue_count": row["observed_issue_count"],
                    "classified": (
                        row["completed_at"] is not None
                        and row["error"] is None
                        and row["open_issue_count"] == row["observed_issue_count"]
                    ),
                    "error": row["error"],
                    "lease_epoch": row["lease_epoch"],
                })
            return {
                "leader": {
                    "node_id": lease["leader_node"],
                    "lease_epoch": lease["lease_epoch"],
                    "renewed_at": lease["renewed_at"].isoformat(),
                    "lease_age_seconds": lease_age,
                    "eligible_for_takeover": lease_age
                    >= self.takeover_after.total_seconds(),
                },
                "nodes": nodes,
                "reconciliation": reconciliation,
            }

    def schema_capabilities(self) -> dict[str, bool]:
        with self._transaction() as (_connection, cursor):
            cursor.execute(
                """
                SELECT
                    EXISTS (
                        SELECT 1 FROM pg_trigger
                        WHERE tgname = 'issue_events_append_only'
                    ) AS append_only_events,
                    EXISTS (
                        SELECT 1 FROM pg_trigger
                        WHERE tgname = 'issue_snapshots_append_only'
                    ) AS append_only_snapshots,
                    EXISTS (
                        SELECT 1 FROM pg_extension WHERE extname = 'vector'
                    ) AS pgvector
                """
            )
            return dict(
                _required_row(
                    cursor.fetchone(),
                    operation="read schema capabilities",
                )
            )

    def unsafe_update_event_for_test(self, event_id: str) -> None:
        with self._transaction() as (_connection, cursor):
            cursor.execute(
                """
                UPDATE issue_events
                SET event_type = event_type
                WHERE event_id = %s
                """,
                (event_id,),
            )

    def age_lease_for_test(self, elapsed: timedelta) -> None:
        if not self.schema.startswith("issue_control_test_"):
            raise RepositoryError("refusing to alter a non-test lease")
        if elapsed < timedelta(0):
            raise ValueError("lease age must be non-negative")
        with self._transaction() as (_connection, cursor):
            cursor.execute(
                """
                UPDATE issue_control_leases
                SET renewed_at = clock_timestamp() - %s
                WHERE cluster_name = %s
                """,
                (elapsed, self.cluster_name),
            )
            if cursor.rowcount != 1:
                raise RepositoryError("cluster is not bootstrapped")

    def age_reconciliation_for_test(
        self,
        repository: str,
        elapsed: timedelta,
    ) -> None:
        if not self.schema.startswith("issue_control_test_"):
            raise RepositoryError("refusing to alter non-test reconciliation status")
        if elapsed < timedelta(0):
            raise ValueError("reconciliation age must be non-negative")
        with self._transaction() as (_connection, cursor):
            cursor.execute(
                """
                UPDATE issue_reconciliation_status
                SET completed_at = clock_timestamp() - %s
                WHERE repository = %s
                """,
                (elapsed, repository),
            )
            if cursor.rowcount != 1:
                raise RepositoryError("reconciliation status is not recorded")

    def drop_schema_for_test(self) -> None:
        if not self.schema.startswith("issue_control_test_"):
            raise RepositoryError("refusing to drop a non-test schema")
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(
                        sql.Identifier(self.schema)
                    )
                )


def _canonical_issue_key(value: str) -> str:
    repository, separator, number = value.rpartition("#")
    if separator != "#" or not number.isdecimal():
        raise ValueError("issue_key must have the form owner/repo#number")
    canonical = make_issue_key(repository, int(number))
    if canonical != value:
        raise ValueError("issue_key must be canonical")
    return canonical


def _required_row(
    row: dict[str, Any] | None,
    *,
    operation: str,
) -> dict[str, Any]:
    if row is None:
        raise RepositoryError(
            f"PostgreSQL returned no row while attempting to {operation}"
        )
    return row


def _assert_same_event(row: dict[str, Any], event: IssueEvent) -> None:
    immutable = {
        "issue_key": event.issue_key,
        "github_version": event.github_version,
        "event_type": event.event_type,
        "actor_kind": event.actor_kind.value,
        "occurred_at": event.occurred_at,
        "sanitized_payload_ref": event.sanitized_payload_ref,
    }
    if any(row[key] != value for key, value in immutable.items()):
        raise RepositoryConflict(
            f"event identity collision for {event.event_id!r} with different facts"
        )


def _event_tiebreaker(event: IssueEvent) -> str:
    """Order same-version observations independently of delivery order."""
    identity = "\0".join((
        event.event_type,
        event.sanitized_payload_ref,
        event.event_id,
    ))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _durable_context_version(
    *,
    state: IssueState,
    state_revision: int,
    github_version: int,
    github_tiebreaker: str,
    lease_epoch: int,
) -> int:
    identity = "\0".join((
        state.value,
        str(state_revision),
        str(github_version),
        github_tiebreaker,
        str(lease_epoch),
    ))
    version = int.from_bytes(
        hashlib.sha256(identity.encode("utf-8")).digest()[:8],
        "big",
    ) & ((1 << 63) - 1)
    return version or 1


__all__ = [
    "DEFAULT_RENEWAL_INTERVAL",
    "DEFAULT_TAKEOVER_AFTER",
    "EventDisposition",
    "LeadershipDecision",
    "MutationContext",
    "ObservationResult",
    "PostgresIssueRepository",
    "RepositoryConflict",
    "RepositoryError",
    "SessionMutation",
    "StaleFenceError",
]
