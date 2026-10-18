"""Constrained Ontology definition for ChronosOps Company Brain."""
from __future__ import annotations
from pathlib import Path

from graphrag_sdk import Attribute, Column, Entity, Link, Ontology, Relation, TableMapping
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "seed" / "data"


def get_company_ontology() -> Ontology:
    """Build the declared Company Brain ontology and structured source mappings."""
    entities = [
        Entity(
            label="Service",
            description="Production service or microservice from services.csv.",
            properties=[],
        ),
        Entity(
            label="Person",
            description="Company engineer, lead, or operator from people.csv.",
            properties=[],
        ),
        Entity(
            label="Team",
            description="Engineering squad or business unit from teams.csv.",
            properties=[],
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
        Relation(label="OWNED_BY", description="Service is owned by a Team", patterns=[("Service", "Team")]),
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
            # entity_key is SDK reconciliation state.  The canonical ID is a
            # separately declared property, written by the SDK as
            # services__service_id.
            key="service_name",
            name="service_name",
            properties={
                "service_id": Column("service_id", "STRING"),
                "environment": Column("environment", "STRING"),
                "owner_team": Column("owner_team", "STRING"),
                "tier": Column("tier", "STRING"),
            },
            links=[Link("OWNED_BY", to="Team", by="owner_team")],
        ),
        TableMapping(
            source=str(DATA_DIR / "people.csv"),
            label="Person",
            key="name",
            name="name",
            properties={
                "person_id": Column("person_id", "STRING"),
                "email": Column("email", "STRING"),
                "team": Column("team", "STRING"),
            },
            links=[Link("MEMBER_OF", to="Team", by="team")],
        ),
        TableMapping(
            source=str(DATA_DIR / "teams.csv"),
            label="Team",
            key="name",
            name="name",
            properties={"team_id": Column("team_id", "STRING")},
        ),
    ]

    return Ontology(entities=entities, relations=relations, tables=tables)


if __name__ == "__main__":
    ont = get_company_ontology()
    print(f"Ontology compiled: {len(ont.entities)} entities, {len(ont.relations)} relations, {len(ont.tables)} tables.")
