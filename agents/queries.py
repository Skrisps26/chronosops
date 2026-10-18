"""Centralized OpenCypher queries and algorithm procedures for ChronosOps."""
from __future__ import annotations

CHECK_READY_QUERY = """
MATCH (m:Meta {id: 'ops_topology_meta'})
RETURN m.ready AS ready, m.hash AS hash
"""

CHECK_SERVICE_EXISTS = """
MATCH (s:Service {service_id: $service_id})
RETURN s.service_id AS service_id, s.name AS name, s.health AS health, s.owner_team AS owner_team
"""

# Blast radius: services that depend on the failed root within depth 5
BLAST_RADIUS_DEPENDENTS = """
MATCH (dependent:Service)-[:DEPENDS_ON*1..5]->(failed:Service {service_id: $service_id})
RETURN DISTINCT dependent.service_id AS service_id
LIMIT 100
"""

BLAST_RADIUS_PATHS = """
MATCH p = (dependent:Service)-[:DEPENDS_ON*1..5]->(failed:Service {service_id: $service_id})
RETURN [n IN nodes(p) | n.service_id] AS chain, length(p) AS hop_count
LIMIT 100
"""

BLAST_RADIUS_DEPTH_FRONTIER = """
MATCH (dependent:Service)-[:DEPENDS_ON*6..6]->(failed:Service {service_id: $service_id})
RETURN count(dependent) AS count
"""

# Failover route evidence: fixed 3-second callers validate endpoint metadata,
# then project the same first five SPpaths candidates with every node and edge
# value needed to make an eligibility decision in Python.
FAILOVER_ROUTE_CONTEXT = """
MATCH (m:Meta {id: 'ops_topology_meta'})
OPTIONAL MATCH (s:Service {service_id: $source})
OPTIONAL MATCH (t:Service {service_id: $target})
RETURN m.ready AS topology_ready,
       m.hash AS topology_version,
       s.service_id AS source_id,
       s.health AS source_health,
       t.service_id AS target_id,
       t.health AS target_health
"""

FAILOVER_ROUTE_CANDIDATES = """
MATCH (m:Meta {id: 'ops_topology_meta'})
MATCH (s:Service {service_id: $source}), (t:Service {service_id: $target})
CALL algo.SPpaths({
  sourceNode: s,
  targetNode: t,
  relTypes: ['ROUTES_TO'],
  weightProp: 'latency_ms',
  costProp: 'cost',
  maxLen: 5,
  relDirection: 'outgoing',
  pathCount: 5
})
YIELD path, pathWeight, pathCost
RETURN m.hash AS topology_version,
       [n IN nodes(path) | n.service_id] AS route,
       [n IN nodes(path) | n.health] AS node_health,
       [r IN relationships(path) | r.health] AS edge_health,
       [r IN relationships(path) | r.cost] AS edge_costs,
       [r IN relationships(path) | r.latency_ms] AS edge_latency_ms,
       pathWeight AS latency_ms,
       pathCost AS cost
ORDER BY latency_ms ASC, cost ASC
"""

MEMORY_ROUTE_EXCLUSIONS = """
MATCH (f:SemanticFact {active: true})
RETURN f.id AS fact_id,
       f.subject AS subject,
       f.predicate AS predicate,
       f.object AS object,
       f.reason AS reason,
       f.topology_version AS topology_version,
       f.max_cost AS max_cost,
       f.source_version AS source_version
ORDER BY fact_id ASC
LIMIT 100
"""

# Choke points: betweenness centrality on DEPENDS_ON network
CHOKE_POINTS_BETWEENNESS = """
CALL algo.betweenness({
  nodeLabels: ['Service'],
  relationshipTypes: ['DEPENDS_ON'],
  samplingSize: 32,
  samplingSeed: 42
})
YIELD node, score
RETURN node.service_id AS service_id, score
ORDER BY score DESC
LIMIT 10
"""
