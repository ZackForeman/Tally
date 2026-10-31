"""Fold work-log rows and repeat photos of the same ticket into one job.

The same load often arrives twice: once as a ticket photo and once as a row in
the driver's work log. Counting both would claim the load twice, so records
sharing an issuer ticket number are merged, with the ticket's own reading
winning and the log filling in what the ticket doesn't print (miles, usually).
"""

from __future__ import annotations

from collections import defaultdict

from tally.engine.models import Job, Source
from tally.engine.normalize import customer_key, ticket_key


def merge_jobs(jobs: list[Job]) -> list[Job]:
    groups: dict[tuple, list[Job]] = defaultdict(list)
    out: list[Job] = []
    for job in jobs:
        key = ticket_key(job.ticket_no)
        if key is None:
            out.append(job)
        else:
            groups[(customer_key(job.customer), key, job.leg)].append(job)
    for group in groups.values():
        # The base record is a ticket if there is one, and one that carries the load.
        group.sort(key=lambda j: (j.source is not Source.TICKET, not j.carries_load))
        merged = group[0]
        for other in group[1:]:
            fill = {
                name: getattr(other, name)
                for name in Job.model_fields
                if getattr(merged, name) is None and getattr(other, name) is not None
            }
            merged = merged.model_copy(update=fill | {"evidence": merged.evidence + other.evidence})
        out.append(merged)
    return out
