"""Who builds next on the live board.

Each team has at most one queued commit (a newer push replaces it), at most
one job in flight, and at most one dispatch per `min_interval_s` (15 minutes
on the box). Among the teams whose next job may start, the one served least
recently goes first; a team never served goes before any team that has been.

"Served" means dispatched: the moment the board takes the team's commit off
the queue to fetch and build it. Times are epoch seconds and are passed in,
so the rules can be tested without a clock.
"""
import dataclasses


@dataclasses.dataclass
class TeamState:
    slug: str
    head: str = ""                   # last sha seen on the remote branch ("" = none)
    head_seen_at: float = 0.0
    pending: dict | None = None      # {"commit", "pushed_at", "attempt"}
    inflight: dict | None = None     # {"job_id", "commit", "pushed_at", "dispatched_at", "attempt", "prev_*"}
    last_dispatched_at: float | None = None
    last_dispatched_commit: str = ""
    checked_at: float = 0.0
    watch_error: str = ""

    def to_json(self):
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, data):
        fields = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in fields})


class FairQueue:
    def __init__(self, min_interval_s):
        self.min_interval_s = float(min_interval_s)
        self.teams = {}

    def team(self, slug):
        if slug not in self.teams:
            self.teams[slug] = TeamState(slug=slug)
        return self.teams[slug]

    # -- watching -------------------------------------------------------------

    def observe(self, slug, sha, now):
        """Record the branch head seen by git ls-remote. True if a run was queued or replaced."""
        st = self.team(slug)
        st.checked_at = now
        st.watch_error = ""
        if not sha:
            st.head = ""
            return False
        if sha == st.head:
            return False
        st.head, st.head_seen_at = sha, now
        # The commit being built or run, or the one last dispatched, is not new work.
        current = st.inflight["commit"] if st.inflight else st.last_dispatched_commit
        if sha == current:
            st.pending = None
            return False
        if st.pending and st.pending["commit"] == sha:
            return False
        st.pending = {"commit": sha, "pushed_at": now, "attempt": 1}
        return True

    # -- dispatching ----------------------------------------------------------

    def eligible_at(self, st):
        """When the team's queued commit may be dispatched, ignoring a job still in flight."""
        if not st.pending:
            return None
        if st.last_dispatched_at is None:
            return st.pending["pushed_at"]
        return max(st.pending["pushed_at"], st.last_dispatched_at + self.min_interval_s)

    def _key(self, st):
        last = st.last_dispatched_at if st.last_dispatched_at is not None else float("-inf")
        return (last, st.pending["pushed_at"], st.slug)

    def next_eligible(self, now):
        ready = [st for st in self.teams.values()
                 if st.pending and not st.inflight and self.eligible_at(st) <= now]
        return min(ready, key=self._key) if ready else None

    def next_wakeup(self, now):
        """Earliest future moment a queued team becomes eligible (None if nothing is waiting on time)."""
        times = [self.eligible_at(st) for st in self.teams.values() if st.pending and not st.inflight]
        future = [t for t in times if t > now]
        return min(future) if future else None

    def dispatch(self, slug, now, job_id):
        st = self.teams[slug]
        p = st.pending
        st.inflight = {
            "job_id": job_id, "commit": p["commit"], "pushed_at": p["pushed_at"],
            "dispatched_at": now, "attempt": p.get("attempt", 1),
            "prev_dispatched_at": st.last_dispatched_at,
            "prev_dispatched_commit": st.last_dispatched_commit,
        }
        st.pending = None
        st.last_dispatched_at = now
        st.last_dispatched_commit = p["commit"]
        return dict(st.inflight)

    def set_commit(self, slug, commit):
        """The clone found a newer head than ls-remote saw; the job runs that one instead."""
        st = self.teams[slug]
        if st.inflight:
            st.inflight["commit"] = commit
        st.last_dispatched_commit = commit
        if st.pending and st.pending["commit"] == commit:
            st.pending = None

    def finish(self, slug):
        self.teams[slug].inflight = None

    def requeue_interrupted(self, slug, max_attempts):
        """After a restart: put an interrupted job back at the front of its team's queue.

        Returns the interrupted inflight dict, and whether it was requeued (False
        once it has been interrupted max_attempts times, so a job that crashes the
        board cannot loop forever).
        """
        st = self.teams[slug]
        inf = st.inflight
        if not inf:
            return None, False
        st.inflight = None
        st.last_dispatched_at = inf.get("prev_dispatched_at")
        st.last_dispatched_commit = inf.get("prev_dispatched_commit") or ""
        if inf.get("attempt", 1) >= max_attempts:
            st.last_dispatched_at = inf["dispatched_at"]
            st.last_dispatched_commit = inf["commit"]
            return inf, False
        if st.pending is None:
            st.pending = {"commit": inf["commit"], "pushed_at": inf["pushed_at"], "attempt": inf.get("attempt", 1) + 1}
        return inf, True

    # -- reporting ------------------------------------------------------------

    def queue_order(self, now):
        """Queued teams in their estimated dispatch order: [(slug, eligible_at)].

        A team that may start now goes ahead of one still inside its interval;
        among teams that may start, least recently served first. A team with a
        job in flight waits at least for its interval and for that job.
        """
        waiting = [st for st in self.teams.values() if st.pending]
        waiting.sort(key=lambda st: (max(now, self.eligible_at(st)), *self._key(st)))
        return [(st.slug, self.eligible_at(st)) for st in waiting]
