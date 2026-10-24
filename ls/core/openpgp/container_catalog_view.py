"""Bounded catalog projections that preserve all authenticated version heads."""
from .container_reader import ContainerReadError
from .container_versions import VersionNode, build_version_graph, version_heads


def index_rows(result, artifact):
    records = result.snapshot.catalog['containers']
    records = [row for row in records if artifact is None or row['artifact_id'] == artifact]
    if artifact is not None and not records:
        raise ContainerReadError('unavailable')
    graph = build_version_graph(VersionNode(row['artifact_id'], row['version_id'],
        tuple(row['parents']), row['commitment_sha256']) for row in records)
    current = {(item.metadata.logical['artifact_id'], item.metadata.logical['version_id'])
               for item in result.observations}
    yield {'kind': 'artifacts' if artifact is None else 'versions', 'payload_verified': False}
    for identity in sorted({row['artifact_id'] for row in records}):
        heads = version_heads(graph, artifact_id=identity)
        collisions = {version for owner, version in graph.collisions if owner == identity}
        missing = any(owner == identity for owner, _ in graph.missing_parents)
        cyclic = {version for cycle in graph.cycles for owner, version in cycle if owner == identity}
        nodes = [node for node in graph.nodes if node.artifact_id == identity]
        if artifact is None:
            yield {'path': '@artifacts/'+identity, 'heads': list(heads),
                   'state': 'conflict' if len(heads)>1 or collisions or cyclic else 'incomplete' if missing else 'indexed'}
        else:
            for node in nodes:
                yield {'path': '@artifacts/'+identity+'/'+node.version_id,
                       'parents': list(node.parents), 'head': node.version_id in heads,
                       'state': 'conflict' if node.version_id in collisions or node.version_id in cyclic else 'indexed',
                       'metadata_verified': (identity, node.version_id) in current,
                       'payload_verified': False}
            for version in sorted(collisions):
                yield {'path': '@artifacts/'+identity+'/'+version, 'state': 'conflict',
                       'metadata_verified': False, 'payload_verified': False}
