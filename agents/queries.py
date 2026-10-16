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

# Failover candidate queries via algo.SPpaths
FAILOVER_ROUTES_UNCONSTRAINED = """
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
RETURN [n IN nodes(path) | n.service_id] AS route, pathWeight AS latency_ms, pathCost AS cost
"""

FAILOVER_ROUTES_CONSTRAINED = """
MATCH (s:Service {service_id: $source}), (t:Service {service_id: $target})
CALL algo.SPpaths({
  sourceNode: s,
  targetNode: t,
  relTypes: ['ROUTES_TO'],
  weightProp: 'latency_ms',
  costProp: 'cost',
  maxCost: $max_cost,
  maxLen: 5,
  relDirection: 'outgoing',
  pathCount: 5
})
YIELD path, pathWeight, pathCost
RETURN [n IN nodes(path) | n.service_id] AS route, pathWeight AS latency_ms, pathCost AS cost
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
