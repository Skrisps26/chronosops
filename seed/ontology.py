"""Constrained Ontology definition for ChronosOps Company Brain."""
from __future__ import annotations

from pathlib import Path
from graphrag_sdk import Attribute, Column, Entity, Ontology, Relation, TableMapping

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "seed" / "data"


def get_company_ontology() -> Ontology:
    """Build and return the formal ontology for Company Brain."""
    entities = [
        Entity(
            label="Service",
            description="Production service or microservice with canonical service_id.",
            properties=[
                Attribute(name="service_id", type="STRING"),
                Attribute(name="owner_team", type="STRING"),
                Attribute(name="environment", type="STRING"),
                Attribute(name="tier", type="STRING"),
            ],
        ),
        Entity(
            label="Person",
            description="Company engineer, lead, or operator.",
            properties=[
                Attribute(name="person_id", type="STRING"),
                Attribute(name="email", type="STRING"),
                Attribute(name="team", type="STRING"),
            ],
        ),
        Entity(
            label="Team",
            description="Engineering squad or business unit.",
            properties=[
                Attribute(name="team_id", type="STRING"),
            ],
        ),
        Entity(
            label="Decision",
            description="Architectural or operational decision record.",
            properties=[
                Attribute(name="decision_id", type="STRING"),
                Attribute(name="status", type="STRING"),
            ],
        ),
        Entity(
            label="Ticket",
            description="Issue tracker ticket or incident record.",
            properties=[
                Attribute(name="ticket_id", type="STRING"),
                Attribute(name="status", type="STRING"),
            ],
        ),
        Entity(
            label="PullRequest",
            description="Code change or pull request.",
            properties=[
                Attribute(name="pr_id", type="STRING"),
            ],
        ),
        Entity(
            label="SlackThread",
            description="Discussion thread in an operational Slack channel.",
            properties=[
                Attribute(name="channel", type="STRING"),
                Attribute(name="timestamp", type="STRING"),
            ],
        ),
    ]

    relations = [
        Relation(label="MEMBER_OF", description="Person is a member of a Team", patterns=[("Person", "Team")]),
        Relation(label="OWNS", description="Team owns a Service", patterns=[("Team", "Service")]),
        Relation(
            label="AUTHORED",
            description="Person authored a document, ticket, PR, or Slack thread",
            patterns=[
                ("Person", "Decision"),
                ("Person", "Ticket"),
                ("Person", "PullRequest"),
                ("Person", "SlackThread"),
            ],
        ),
        Relation(label="RESOLVED", description="Person resolved an issue or ticket", patterns=[("Person", "Ticket")]),
        Relation(
            label="ABOUT",
            description="Item pertains to a specific Service",
            patterns=[
                ("Decision", "Service"),
                ("Ticket", "Service"),
                ("PullRequest", "Service"),
                ("SlackThread", "Service"),
            ],
        ),
    ]

    tables = [
        TableMapping(
            source=str(DATA_DIR / "services.csv"),
            label="Service",
            key="service_id",
            name="service_name",
            properties={
                "environment": Column("environment", "STRING"),
                "owner_team": Column("owner_team", "STRING"),
                "tier": Column("tier", "STRING"),
            },
        ),
        TableMapping(
            source=str(DATA_DIR / "people.csv"),
            label="Person",
            key="person_id",
            name="name",
            properties={
                "email": Column("email", "STRING"),
                "team": Column("team", "STRING"),
            },
        ),
        TableMapping(
            source=str(DATA_DIR / "teams.csv"),
            label="Team",
            key="team_id",
            name="name",
        ),
    ]

    return Ontology(entities=entities, relations=relations, tables=tables)


if __name__ == "__main__":
    ont = get_company_ontology()
    print(f"Ontology compiled: {len(ont.entities)} entities, {len(ont.relations)} relations, {len(ont.tables)} tables.")
