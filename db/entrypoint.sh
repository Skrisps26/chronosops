#!/bin/sh
set -eu

acl_dir=/var/lib/falkordb/acl
acl_file="$acl_dir/users.acl"

for variable in \
    FALKORDB_ADMIN_PASSWORD \
    FALKORDB_READER_ACME_PASSWORD \
    FALKORDB_READER_GLOBEX_PASSWORD \
    FALKORDB_WRITER_ACME_PASSWORD \
    FALKORDB_WRITER_GLOBEX_PASSWORD; do
    eval "value=\${$variable:-}"
    if [ -z "$value" ]; then
        echo "missing required database credential: $variable" >&2
        exit 64
    fi
done

umask 077

# The server owns graph namespaces. Production retains the established names;
# isolated tests must use an explicit, constrained prefix so an ACL can never
# accidentally grant access to a production graph.
graph_prefix=${CHRONOSOPS_GRAPH_PREFIX:-}
if [ -n "$graph_prefix" ]; then
    case "$graph_prefix" in
        test_*)
            prefix_suffix=${graph_prefix#test_}
            case "$prefix_suffix" in
                ''|*[!a-z0-9_]*)
                    echo "invalid CHRONOSOPS_GRAPH_PREFIX" >&2
                    exit 64
                    ;;
            esac
            ;;
        *)
            echo "invalid CHRONOSOPS_GRAPH_PREFIX" >&2
            exit 64
            ;;
    esac
fi

company_graph="${graph_prefix}company_brain"
topology_graph="${graph_prefix}ops_topology"
memory_acme_graph="${graph_prefix}agent_memory_acme"
memory_globex_graph="${graph_prefix}agent_memory_globex"

mkdir -p "$acl_dir"
temporary_acl="$acl_dir/users.acl.tmp"

# Graph names are Redis ACL keys. Readers can inspect only the shared graphs,
# their own memory graph, and dedicated unprefixed probe graphs. Writers do
# not receive company access, graph enumeration, deletion, or admin ACL
# commands. The only non-graph commands are connection-handshake commands.
cat >"$temporary_acl" <<EOF
user default off
user admin on >${FALKORDB_ADMIN_PASSWORD} ~* &* +@all
user reader_acme on >${FALKORDB_READER_ACME_PASSWORD} ~${company_graph} ~${topology_graph} ~${memory_acme_graph} ~probe_acme &* -@all +ping +hello +info +client|setinfo +client|setname +graph.ro_query
user reader_globex on >${FALKORDB_READER_GLOBEX_PASSWORD} ~${company_graph} ~${topology_graph} ~${memory_globex_graph} ~probe_globex &* -@all +ping +hello +info +client|setinfo +client|setname +graph.ro_query
user writer_acme on >${FALKORDB_WRITER_ACME_PASSWORD} ~${topology_graph} ~${memory_acme_graph} ~probe_acme &* -@all +ping +hello +info +client|setinfo +client|setname +graph.query +graph.ro_query
user writer_globex on >${FALKORDB_WRITER_GLOBEX_PASSWORD} ~${topology_graph} ~${memory_globex_graph} ~probe_globex &* -@all +ping +hello +info +client|setinfo +client|setname +graph.query +graph.ro_query
EOF

chmod 600 "$temporary_acl"
if [ "$(id -u)" = 0 ]; then
    chown 999:999 "$temporary_acl"
fi
mv -f "$temporary_acl" "$acl_file"

exec /var/lib/falkordb/bin/run.sh
